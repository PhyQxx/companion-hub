"""Resolve declared package exports without loading unrelated adapters."""

from collections.abc import Mapping
from importlib import import_module
from typing import Any


def resolve_export(
    package: str,
    namespace: dict[str, Any],
    targets: Mapping[str, tuple[str, str]],
    name: str,
) -> Any:
    target = targets.get(name)
    if target is None:
        raise AttributeError(f"module {package!r} has no attribute {name!r}")
    module, attribute = target
    value = getattr(import_module(module), attribute)
    namespace[name] = value
    return value
