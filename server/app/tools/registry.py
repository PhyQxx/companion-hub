from __future__ import annotations

from collections.abc import Iterable

from app.llm import ToolDefinition

from .contracts import ToolHandler


class ToolRegistry:
    def __init__(self, handlers: Iterable[ToolHandler] = ()) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: ToolHandler) -> None:
        if handler.name in self._handlers:
            raise ValueError(f"duplicate tool: {handler.name}")
        self._handlers[handler.name] = handler

    def get(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def definitions(self, names: Iterable[str] | None = None) -> list[ToolDefinition]:
        if names is None:
            return [handler.definition() for handler in self._handlers.values()]
        return [
            self._handlers[name].definition()
            for name in names
            if name in self._handlers
        ]
