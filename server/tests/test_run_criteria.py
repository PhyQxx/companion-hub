"""Malformed manifests and unsupported proof must never promote a goal to passed."""

import pytest

from app.db import JobRecord, TaskRunRecord
from app.harness.criteria import (
    PlanCriteriaSnapshot,
    RunCriteriaSnapshot,
    StepCriteriaSnapshot,
    WorkRequirement,
    evaluate_criteria,
)
from app.ids import uuid7
from app.runs.goals import goal_view
from app.schemas.execution import ExecutionOutcome


@pytest.mark.parametrize(
    "manifest",
    [
        "bad",
        {},
        [False],
        ["bad"],
        [{"kind": "action_plan"}],
        [{"kind": "action_plan", "id": 5}],
        [{"kind": "action_plan", "id": "bad"}],
    ],
)
def test_malformed_declared_manifest_does_not_fall_back_to_successful_root(
    manifest: object,
) -> None:
    run = TaskRunRecord(
        id=uuid7(),
        status="succeeded",
        contract={
            "criterion": "reply_committed",
            "required_work": manifest,
        },
    )
    goal = goal_view(run, [], [], [])
    assert goal.scope == "declared_run_contract"
    assert goal.status == "inconclusive" and goal.passed == 1
    assert goal.criteria[-1].reason_code == "criterion_invalid"


@pytest.mark.parametrize("oversized", [False, True])
def test_duplicate_or_oversized_manifest_is_invalid(oversized: bool) -> None:
    item = {"kind": "delegated_job", "id": str(uuid7())}
    run = TaskRunRecord(
        id=uuid7(),
        status="succeeded",
        contract={
            "required_work": [item] * (1001 if oversized else 2),
        },
    )
    assert goal_view(run, [], [], []).criteria[0].reason_code == "criterion_invalid"


@pytest.mark.parametrize("channels", ["web", [1], [{}], [], ["web"] * 17])
def test_malformed_channel_evidence_cannot_pass_delivery(channels: object) -> None:
    run = TaskRunRecord(
        id=uuid7(),
        status="succeeded",
        contract={
            "criterion": "delivery_channels_returned",
            "required_work": [],
            "dispatch_state": "returned",
            "delivery_channels": channels,
        },
    )
    criterion = goal_view(run, [], [], []).criteria[0]
    assert criterion.status == "failed" and criterion.validation_level == "V0"


@pytest.mark.parametrize("bad_proof", ["level", "execution", "effect", "references"])
def test_pure_aggregator_requires_consistent_evidence(bad_proof: str) -> None:
    plan_id, run_id = uuid7(), uuid7()
    data: dict[str, object] = {
        "execution_status": "succeeded",
        "side_effect_state": "submitted",
        "validation_status": "passed",
        "validation_level": "V1",
        "retry_class": "never",
        "evidence_refs": [{"kind": "action_step", "source_id": str(uuid7())}],
    }
    key, value = {
        "level": ("validation_level", "V0"),
        "execution": ("execution_status", "failed"),
        "effect": ("side_effect_state", "unknown"),
        "references": ("evidence_refs", []),
    }[bad_proof]
    data[key] = value
    goal = evaluate_criteria(
        RunCriteriaSnapshot(
            run_id, "succeeded", "reply_committed", (WorkRequirement("action_plan", plan_id),)
        ),
        (PlanCriteriaSnapshot(plan_id, "completed", None),),
        (StepCriteriaSnapshot(plan_id, "completed", ExecutionOutcome.model_validate(data)),),
        (),
    )
    assert goal.status == "inconclusive" and goal.criteria[-1].validation_level == "V0"


def test_unknown_required_kind_remains_inconclusive() -> None:
    identifier = uuid7()
    run = TaskRunRecord(
        id=uuid7(),
        status="succeeded",
        contract={
            "required_work": [{"kind": "future_business_proof", "id": str(identifier)}],
        },
    )
    criterion = goal_view(run, [], [], []).criteria[0]
    assert criterion.source_id == identifier and criterion.reason_code == "criterion_not_supported"


def test_legacy_job_association_keeps_legacy_scope_and_unverified_result() -> None:
    run = TaskRunRecord(id=uuid7(), status="succeeded", contract={})
    goal = goal_view(run, [], [], [JobRecord(id=uuid7(), status="succeeded", kind="deleg.test")])
    assert goal.scope == "legacy_associations" and goal.status == "inconclusive"


@pytest.mark.parametrize("contract", [[], "bad", None])
def test_invalid_contract_shape_remains_inconclusive(contract: object) -> None:
    run = TaskRunRecord(id=uuid7(), status="succeeded", contract=contract)
    goal = goal_view(run, [], [], [])
    assert goal.scope == "declared_run_contract" and goal.status == "inconclusive"
    assert goal.criteria[0].reason_code == "criterion_invalid"


@pytest.mark.parametrize("criterion", ["audio_frames_sent", "voice_reply_sent"])
@pytest.mark.parametrize(
    ("status", "result", "expected"),
    [
        ("running", "matched", "pending"),
        ("succeeded", "matched", "passed"),
        ("succeeded", None, "inconclusive"),
        ("succeeded", "other", "inconclusive"),
        ("failed", "audio_delivery_incomplete", "failed"),
        ("failed", "expired_delivery_unknown", "inconclusive"),
        ("cancelled", "matched", "inconclusive"),
        ("cancelled", None, "failed"),
    ],
)
def test_voice_goal_requires_matching_transport_evidence(
    criterion: str, status: str, result: str | None, expected: str
) -> None:
    run = TaskRunRecord(
        id=uuid7(),
        status=status,
        contract={
            "criterion": criterion,
            "required_work": [],
            "delivery_result": criterion if result == "matched" else result,
        },
    )
    goal = goal_view(run, [], [], [])
    assert goal.status == expected
    assert goal.criteria[0].validation_level == ("V1" if expected == "passed" else "V0")
