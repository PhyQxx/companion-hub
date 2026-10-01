from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update
from test_chat import _summary_service, create_user

from app.cognition import ActionInvocation, ActionPlanService, build_builtin_action_registry
from app.db import ActionStepRecord
from app.ids import uuid7
from app.runs.outcomes import action_outcome
from app.schemas import PrivacyLevel


@pytest.mark.parametrize(
    ("status", "policy", "verifier", "verification", "level", "effect", "retry"),
    [
        ("completed", "receipt", "skill.write_receipt", "verified", "V1", "submitted", "never"),
        ("completed", "receipt", "device.command_receipt", "verified", "V1", "submitted", "never"),
        (
            "completed",
            "read_after_write",
            "home.entity_state",
            "verified",
            "V3",
            "confirmed",
            "never",
        ),
        ("completed", "read_after_write", "unknown", "verified", "V0", "submitted", "never"),
        ("completed", "none", None, "not_required", "V0", "submitted", "never"),
        (
            "unknown_outcome",
            "receipt",
            "device.command_receipt",
            "pending",
            "V0",
            "unknown",
            "manual_reconcile",
        ),
        (
            "cancelled",
            "receipt",
            "device.command_receipt",
            "pending",
            "V0",
            "unknown",
            "manual_reconcile",
        ),
        (
            "failed",
            "read_after_write",
            "home.entity_state",
            "inconclusive",
            "V0",
            "unknown",
            "manual_reconcile",
        ),
    ],
)
def test_execution_signal_does_not_overstate_verification(
    status: str,
    policy: str,
    verifier: str | None,
    verification: str,
    level: str,
    effect: str,
    retry: str,
) -> None:
    step = ActionStepRecord(
        id=uuid7(),
        status=status,
        risk="A2",
        verification_policy=policy,
        verifier_id=verifier,
        verification_status=verification,
        started_at=datetime.now(UTC),
        reason_code="reason",
        result={"secret": "private response"},
        verification_result={"secret": "private evidence"},
    )
    outcome = action_outcome(step)
    assert outcome.validation_level == level
    assert outcome.side_effect_state == effect
    assert outcome.retry_class == retry
    assert "private" not in outcome.model_dump_json()


async def test_run_detail_links_owned_action_evidence_without_body(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="evidence")
        turn = await service.send_message(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        plans = ActionPlanService(database, build_builtin_action_registry())
        plan = await plans.create_plan(
            user_id=user.id,
            title="private title",
            source_turn_id=turn.assistant_message.turn_id,
            invocations=[
                ActionInvocation(
                    action_id="home.light.turn_off", arguments={"target": "private room"}
                )
            ],
            idempotency_key="evidence-plan",
        )
        async with database.sessions.begin() as session:
            await session.execute(
                update(ActionStepRecord)
                .where(ActionStepRecord.id == plan.steps[0].id)
                .values(
                    status="completed",
                    started_at=datetime.now(UTC),
                    verification_status="verified",
                    verification_result={"observed_state": "off"},
                )
            )
        detail = await service.runs.get(turn.assistant_message.turn_id, user_id=user.id)
        assert detail.plan_ids == [plan.id]
        assert len(detail.action_outcomes) == 1
        outcome = detail.action_outcomes[0]
        assert outcome.step_id == plan.steps[0].id
        assert outcome.outcome.validation_level == "V3"
        assert outcome.outcome.evidence_refs[0].source_id == str(plan.steps[0].id)
        assert "private" not in detail.model_dump_json()
        with pytest.raises(LookupError):
            await service.runs.get(detail.id, user_id=uuid7())
    finally:
        await service.drain_background_work()
        await database.close()
