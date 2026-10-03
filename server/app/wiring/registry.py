"""Explicit dependency ordering and lifecycle for incrementally migrated modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

Lifecycle = Callable[[], Awaitable[None]]
ModuleState = Literal["registered", "ready", "degraded", "blocked", "stopped"]


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    name: str
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    critical: bool = True
    start: Lifecycle | None = None
    stop: Lifecycle | None = None


class ModuleRegistry:
    def __init__(self, specs: Iterable[ModuleSpec]) -> None:
        self._specs: dict[str, ModuleSpec] = {}
        providers: set[str] = set()
        for spec in specs:
            if not spec.name or spec.name in self._specs:
                raise ValueError("module_name_duplicate_or_empty")
            if len(set(spec.provides)) != len(spec.provides) or providers.intersection(
                spec.provides
            ):
                raise ValueError("module_provider_duplicate")
            providers.update(spec.provides)
            self._specs[spec.name] = spec
        self.order = self._sort()
        self.states: dict[str, ModuleState] = dict.fromkeys(self.order, "registered")
        self.reason_codes: dict[str, str] = {}
        self._started: list[str] = []

    def _sort(self) -> tuple[str, ...]:
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name not in self._specs:
                raise ValueError("module_dependency_missing")
            if name in visiting:
                raise ValueError("module_dependency_cycle")
            if name in ordered:
                return
            visiting.add(name)
            for dependency in self._specs[name].requires:
                visit(dependency)
            visiting.remove(name)
            ordered.append(name)

        for name in self._specs:
            visit(name)
        return tuple(ordered)

    async def start(self, name: str) -> None:
        spec = self._specs[name]
        if self.states[name] != "registered":
            return
        for dependency in spec.requires:
            await self.start(dependency)
        if any(self.states[item] != "ready" for item in spec.requires):
            self.states[name] = "blocked"
            self.reason_codes[name] = "module_dependency_unavailable"
            if spec.critical:
                raise RuntimeError("module_dependency_unavailable")
            return
        try:
            if spec.start is not None:
                await spec.start()
        except BaseException as error:
            self.states[name] = "degraded"
            self.reason_codes[name] = "module_start_failed"
            if spec.stop is not None:
                try:
                    await spec.stop()
                except BaseException:
                    self.reason_codes[name] = "module_start_cleanup_failed"
            if spec.critical or not isinstance(error, Exception):
                raise
            return
        self.states[name] = "ready"
        self._started.append(name)

    async def start_all(self) -> None:
        try:
            for name in self.order:
                await self.start(name)
        except BaseException:
            # Cleanup interruption must not replace the original startup
            # failure/cancellation after releasing the started dependencies.
            with suppress(BaseException):
                await self.stop_all()
            raise

    async def stop_all(self) -> None:
        errors: list[BaseException] = []
        for name in reversed(self._started):
            try:
                callback = self._specs[name].stop
                if callback is not None:
                    await callback()
            except BaseException as error:
                self.reason_codes[name] = "module_stop_failed"
                errors.append(error)
            finally:
                self.states[name] = "stopped"
        self._started.clear()
        if errors:
            interruption = next(
                (error for error in errors if not isinstance(error, Exception)), None
            )
            if interruption is not None:
                # Retain recognizable cancellation/abort after best-effort
                # reverse cleanup, with any other failures available as cause.
                others = [error for error in errors if error is not interruption]
                if others:
                    raise interruption from BaseExceptionGroup("module_shutdown_failed", others)
                raise interruption
            # Python specializes this to ExceptionGroup for ordinary errors.
            raise BaseExceptionGroup("module_shutdown_failed", errors)
