"""Offline trajectory contract replay. No executor, provider, or network port."""

import hashlib
import json
from typing import Any

from app.cognition.action_registry import ActionRegistry, CompiledAction
from app.schemas.evaluation import WorkflowFixtureRequest

from .models import WorkflowStep

VALIDATOR_VERSION = "workflow-contract-v1"


def trajectory_hash(steps: list[WorkflowStep]) -> str:
    return _hash([step.model_dump(mode="json") for step in steps])


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def compile_steps(
    registry: ActionRegistry, steps: list[WorkflowStep]
) -> list[CompiledAction] | None:
    try:
        if not 1 <= len(steps) <= 10:
            return None
        return [registry.compile(step.action_id, dict(step.arguments)) for step in steps]
    except (ValueError, PermissionError, LookupError):
        return None


def compiled_hash(registry: ActionRegistry, steps: list[WorkflowStep]) -> str:
    compiled = compile_steps(registry, steps)
    return _hash([item.model_dump(mode="json") for item in compiled] if compiled else None)


def evaluate_fixture(
    registry: ActionRegistry,
    baseline: list[WorkflowStep] | None,
    candidate: list[WorkflowStep],
    corpus: WorkflowFixtureRequest,
    *,
    recorded_contracts: list[dict[str, str]] | None,
) -> dict[str, Any]:
    before = compile_steps(registry, baseline) if baseline else None
    after = compile_steps(registry, candidate)

    def request(actions: list[CompiledAction] | None) -> list[dict[str, Any]] | None:
        if actions is None:
            return None
        return [
            {"tool_name": item.definition.tool_name, "arguments": item.tool_arguments}
            for item in actions
        ]

    checks = [
        {
            "case_id": case.id,
            "before": (
                request(before) == case.expected
                if before
                else case.expected_reason == "workflow_compile_failed"
            )
            if baseline
            else None,
            "after": request(after) == case.expected
            if after
            else case.expected_reason == "workflow_compile_failed",
        }
        for case in corpus.cases
    ]
    # A learned trajectory cannot expand permissions or silently switch tools.
    contracts = [
        {
            "action_id": item.definition.action_id,
            "tool_name": item.definition.tool_name,
            "risk": str(item.definition.risk),
            "confirmation_policy": str(item.definition.confirmation_policy),
        }
        for item in after or []
    ]
    safety = bool(after) and all(str(item.definition.risk) in {"A0", "A1"} for item in after or [])
    if recorded_contracts is not None:
        safety = safety and contracts == recorded_contracts
    passed = bool(checks) and safety and all(check["after"] for check in checks)
    regressed = any(check["before"] is True and check["after"] is False for check in checks)
    return {
        "mode": "fixture_replay",
        "validator_version": VALIDATOR_VERSION,
        "validation_level": "V2" if passed else "V0",
        "status": "passed" if passed else "failed" if checks or not safety else "untestable",
        "outcome": "regressed"
        if regressed or not safety
        else "improved"
        if passed and any(check["before"] is False for check in checks)
        else "unchanged"
        if passed
        else "inconclusive",
        "candidate_hash": trajectory_hash(candidate),
        "baseline_hash": trajectory_hash(baseline) if baseline else None,
        "compiled_hash": compiled_hash(registry, candidate),
        "corpus_hash": _hash(corpus.model_dump(mode="json")),
        "case_count": len(checks),
        "safety_passed": safety,
        "checks": checks,
        "semantic_validation": "unverified",
    }
