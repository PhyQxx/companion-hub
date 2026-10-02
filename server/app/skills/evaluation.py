"""Replay a declared synthetic request corpus without an executor or network port."""

import hashlib
import json
from typing import Any

from app.schemas.evaluation import FixtureCase, FixtureEvaluationRequest

from .models import SkillDocument
from .requests import prepare_request

VALIDATOR_VERSION = "skill-request-v1"


def document_hash(document: SkillDocument) -> str:
    return hashlib.sha256(document.model_dump_json().encode()).hexdigest()


def _passes(document: SkillDocument | None, case: FixtureCase) -> bool:
    operation = (
        next((item for item in document.api.operations if item.name == case.operation), None)
        if document and document.api
        else None
    )
    if operation is None:
        return case.expected_reason == "operation_missing"
    try:
        request = prepare_request(operation, case.arguments)
    except ValueError as error:
        return case.expected_reason == str(error)
    return case.expected == {
        "method": operation.method,
        "path": request.path,
        "query": request.query,
        "body": request.body,
    }


def evaluate_fixture(
    baseline: SkillDocument | None,
    candidate: SkillDocument,
    corpus: FixtureEvaluationRequest,
    *,
    base_version: int | None,
) -> dict[str, Any]:
    checks = [
        {
            "case_id": case.id,
            "before": _passes(baseline, case) if baseline else None,
            "after": _passes(candidate, case),
        }
        for case in corpus.cases
    ]
    security_passed = True
    if baseline is not None:
        if baseline.api is None or candidate.api is None:
            security_passed = baseline.api == candidate.api
        else:
            security_passed = (
                baseline.api.connection == candidate.api.connection
                and baseline.api.auth == candidate.api.auth
                and all(
                    any(
                        new.name == old.name and new.risk == old.risk and new.method == old.method
                        for new in candidate.api.operations
                    )
                    for old in baseline.api.operations
                )
            )
    regressed = any(check["before"] is True and check["after"] is False for check in checks)
    passed = bool(checks) and all(check["after"] for check in checks) and security_passed
    outcome = (
        "regressed"
        if regressed or not security_passed
        else "improved"
        if passed and any(check["before"] is False for check in checks)
        else "unchanged"
        if passed
        else "inconclusive"
    )
    return {
        "mode": "fixture_replay",
        "validator_version": VALIDATOR_VERSION,
        "validation_level": "V2" if passed else "V0",
        "status": "passed"
        if passed
        else "failed"
        if checks or not security_passed
        else "untestable",
        "base_version": base_version,
        "baseline_hash": document_hash(baseline) if baseline else None,
        "candidate_hash": document_hash(candidate),
        "corpus_hash": hashlib.sha256(
            json.dumps(corpus.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest(),
        "case_count": len(checks),
        "safety_passed": security_passed,
        "outcome": outcome,
        "checks": checks,
        "semantic_validation": "unverified",
    }
