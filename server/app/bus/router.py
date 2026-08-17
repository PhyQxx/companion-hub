from __future__ import annotations

from collections import defaultdict

from app.db import Database
from app.schemas import InputEnvelope

from .service import EventHandler, consume_event


class LocalEventPublisher:
    """Routes outbox topics to named, idempotent in-process consumers."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._routes: dict[str, list[tuple[str, EventHandler]]] = defaultdict(list)

    def subscribe(self, topic: str, consumer_name: str, handler: EventHandler) -> None:
        if any(name == consumer_name for name, _ in self._routes[topic]):
            raise ValueError(f"duplicate consumer registration: {consumer_name}")
        self._routes[topic].append((consumer_name, handler))

    async def publish(self, topic: str, event: InputEnvelope) -> None:
        routes = self._routes.get(topic)
        if not routes:
            raise LookupError(f"no consumer registered for topic: {topic}")
        for consumer_name, handler in routes:
            async with self._database.sessions.begin() as session:
                await consume_event(
                    session,
                    consumer_name=consumer_name,
                    event_id=event.event_id,
                    handler=handler,
                )
