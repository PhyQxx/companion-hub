"""Read-only owned conversation, browser session and satellite authority."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db import (
    AppUserRecord,
    AuthSessionRecord,
    ConversationRecord,
    Database,
    DeviceClientRecord,
    TaskRunRecord,
)
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied
from app.harness.joined_read import joined_read
from app.harness.source_cleanup import close_after_source
from app.harness.time import utc
from app.harness.voice_sources import VoiceAuthority, VoiceRecipientClaim, VoiceRunFence
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

    async def validate_live_runs(
        self, source: VoiceAuthority, fences: tuple[VoiceRunFence, ...]
    ) -> None:
        # Preserve the source-specific denial, then read the actor and every
        # delivery/quota run in one final SQL snapshot. A source read can wait
        # while a previously checked run is cancelled or expires.
        await self.validate(source)
        await joined_read(self._validate_live_runs(source, fences))

    async def _validate_live_runs(
        self, source: VoiceAuthority, fences: tuple[VoiceRunFence, ...]
    ) -> None:
        if not fences or len({fence.run_id for fence in fences}) != len(fences):
            raise BudgetDenied("budget_run_inactive")
        root_conversation = aliased(ConversationRecord)
        query = (
            select(
                TaskRunRecord.id,
                TaskRunRecord.status,
                TaskRunRecord.privacy_level,
                TaskRunRecord.deadline,
                TaskRunRecord.contract["work_cancel_requested"].as_boolean().label("cancel"),
                TaskRunRecord.contract["budget_usage_overflow"].as_boolean().label("overflow"),
                TaskRunRecord.budget["enabled"].as_boolean().label("budget_enabled"),
            )
            .join(AppUserRecord, AppUserRecord.id == TaskRunRecord.user_id)
            .outerjoin(root_conversation, root_conversation.id == TaskRunRecord.conversation_id)
            .where(
                TaskRunRecord.id.in_([fence.run_id for fence in fences]),
                TaskRunRecord.user_id == source.user_id,
                AppUserRecord.status == "active",
                or_(
                    TaskRunRecord.conversation_id.is_(None),
                    (root_conversation.user_id == source.user_id)
                    & (root_conversation.status == "active"),
                ),
            )
        )
        if not isinstance(source, VoiceRecipientClaim):
            query = query.join(
                ConversationRecord, ConversationRecord.user_id == AppUserRecord.id
            ).where(
                ConversationRecord.id == source.conversation_id,
                ConversationRecord.status == "active",
            )
        browser = not isinstance(source, VoiceRecipientClaim) and source.actor == "browser"
        if browser:
            assert not isinstance(source, VoiceRecipientClaim)
            query = (
                query.add_columns(AuthSessionRecord.expires_at.label("actor_expires_at"))
                .join(AuthSessionRecord, AuthSessionRecord.user_id == AppUserRecord.id)
                .where(
                    AuthSessionRecord.id == source.actor_id,
                    AuthSessionRecord.revoked_at.is_(None),
                    AuthSessionRecord.expires_at > datetime.now(UTC),
                )
            )
        else:
            device_id = (
                source.device_id if isinstance(source, VoiceRecipientClaim) else source.actor_id
            )
            query = (
                query.add_columns(
                    DeviceClientRecord.capabilities, DeviceClientRecord.granted_capabilities
                )
                .join(DeviceClientRecord, DeviceClientRecord.owner_user_id == AppUserRecord.id)
                .where(DeviceClientRecord.id == device_id, DeviceClientRecord.revoked_at.is_(None))
            )
        session = self._database.sessions()
        async with close_after_source(session.close):
            await assert_current_claim(session)
            rows = (await session.execute(query)).mappings().all()
        if len(rows) != len(fences):
            raise BudgetDenied("budget_run_inactive")
        expected = {fence.run_id: fence.budget_enabled for fence in fences}
        now = datetime.now(UTC)
        capability = (
            source.capability if isinstance(source, VoiceRecipientClaim) else SATELLITE_CAPABILITY
        )
        for row in rows:
            if row["status"] not in {"accepted", "running"} or row["cancel"]:
                raise BudgetDenied("budget_run_inactive")
            if row["overflow"]:
                raise BudgetDenied("budget_usage_overflow")
            if row["budget_enabled"] is not expected[row["id"]]:
                raise BudgetDenied("budget_snapshot_missing")
            if row["privacy_level"] > str(source.privacy_level):
                raise BudgetDenied("operation_privacy_downgrade")
            if row["deadline"] is None or utc(row["deadline"]) <= now:
                raise BudgetDenied("run_deadline_exceeded")
            if browser:
                if utc(row["actor_expires_at"]) <= now:
                    raise BudgetDenied("voice_session_inactive")
            elif (
                capability not in row["capabilities"]
                or capability not in row["granted_capabilities"]
            ):
                raise BudgetDenied("voice_device_inactive")

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
