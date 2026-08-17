from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.adapters.contracts import Adapter
from app.schemas import AdapterManifest

AdapterFactory = Callable[[], Adapter]


class AdapterRegistrationError(ValueError):
    pass


class AdapterCompatibilityError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class RegisteredAdapter:
    manifest: AdapterManifest
    factory: AdapterFactory


def _semver_core(value: str) -> tuple[int, int, int]:
    core = value.split("+", 1)[0].split("-", 1)[0]
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch)


class AdapterRegistry:
    """Allow-list of built-in adapter factories; never imports from configuration."""

    def __init__(
        self,
        *,
        hub_version: str,
        known_capabilities: Collection[str] = (),
    ) -> None:
        self._hub_version = _semver_core(hub_version)
        self._known_capabilities = frozenset(known_capabilities)
        self._entries: dict[str, RegisteredAdapter] = {}

    def register(self, factory: AdapterFactory) -> AdapterManifest:
        adapter = factory()
        manifest = adapter.manifest
        if manifest.adapter_id in self._entries:
            raise AdapterRegistrationError(f"adapter already registered: {manifest.adapter_id}")
        self._entries[manifest.adapter_id] = RegisteredAdapter(manifest, factory)
        return manifest

    def manifest(self, adapter_id: str) -> AdapterManifest:
        try:
            return self._entries[adapter_id].manifest
        except KeyError as error:
            raise LookupError(f"adapter not registered: {adapter_id}") from error

    def create(self, adapter_id: str) -> Adapter:
        entry = self._entry(adapter_id)
        self._validate_compatibility(entry.manifest)
        adapter = entry.factory()
        if adapter.manifest != entry.manifest:
            raise AdapterRegistrationError("adapter factory returned a different manifest")
        return adapter

    def list_manifests(self) -> tuple[AdapterManifest, ...]:
        return tuple(entry.manifest for entry in self._entries.values())

    def _entry(self, adapter_id: str) -> RegisteredAdapter:
        try:
            return self._entries[adapter_id]
        except KeyError as error:
            raise LookupError(f"adapter not registered: {adapter_id}") from error

    def _validate_compatibility(self, manifest: AdapterManifest) -> None:
        if self._hub_version < _semver_core(manifest.min_hub_version):
            raise AdapterCompatibilityError("hub_upgrade_required")
        unknown = set(manifest.capabilities.critical) - self._known_capabilities
        if unknown:
            raise AdapterCompatibilityError("upgrade_required")


class RuntimeState(StrEnum):
    CONFIGURED = "configured"
    READY = "ready"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True)
class AdapterInstance:
    instance_id: UUID
    adapter: Adapter
    config: dict[str, object]
    state: RuntimeState
    reason_code: str | None = None


class AdapterLifecycleManager:
    """Supervises adapter instances and preserves the last usable configuration."""

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry
        self._instances: dict[UUID, AdapterInstance] = {}

    async def start(
        self,
        instance_id: UUID,
        adapter_id: str,
        config: Mapping[str, object],
    ) -> AdapterInstance:
        if instance_id in self._instances:
            raise ValueError(f"adapter instance already exists: {instance_id}")
        adapter = self._registry.create(adapter_id)
        instance = AdapterInstance(instance_id, adapter, dict(config), RuntimeState.CONFIGURED)
        self._instances[instance_id] = instance
        try:
            await adapter.configure(instance.config)
            await adapter.start()
        except Exception as error:
            instance.state = RuntimeState.FAILED
            instance.reason_code = f"{type(error).__name__}"
            await self._safe_stop(adapter)
            raise
        instance.state = RuntimeState.READY
        return instance

    async def reload(self, instance_id: UUID, config: Mapping[str, object]) -> AdapterInstance:
        instance = self.get(instance_id)
        previous = instance.config
        candidate = dict(config)
        try:
            await instance.adapter.reload(candidate)
        except Exception:
            try:
                await instance.adapter.reload(previous)
            except Exception as recovery_error:
                instance.state = RuntimeState.FAILED
                instance.reason_code = type(recovery_error).__name__
            else:
                instance.state = RuntimeState.READY
                instance.reason_code = "reload_rejected_rolled_back"
            raise
        instance.config = candidate
        instance.state = RuntimeState.READY
        instance.reason_code = None
        return instance

    async def stop(self, instance_id: UUID) -> None:
        instance = self.get(instance_id)
        try:
            await instance.adapter.stop()
        except Exception as error:
            instance.state = RuntimeState.FAILED
            instance.reason_code = type(error).__name__
            raise
        instance.state = RuntimeState.STOPPED

    def get(self, instance_id: UUID) -> AdapterInstance:
        try:
            return self._instances[instance_id]
        except KeyError as error:
            raise LookupError(f"adapter instance not found: {instance_id}") from error

    @staticmethod
    async def _safe_stop(adapter: Adapter) -> None:
        with suppress(Exception):
            await adapter.stop()
