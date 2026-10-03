"""Output ownership must be enforced even without a caller's Run wrapper."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_proactive_delivery import active_owner, service

from app.cognition import CognitiveDecision, DecisionKind, Urgency
from app.config import ConfigStore, ProactiveOutputConfig
from app.db import AppUserRecord, ProactiveDeliveryReceiptRecord
from app.ids import uuid7
from app.output import ProactiveDeliveryService
from app.output.adapter import DeliveryIntent, DeliveryReceipt
from app.output.contracts import ProactiveChannelAttempt, ProactiveOutputRepository
from app.schemas import PrivacyLevel


def config() -> ProactiveOutputConfig:
    return ProactiveOutputConfig.model_validate(
        {
            "delivery_mode": "all_enabled",
            "web_chat": {"enabled": True, "priority": 100},
            "desktop_notification": {"enabled": True, "priority": 80},
            "voice": {"enabled": True, "priority": 60},
        }
    )


async def deliver(
    output: ProactiveDeliveryService,
    user_id: UUID | None,
    *,
    decision: CognitiveDecision | None = None,
    privacy: PrivacyLevel = PrivacyLevel.L1,
) -> Any:
    return await output.deliver(
        "Private fixture",
        entity_id="fixture",
        rule_id="fixture",
        trigger_kind="fixture",
        privacy_level=privacy,
        target_user_id=user_id,
        cognitive_decision=decision,
        broadcast=True,
    )


@pytest.mark.parametrize("state", ["missing", "disabled", "implicit_disabled", "L3"])
async def test_invalid_owner_or_ephemeral_output_never_reaches_adapters(state: str) -> None:
    output, chat, broadcaster, gateway, voice, database = await service(config())
    try:
        owner = await active_owner(database)
        if state in {"disabled", "implicit_disabled"}:
            async with database.sessions.begin() as session:
                await session.execute(
                    update(AppUserRecord).where(AppUserRecord.id == owner).values(status="disabled")
                )
            await active_owner(database)  # Must not take over the implicit private source.
        target = None if state == "implicit_disabled" else uuid7() if state == "missing" else owner
        assert (
            await deliver(
                output, target, privacy=PrivacyLevel.L3 if state == "L3" else PrivacyLevel.L1
            )
            is None
        )
        assert (
            not chat.calls
            and not broadcaster.messages
            and not gateway.commands
            and not voice.messages
        )
        async with database.sessions() as session:
            assert list(await session.scalars(select(ProactiveDeliveryReceiptRecord))) == []
    finally:
        await database.close()


class Repository:
    def __init__(self) -> None:
        self.owner = uuid7()
        self.active = True
        self.recorded: list[tuple[ProactiveChannelAttempt, ...]] = []

    async def default_owner(self) -> UUID | None:
        return self.owner if self.active else None

    async def owner_active(self, user_id: UUID) -> bool:
        return self.active and user_id == self.owner

    async def record_attempts(
        self,
        user_id: UUID,
        attempts: tuple[ProactiveChannelAttempt, ...],
        *,
        privacy_level: PrivacyLevel,
        decision_id: UUID | None,
    ) -> None:
        assert user_id == self.owner
        assert privacy_level == PrivacyLevel.L1
        self.recorded.append(attempts)


class Adapter:
    available = True

    def __init__(self, name: str, hook: Callable[[], Awaitable[None]] | None = None) -> None:
        self.name = name
        self.hook = hook
        self.intents: list[DeliveryIntent] = []

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt:
        self.intents.append(intent)
        if self.hook is not None:
            await self.hook()
        return DeliveryReceipt(True, self.name, external_operation_id="fixture-receipt")


def owned_decision(owner: UUID) -> CognitiveDecision:
    now = datetime.now(UTC)
    return CognitiveDecision(
        id=uuid7(),
        event_id=uuid7(),
        user_id=owner,
        trigger_kind="fixture",
        decision=DecisionKind.INFORM,
        reason_codes=["fixture"],
        evidence_ids=["fixture"],
        confidence=1,
        urgency=Urgency.NORMAL,
        attention_score=1,
        policy_version="fixture",
        message="Private fixture",
        expires_at=now + timedelta(minutes=1),
        created_at=now,
    )


def port_service(
    repository: ProactiveOutputRepository, current: Any, adapters: list[Any]
) -> ProactiveDeliveryService:
    return ProactiveDeliveryService(
        cast(Any, None),
        cast(ConfigStore, current),
        cast(Any, None),
        cast(Any, None),
        repository=repository,
        adapters=adapters,
    )


@pytest.mark.parametrize("reason", ["different_owner", "expired", "expired_naive"])
async def test_decision_owner_and_expiry_are_bound_before_output(reason: str) -> None:
    repository = Repository()
    decision = owned_decision(repository.owner)
    if reason == "different_owner":
        decision = decision.model_copy(update={"user_id": uuid7()})
    else:
        expires = datetime.now(UTC) - timedelta(seconds=1)
        if reason == "expired_naive":
            expires = expires.replace(tzinfo=None)
        decision = decision.model_copy(update={"expires_at": expires})
    current = SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(proactive_output=config()))
    )
    adapter = Adapter("web_chat")
    output = port_service(repository, current, [adapter])
    assert await deliver(output, repository.owner, decision=decision) is None
    assert not adapter.intents and not repository.recorded


@pytest.mark.parametrize("revocation", ["owner", "decision", "global", "channel", "privacy"])
async def test_each_channel_rechecks_admission_without_erasing_prior_receipts(
    revocation: str,
) -> None:
    repository = Repository()
    decision = owned_decision(repository.owner)
    if revocation == "decision":
        decision = decision.model_copy(
            update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.2)}
        )
    current = SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(proactive_output=config()))
    )

    async def revoke() -> None:
        if revocation == "owner":
            repository.active = False
        elif revocation == "decision":
            await asyncio.sleep(0.3)
        else:
            replacement = config().model_dump(mode="json")
            if revocation == "global":
                replacement["enabled"] = False
            elif revocation == "channel":
                replacement["desktop_notification"]["enabled"] = False
                replacement["voice"]["enabled"] = False
            else:
                replacement["desktop_notification"]["max_privacy_level"] = "L0"
                replacement["voice"]["max_privacy_level"] = "L0"
            current.current = SimpleNamespace(
                config=SimpleNamespace(
                    proactive_output=ProactiveOutputConfig.model_validate(replacement)
                )
            )

    first = Adapter("web_chat", revoke)
    second, third = Adapter("desktop_notification"), Adapter("voice")
    output = port_service(repository, current, [first, second, third])
    result = await deliver(output, repository.owner, decision=decision)
    assert result is not None and result.delivered_channels == ("web_chat",)
    assert not second.intents and not third.intents
    assert repository.recorded == [result.attempts]


async def test_later_adapter_failure_keeps_earlier_known_receipt() -> None:
    repository = Repository()
    current = SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(proactive_output=config()))
    )

    async def fail() -> None:
        raise RuntimeError("fixture interrupted adapter")

    first, second = Adapter("web_chat"), Adapter("desktop_notification", fail)
    output = port_service(repository, current, [first, second])
    with pytest.raises(RuntimeError, match="fixture interrupted adapter"):
        await deliver(output, repository.owner)
    assert len(first.intents) == 1 and len(second.intents) == 1
    assert len(repository.recorded) == 1
    assert [(receipt.channel, receipt.delivered) for receipt in repository.recorded[0]] == [
        ("web_chat", True)
    ]


async def test_config_is_read_after_awaiting_owner_admission() -> None:
    current = SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(proactive_output=config()))
    )

    class RevokeDuringAdmission(Repository):
        checks = 0

        async def owner_active(self, user_id: UUID) -> bool:
            self.checks += 1
            if self.checks == 2:
                replacement = config().model_copy(update={"enabled": False})
                current.current = SimpleNamespace(
                    config=SimpleNamespace(proactive_output=replacement)
                )
            return await super().owner_active(user_id)

    repository = RevokeDuringAdmission()
    adapter = Adapter("web_chat")
    output = port_service(repository, current, [adapter])
    assert await deliver(output, repository.owner) is None
    assert not adapter.intents


async def test_sql_audit_retains_known_delivery_after_owner_disabled_between_channels() -> None:
    output, _, _, _, _, database = await service(config())
    try:
        owner = await active_owner(database)

        async def disable() -> None:
            async with database.sessions.begin() as session:
                await session.execute(
                    update(AppUserRecord).where(AppUserRecord.id == owner).values(status="disabled")
                )

        first, second = Adapter("web_chat", disable), Adapter("desktop_notification")
        output._adapters = [first, second]
        result = await deliver(output, owner)
        assert result is not None and result.delivered_channels == ("web_chat",)
        assert not second.intents
        async with database.sessions() as session:
            receipts = list(await session.scalars(select(ProactiveDeliveryReceiptRecord)))
        assert [(receipt.user_id, receipt.channel, receipt.status) for receipt in receipts] == [
            (owner, "web_chat", "delivered")
        ]
    finally:
        await database.close()
