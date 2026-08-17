from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .models import HubConfig

ConfigValidator = Callable[[HubConfig], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    version: int
    content_hash: str
    config: HubConfig
    published_at: datetime
    rollback_from: int | None = None


@dataclass(frozen=True, slots=True)
class ConfigAudit:
    action: str
    success: bool
    version: int | None
    occurred_at: datetime
    reason_code: str | None = None


class ConfigStore:
    def __init__(
        self,
        path: Path,
        *,
        validators: tuple[ConfigValidator, ...] = (),
    ) -> None:
        self._path = path
        self._validators = validators
        self._lock = asyncio.Lock()
        self._current: ConfigSnapshot | None = None
        self._history: dict[int, ConfigSnapshot] = {}
        self.audit: list[ConfigAudit] = []
        self.last_error: str | None = None

    @property
    def current(self) -> ConfigSnapshot:
        if self._current is None:
            raise RuntimeError("configuration has not been loaded")
        return self._current

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> ConfigSnapshot:
        return await self.reload(force=True)

    async def reload(self, *, force: bool = False) -> ConfigSnapshot:
        async with self._lock:
            try:
                candidate, content_hash = self._read_candidate()
                if (
                    not force
                    and self._current is not None
                    and self._current.content_hash == content_hash
                ):
                    return self._current
                for validator in self._validators:
                    await validator(candidate)
            except Exception as error:
                self.last_error = type(error).__name__
                self.audit.append(
                    ConfigAudit("publish", False, None, datetime.now(UTC), self.last_error)
                )
                raise
            version = 1 if self._current is None else self._current.version + 1
            snapshot = ConfigSnapshot(version, content_hash, candidate, datetime.now(UTC))
            self._current = snapshot
            self._history[version] = snapshot
            self.last_error = None
            self.audit.append(ConfigAudit("publish", True, version, datetime.now(UTC)))
            return snapshot

    async def rollback(self, version: int) -> ConfigSnapshot:
        async with self._lock:
            try:
                target = self._history[version]
            except KeyError as error:
                raise LookupError(f"configuration version not found: {version}") from error
            for validator in self._validators:
                await validator(target.config)
            next_version = self.current.version + 1
            snapshot = ConfigSnapshot(
                next_version,
                target.content_hash,
                target.config,
                datetime.now(UTC),
                rollback_from=self.current.version,
            )
            self._current = snapshot
            self._history[next_version] = snapshot
            self.audit.append(ConfigAudit("rollback", True, next_version, datetime.now(UTC)))
            return snapshot

    def _read_candidate(self) -> tuple[HubConfig, str]:
        raw = self._path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise ValueError("configuration root must be a mapping")
        config = HubConfig.model_validate(parsed)
        canonical = json.dumps(
            config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return config, hashlib.sha256(canonical.encode()).hexdigest()


class ConfigWatcher:
    def __init__(self, store: ConfigStore, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._store = store
        self._path = store.path
        self._poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_mtime_ns: int | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._last_mtime_ns = self._path.stat().st_mtime_ns
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-config-watcher")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            if self._stop.is_set():
                break
            try:
                mtime_ns = self._path.stat().st_mtime_ns
            except OSError:
                # A transient replace/delete must not terminate the watcher. The
                # currently published configuration remains authoritative.
                continue
            if mtime_ns == self._last_mtime_ns:
                continue
            self._last_mtime_ns = mtime_ns
            try:
                await self._store.reload()
            except Exception:
                # The store records a payload-free audit and retains the prior snapshot.
                continue
