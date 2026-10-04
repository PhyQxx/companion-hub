"""Keep device speech authority alive until the last frame has been sent."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select, update

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ConversationRecord, Database, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied, budget_scope, current_budget
from app.harness.guarded_call import guarded_inline_call
from app.harness.joined_read import join_on_cancel, joined_read
from app.harness.operations import OperationPolicy
from app.harness.source_cleanup import close_after_source
from app.harness.time import utc
from app.harness.voice_sources import (
    RootVoiceSourceGuard,
    VoiceAuthority,
    VoiceRunFence,
    VoiceSourceClaim,
)
from app.ids import uuid7
from app.schemas import PrivacyLevel
from app.voice.contracts import SpeechSynthesizer
from app.voice.delivery_ports import SpeechDeliveryContext, VoicePricingSource

from .budget import RunModelBudget
from .costs import assert_cost_window, check_cost_allowance
from .store import append_run_event, transition_run
from .stream_operation import stream_with_run


class SqlSpeechDelivery:
    def __init__(
        self,
        database: Database,
        config: ConfigStore | DatabaseConfigStore,
        source_guard: RootVoiceSourceGuard,
        pricing: VoicePricingSource,
    ) -> None:
        self.database, self.config = database, config
        self.source_guard, self.pricing = source_guard, pricing

    async def execute(
        self,
        source: VoiceAuthority,
        invoke: Callable[[SpeechDeliveryContext], Awaitable[bool]],
        *,
        with_text: bool = False,
    ) -> bool:
        if source.privacy_level == PrivacyLevel.L3:
            raise BudgetDenied("ephemeral_operation_run_forbidden")
        parent = current_budget()
        if parent is not None and (
            not isinstance(parent, RunModelBudget) or parent.owner_id != source.user_id
        ):
            raise BudgetDenied("budget_owner_invalid")
        await self.source_guard.validate(source)
        await recover_expired_speech_deliveries(self.database)
        snapshot = (
            await self.config.refresh()
            if isinstance(self.config, DatabaseConfigStore)
            else self.config.current
        )
        context = _Delivery(
            self,
            source,
            uuid7(),
            snapshot.version,
            snapshot.config.run_budget,
            parent if isinstance(parent, RunModelBudget) else None,
            entry="voice.proactive_output" if with_text else "voice.speech_delivery",
            criterion="text_with_optional_audio_sent" if with_text else "audio_frames_sent",
        )
        status: Literal["failed", "cancelled", "succeeded"] = "failed"
        reason = "speech_delivery_incomplete"

        async def finish() -> None:
            await join_on_cancel(context.finish(status, reason), name="speech-delivery-close")

        async with close_after_source(finish):
            try:
                await join_on_cancel(context.create(), name="speech-delivery-create")
                with budget_scope(context.budget):
                    async with asyncio.timeout(
                        max(0, (context.deadline - datetime.now(UTC)).total_seconds())
                    ):
                        result = await guarded_inline_call(
                            lambda: invoke(context), context.validate
                        )
                if result:
                    status, reason = "succeeded", context.criterion
                return result
            except asyncio.CancelledError:
                status, reason = "cancelled", "caller_cancelled"
                raise
            except TimeoutError as error:
                reason = "run_deadline_exceeded"
                raise BudgetDenied(reason) from error
            except BudgetDenied as error:
                reason = error.reason_code
                raise
            except Exception:
                reason = "speech_provider_error"
                raise


class _Delivery:
    def __init__(
        self,
        port: SqlSpeechDelivery,
        source: VoiceAuthority,
        run_id: UUID,
        config_version: int,
        config: RunBudgetConfig,
        parent: RunModelBudget | None,
        *,
        entry: str = "voice.speech_delivery",
        criterion: str = "audio_frames_sent",
    ) -> None:
        self.port, self.source, self.run_id = port, source, run_id
        self.config_version, self.config, self.parent = config_version, config, parent
        self.entry, self.criterion = entry, criterion
        self.created_at = datetime.now(UTC)
        self.deadline = self.created_at + timedelta(seconds=config.maintenance_deadline_seconds)
        self.budget = parent or (
            RunModelBudget(port.database, run_id=run_id, user_id=source.user_id, config=config)
            if config.enabled
            else None
        )
        self.currency: str | None = config.cost_currency
        self.provider: object | None = None

    def check_rows(self, rows: list[TaskRunRecord]) -> None:
        now = datetime.now(UTC)
        if self.deadline <= now:
            raise BudgetDenied("run_deadline_exceeded")
        for row in rows:
            if (
                row.user_id != self.source.user_id
                or row.status not in {"accepted", "running"}
                or row.contract.get("work_cancel_requested")
            ):
                raise BudgetDenied("budget_run_inactive")
            if row.contract.get("budget_usage_overflow"):
                raise BudgetDenied("budget_usage_overflow")
            if (
                self.parent is not None
                and row.id == self.parent.run_id
                and (not row.budget or not row.budget.get("enabled"))
            ):
                raise BudgetDenied("budget_snapshot_missing")
            if row.privacy_level > str(self.source.privacy_level):
                raise BudgetDenied("operation_privacy_downgrade")
            if row.deadline is not None and utc(row.deadline) <= now:
                raise BudgetDenied("run_deadline_exceeded")

    async def create(self) -> None:
        async with self.port.database.sessions.begin() as session:
            await assert_current_claim(session)
            owner = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == self.source.user_id, AppUserRecord.status == "active")
                .values(id=AppUserRecord.id)
                .returning(AppUserRecord.id)
            )
            if owner is None:
                raise BudgetDenied("budget_owner_invalid")
            parents = []
            if self.parent is not None:
                row = await session.scalar(
                    update(TaskRunRecord)
                    .where(
                        TaskRunRecord.id == self.parent.run_id,
                        TaskRunRecord.user_id == self.source.user_id,
                    )
                    .values(updated_at=TaskRunRecord.updated_at)
                    .returning(TaskRunRecord)
                )
                if row is None:
                    raise BudgetDenied("budget_run_inactive")
                if row.deadline is None:
                    raise BudgetDenied("run_deadline_exceeded")
                self.deadline = min(self.deadline, utc(row.deadline))
                if row.conversation_id is not None:
                    conversation = await session.scalar(
                        select(ConversationRecord.id)
                        .where(
                            ConversationRecord.id == row.conversation_id,
                            ConversationRecord.user_id == self.source.user_id,
                            ConversationRecord.status == "active",
                        )
                        .with_for_update()
                    )
                    if conversation is None:
                        raise BudgetDenied("budget_run_inactive")
                parents.append(row)
            self.check_rows(parents)
            session.add(
                TaskRunRecord(
                    id=self.run_id,
                    user_id=self.source.user_id,
                    conversation_id=self.source.conversation_id
                    if isinstance(self.source, VoiceSourceClaim)
                    else None,
                    parent_run_id=self.parent.run_id if self.parent is not None else None,
                    request_id=f"{self.entry}:{self.run_id}",
                    status="accepted",
                    privacy_level=str(self.source.privacy_level),
                    config_version=self.config_version,
                    budget=self.config.model_dump(mode="json"),
                    deadline=self.deadline,
                    contract={
                        "entry": self.entry,
                        "criterion": self.criterion,
                        "required_work": [],
                        "source_actor": source_actor(self.source),
                        "source_id": str(
                            self.source.actor_id
                            if isinstance(self.source, VoiceSourceClaim)
                            else self.source.device_id
                        ),
                    },
                    created_at=self.created_at,
                    updated_at=self.created_at,
                )
            )
            await session.flush()
            root = await session.get_one(TaskRunRecord, self.run_id)
            await append_run_event(session, root, "run.accepted")
            await transition_run(session, self.run_id, "running")
            await session.flush()
            self.check_rows(parents)

    async def validate(self) -> None:
        if self.provider is not None:
            self.port.pricing.validate_provider(self.provider)

        async def read() -> None:
            async with self.port.database.sessions() as session:
                await assert_current_claim(session)
                ids = [self.run_id, *([self.parent.run_id] if self.parent is not None else [])]
                rows = list(
                    await session.scalars(
                        select(TaskRunRecord)
                        .join(AppUserRecord, AppUserRecord.id == TaskRunRecord.user_id)
                        .outerjoin(
                            ConversationRecord,
                            ConversationRecord.id == TaskRunRecord.conversation_id,
                        )
                        .where(
                            TaskRunRecord.id.in_(ids),
                            AppUserRecord.status == "active",
                            TaskRunRecord.user_id == self.source.user_id,
                            or_(
                                TaskRunRecord.conversation_id.is_(None),
                                (ConversationRecord.user_id == self.source.user_id)
                                & (ConversationRecord.status == "active"),
                            ),
                        )
                    )
                )
                if len(rows) != len(ids):
                    raise BudgetDenied("budget_run_inactive")
                self.check_rows(rows)
                current = self.port.config.current.config.run_budget
                snapshots = [
                    self.config,
                    *(RunBudgetConfig.model_validate(row.budget) for row in rows if row.budget),
                ]
                if self.parent is not None:
                    snapshots.append(self.parent.budget_config)
                assert_cost_window(self.created_at, datetime.now(UTC), (*snapshots, current))
                currency = self.currency or current.cost_currency
                if currency is not None:
                    for snapshot in snapshots:
                        await check_cost_allowance(
                            session,
                            user_id=self.source.user_id,
                            amount=0,
                            currency=currency,
                            config=current,
                            snapshot=snapshot,
                            now=datetime.now(UTC),
                        )
                self.check_rows(rows)

        await joined_read(read())
        fences: tuple[VoiceRunFence, ...] = (VoiceRunFence(self.run_id, self.config.enabled),)
        if self.parent is not None:
            fences += (VoiceRunFence(self.parent.run_id, True),)
        await self.port.source_guard.validate_live_runs(self.source, fences)
        if self.provider is not None:
            self.port.pricing.validate_provider(self.provider)

    async def finish(self, status: str, reason: str) -> None:
        async with self.port.database.sessions.begin() as session:
            row = await session.scalar(
                update(TaskRunRecord)
                .where(
                    TaskRunRecord.id == self.run_id,
                    TaskRunRecord.user_id == self.source.user_id,
                )
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord)
            )
            if row is None or row.status not in {"accepted", "running"}:
                return
            if status == "succeeded":
                self.check_rows([row])
            row.contract = {**row.contract, "delivery_result": reason}
            await transition_run(session, self.run_id, status)

    async def synthesize(
        self, provider: SpeechSynthesizer, text: str, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        if privacy_level != self.source.privacy_level:
            raise BudgetDenied("voice_privacy_changed")
        self.provider = provider
        binding = self.port.pricing.binding_for(provider)
        quote = binding.quote if binding is not None else None
        if quote is not None:
            self.currency = quote.pricing.currency
        await self.validate()
        policy = OperationPolicy(
            binding.config_version
            if binding and binding.config_version is not None
            else self.config_version,
            tuple(self.config.model_dump().items()),
            quote,
        )
        tool = self.budget.tool_budget if self.budget is not None else None
        permit = (
            await tool.reserve_tool(tool_name="voice.tts", user_id=self.source.user_id)
            if tool
            else None
        )
        started = succeeded = False

        def create_stream() -> AsyncIterator[bytes]:
            nonlocal started
            started = True
            return provider.synthesize(text, privacy_level=privacy_level)

        async def settle() -> None:
            if tool is not None and permit is not None:
                await join_on_cancel(
                    tool.settle_tool(
                        permit.call_id,
                        reported_ok=True if succeeded else None if started else False,
                    ),
                    name="speech-tool-settle",
                )

        async with close_after_source(settle):
            stream = stream_with_run(
                self.port.database,
                policy,
                user_id=self.source.user_id,
                privacy_level=privacy_level,
                entry="voice.tts",
                create_stream=create_stream,
                evidence=lambda: {"speech_delivery_id": str(self.run_id)},
                source_guard=self.validate,
                cost_endpoint=binding.endpoint
                if binding
                else f"voice.tts.{type(provider).__name__}",
                budget_source=lambda: self.port.config.current.config.run_budget,
                trace_parent_id=self.run_id,
                missing_quote_reason="voice_cost_estimate_unavailable",
            )
            async with close_after_source(stream.aclose):
                async for chunk in stream:
                    yield chunk
            succeeded = True


def source_actor(source: VoiceAuthority) -> str:
    return source.actor if isinstance(source, VoiceSourceClaim) else source.capability


async def recover_expired_speech_deliveries(database: Database) -> int:
    """Expire interrupted delivery scopes without replaying speech or refunding fees."""
    count = 0
    kinds = or_(
        (TaskRunRecord.contract["entry"].as_string() == "voice.speech_delivery")
        & (TaskRunRecord.contract["criterion"].as_string() == "audio_frames_sent"),
        (TaskRunRecord.contract["entry"].as_string() == "voice.utterance")
        & (TaskRunRecord.contract["criterion"].as_string() == "voice_reply_sent"),
        (TaskRunRecord.contract["entry"].as_string() == "voice.proactive_output")
        & (TaskRunRecord.contract["criterion"].as_string() == "text_with_optional_audio_sent"),
    )
    while True:
        async with database.sessions() as reader:
            candidates = (
                await reader.execute(
                    select(TaskRunRecord.id, TaskRunRecord.user_id)
                    .where(
                        TaskRunRecord.status.in_({"accepted", "running"}),
                        TaskRunRecord.deadline <= datetime.now(UTC),
                        kinds,
                    )
                    .order_by(TaskRunRecord.id)
                    .limit(50)
                )
            ).all()
        if not candidates:
            return count
        recovered = []
        async with database.sessions.begin() as session:
            for identifier, owner in candidates:
                row = await session.scalar(
                    update(TaskRunRecord)
                    .where(
                        TaskRunRecord.id == identifier,
                        TaskRunRecord.user_id == owner,
                        TaskRunRecord.status.in_({"accepted", "running"}),
                        TaskRunRecord.deadline <= datetime.now(UTC),
                        kinds,
                    )
                    .values(updated_at=TaskRunRecord.updated_at)
                    .returning(TaskRunRecord)
                    .execution_options(synchronize_session=False, populate_existing=True)
                )
                if row is None:
                    continue
                row.contract = {**row.contract, "delivery_result": "expired_delivery_unknown"}
                await transition_run(session, identifier, "failed")
                recovered.append(identifier)
        count += len(recovered)
        from .resources import recover_tool_reservations

        for identifier in recovered:
            await recover_tool_reservations(database, run_id=identifier)
        if len(candidates) < 50:
            return count
