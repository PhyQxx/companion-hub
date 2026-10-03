"""Read-only owned conversation, browser session and satellite authority."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import (
    AppUserRecord,
    AuthSessionRecord,
    ConversationRecord,
    Database,
    DeviceClientRecord,
)
from app.harness.budget import BudgetDenied
from app.harness.source_cleanup import close_after_source
from app.harness.time import utc
from app.harness.voice_sources import VoiceSourceClaim
from app.satellite.models import SATELLITE_CAPABILITY


class SqlVoiceSourceGuard:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def validate(self, source: VoiceSourceClaim) -> None:
        # Acquisition/rollback must finish before cancellation escapes. A
        # cancelled read can otherwise strand an asynchronously checked-out
        # SQLite connection before the session has registered ownership.
        query = asyncio.create_task(self._validate(source))
        try:
            await asyncio.shield(query)
        except asyncio.CancelledError:
            # A second cancellation (interrupt followed by disconnect) must
            # not be forwarded to the query while it acquires or closes SQL.
            while not query.done():
                try:
                    await asyncio.shield(query)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not query.cancelled():
                query.exception()  # Consume a read failure; cancellation wins.
            raise

    async def _validate(self, source: VoiceSourceClaim) -> None:
        session = self._database.sessions()
        async with close_after_source(session.close):
            owned = await session.scalar(
                select(ConversationRecord.id)
                .join(AppUserRecord, AppUserRecord.id == ConversationRecord.user_id)
                .where(
                    ConversationRecord.id == source.conversation_id,
                    ConversationRecord.user_id == source.user_id,
                    ConversationRecord.status == "active",
                    AppUserRecord.status == "active",
                )
            )
            if owned is None:
                raise BudgetDenied("voice_source_inactive")
            if source.actor == "browser":
                actor = await session.scalar(
                    select(AuthSessionRecord.expires_at).where(
                        AuthSessionRecord.id == source.actor_id,
                        AuthSessionRecord.user_id == source.user_id,
                        AuthSessionRecord.revoked_at.is_(None),
                        AuthSessionRecord.expires_at > datetime.now(UTC),
                    )
                )
                if actor is None or utc(actor) <= datetime.now(UTC):
                    raise BudgetDenied("voice_session_inactive")
            elif source.actor == "satellite":
                device = (
                    await session.execute(
                        select(
                            DeviceClientRecord.capabilities, DeviceClientRecord.granted_capabilities
                        ).where(
                            DeviceClientRecord.id == source.actor_id,
                            DeviceClientRecord.owner_user_id == source.user_id,
                            DeviceClientRecord.revoked_at.is_(None),
                        )
                    )
                ).one_or_none()
                if (
                    device is None
                    or SATELLITE_CAPABILITY not in device.capabilities
                    or SATELLITE_CAPABILITY not in device.granted_capabilities
                ):
                    raise BudgetDenied("voice_device_inactive")
            else:
                raise BudgetDenied("voice_actor_invalid")
