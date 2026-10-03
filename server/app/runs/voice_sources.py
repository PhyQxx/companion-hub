"""Read-only owned conversation, browser session and satellite authority."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.harness.voice_sources import VoiceAuthority, VoiceRecipientClaim
from app.satellite.models import SATELLITE_CAPABILITY


class SqlVoiceSourceGuard:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def validate(self, source: VoiceAuthority) -> None:
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

    async def _validate(self, source: VoiceAuthority) -> None:
        session = self._database.sessions()
        async with close_after_source(session.close):
            if isinstance(source, VoiceRecipientClaim):
                owner = await session.scalar(
                    select(AppUserRecord.id).where(
                        AppUserRecord.id == source.user_id, AppUserRecord.status == "active"
                    )
                )
                if owner is None:
                    raise BudgetDenied("voice_recipient_inactive")
                if source.capability not in {"avatar.chat", SATELLITE_CAPABILITY}:
                    raise BudgetDenied("voice_actor_invalid")
                await self._validate_device(
                    session, source.user_id, source.device_id, source.capability
                )
                return
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
                    select(AuthSessionRecord.expires_at)
                    .join(AppUserRecord, AppUserRecord.id == AuthSessionRecord.user_id)
                    .join(ConversationRecord, ConversationRecord.user_id == AppUserRecord.id)
                    .where(
                        AuthSessionRecord.id == source.actor_id,
                        AuthSessionRecord.user_id == source.user_id,
                        AuthSessionRecord.revoked_at.is_(None),
                        AuthSessionRecord.expires_at > datetime.now(UTC),
                        AppUserRecord.status == "active",
                        ConversationRecord.id == source.conversation_id,
                        ConversationRecord.status == "active",
                    )
                )
                if actor is None or utc(actor) <= datetime.now(UTC):
                    raise BudgetDenied("voice_session_inactive")
            elif source.actor == "satellite":
                await self._validate_device(
                    session,
                    source.user_id,
                    source.actor_id,
                    SATELLITE_CAPABILITY,
                    conversation_id=source.conversation_id,
                )
            else:
                raise BudgetDenied("voice_actor_invalid")

    @staticmethod
    async def _validate_device(
        session: AsyncSession,
        user_id: UUID,
        device_id: UUID,
        capability: str,
        *,
        conversation_id: UUID | None = None,
    ) -> None:
        query = (
            select(DeviceClientRecord.capabilities, DeviceClientRecord.granted_capabilities)
            .join(AppUserRecord, AppUserRecord.id == DeviceClientRecord.owner_user_id)
            .where(
                DeviceClientRecord.id == device_id,
                DeviceClientRecord.owner_user_id == user_id,
                DeviceClientRecord.revoked_at.is_(None),
                AppUserRecord.status == "active",
            )
        )
        if conversation_id is not None:
            query = query.join(
                ConversationRecord, ConversationRecord.user_id == AppUserRecord.id
            ).where(ConversationRecord.id == conversation_id, ConversationRecord.status == "active")
        device = (await session.execute(query)).one_or_none()
        if (
            device is None
            or capability not in device.capabilities
            or capability not in device.granted_capabilities
        ):
            raise BudgetDenied("voice_device_inactive")
