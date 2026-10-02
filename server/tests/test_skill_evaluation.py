from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.db import Base, Database, create_database
from app.schemas.evaluation import FixtureCase, FixtureEvaluationRequest
from app.skills.evaluation import evaluate_fixture
from app.skills.generator import SkillProposal
from app.skills.models import SkillApiManifest, SkillDocument, SkillOperation, SkillParameter
from app.skills.store import SkillStore


def document(path: str = "/items/{itemId}", connection: str = "example") -> SkillDocument:
    return SkillDocument(
        name="fixture-items",
        description="Synthetic fixture capability",
        instructions="Read items",
        api=SkillApiManifest(
            schema_version=1,
            connection=connection,
            operations=[
                SkillOperation(
                    name="get",
                    description="Read",
                    method="GET",
                    path=path,
                    risk="read",
                    parameters={
                        "itemId": SkillParameter(type="string", required=True, location="path"),
                        "enabled": SkillParameter(type="boolean"),
                    },
                )
            ],
        ),
    )


def corpus(path: str = "/items/synthetic_42") -> FixtureEvaluationRequest:
    return FixtureEvaluationRequest(
        data_class="synthetic",
        cases=[
            FixtureCase(
                id="request",
                operation="get",
                arguments={"itemId": "synthetic_42", "enabled": True},
                expected={"method": "GET", "path": path, "query": {"enabled": "true"}, "body": {}},
            ),
            FixtureCase(
                id="missing",
                operation="get",
                arguments={},
                expected_reason="skill_arguments_invalid",
            ),
            FixtureCase(
                id="traversal",
                operation="get",
                arguments={"itemId": "../secret"},
                expected_reason="invalid_path_parameter",
            ),
        ],
    )


def test_fixture_compares_same_cases_and_records_no_parameters() -> None:
    result = evaluate_fixture(document("/old/{itemId}"), document(), corpus(), base_version=1)
    assert result["outcome"] == "improved" and result["status"] == "passed"
    assert result["validation_level"] == "V2" and result["semantic_validation"] == "unverified"
    assert result["case_count"] == 3 and result["checks"][0] == {
        "case_id": "request",
        "before": False,
        "after": True,
    }
    assert "synthetic_42" not in json.dumps(result) and "secret" not in json.dumps(result)
    assert result["baseline_hash"] != result["candidate_hash"]


def test_fixture_detects_regression_and_security_change() -> None:
    regressed = evaluate_fixture(document(), document("/old/{itemId}"), corpus(), base_version=1)
    assert regressed["outcome"] == "regressed" and regressed["status"] == "failed"
    changed = evaluate_fixture(document(), document(connection="other"), corpus(), base_version=1)
    assert changed["outcome"] == "regressed" and not changed["safety_passed"]
    assert changed["status"] == "failed"


def test_empty_corpus_is_untestable_and_invalid_corpora_rejected() -> None:
    result = evaluate_fixture(
        None,
        document(),
        FixtureEvaluationRequest(data_class="synthetic", cases=[]),
        base_version=None,
    )
    assert result["status"] == "untestable" and result["validation_level"] == "V0"
    with pytest.raises(ValidationError):
        FixtureEvaluationRequest.model_validate({"data_class": "production", "cases": []})
    with pytest.raises(ValidationError):
        FixtureEvaluationRequest(
            data_class="synthetic", cases=[corpus().cases[0], corpus().cases[0]]
        )
    with pytest.raises(ValidationError):
        FixtureCase(id="no-expectation", operation="get")


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'fixture.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def test_fixture_is_persisted_independently_of_probe_and_gates_review(
    database: Database,
) -> None:
    store = SkillStore(database)
    baseline = await store.create(document(), source="uploaded")
    draft = await store.save_draft(
        SkillProposal(document=document("/old/{itemId}"), warnings=[], evidence=[]),
        system_name="example",
        source="learning",
        target_skill_id=baseline.id,
        base_version=1,
    )
    assert draft is not None
    evaluated = await store.evaluate_draft(draft.id, corpus())
    assert evaluated.verify_status is None and evaluated.verification_report is not None
    assert evaluated.verification_report["fixture_replay"]["status"] == "failed"  # type: ignore[index]
    with pytest.raises(ValueError, match="skill_fixture_failed"):
        await store.approve_draft(draft.id)
    probe = await store.mark_draft_verified(
        draft.id, ok=True, report={"scope": "changed_contracts"}
    )
    assert probe.verification_report is not None and "fixture_replay" in probe.verification_report
    passed = await store.evaluate_draft(draft.id, corpus("/old/synthetic_42"))
    assert (
        passed.verification_report is not None
        and passed.verification_report["scope"] == "changed_contracts"
    )
    updated = await store.approve_draft(draft.id)
    assert updated.version == 2 and not updated.enabled
    with pytest.raises(ValueError, match="skill_draft_already_reviewed"):
        await store.approve_draft(draft.id)


async def test_changed_baseline_cannot_reuse_fixture_report(database: Database) -> None:
    store = SkillStore(database)
    baseline = await store.create(document(), source="uploaded")
    draft = await store.save_draft(
        SkillProposal(document=document("/old/{itemId}"), warnings=[], evidence=[]),
        system_name="example",
        source="learning",
        target_skill_id=baseline.id,
        base_version=1,
    )
    assert draft is not None
    await store.evaluate_draft(draft.id, corpus("/old/synthetic_42"))
    await store.revise(baseline.id, document(), base_version=1)
    with pytest.raises(ValueError, match="skill_version_changed"):
        await store.approve_draft(draft.id)
    with pytest.raises(ValueError, match="skill_version_changed"):
        await store.evaluate_draft(draft.id, corpus())
