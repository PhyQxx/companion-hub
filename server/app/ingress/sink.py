from __future__ import annotations

from collections.abc import Callable, Iterable

from app.bus import append_event
from app.db import Database
from app.privacy import PrivacyPolicyRegistry
from app.schemas import EphemeralSignal, InputEnvelope

from .buffer import EphemeralBuffer

TopicResolver = Callable[[InputEnvelope], Iterable[str]]


class DatabaseInputSink:
    def __init__(self, database: Database, topics: TopicResolver) -> None:
        self._database = database
        self._topics = topics

    async def emit_durable(self, event: InputEnvelope) -> None:
        async with self._database.sessions.begin() as session:
            await append_event(session, event, topics=self._topics(event))


class GuardedInputSink:
    """The sole adapter ingress: classify first, then route by durability."""

    def __init__(
        self,
        durable: DatabaseInputSink,
        ephemeral: EphemeralBuffer,
        privacy: PrivacyPolicyRegistry,
    ) -> None:
        self._durable = durable
        self._ephemeral = ephemeral
        self._privacy = privacy

    async def emit_durable(self, event: InputEnvelope) -> None:
        await self._durable.emit_durable(self._privacy.classify_durable(event))

    async def emit_ephemeral(self, signal: EphemeralSignal) -> None:
        await self._ephemeral.put(self._privacy.classify_ephemeral(signal))
