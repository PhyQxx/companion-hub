from app.adapters import AdapterRegistry

from .mock import (
    MockEphemeralSensorAdapter,
    MockInputAdapter,
    MockOutputAdapter,
    MockStreamingOutputAdapter,
    MockTextInputAdapter,
    MockTextOutputAdapter,
    RecordingInputSink,
)


def create_builtin_registry() -> AdapterRegistry:
    from app import __version__

    registry = AdapterRegistry(
        hub_version=__version__,
        known_capabilities={"telemetry", "text", "speech"},
    )
    registry.register(MockTextInputAdapter)
    registry.register(MockEphemeralSensorAdapter)
    registry.register(MockTextOutputAdapter)
    registry.register(MockStreamingOutputAdapter)
    return registry

__all__ = [
    "MockEphemeralSensorAdapter",
    "MockInputAdapter",
    "MockOutputAdapter",
    "MockStreamingOutputAdapter",
    "MockTextInputAdapter",
    "MockTextOutputAdapter",
    "RecordingInputSink",
    "create_builtin_registry",
]
