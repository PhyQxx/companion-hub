from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from app.db import AppUserRecord, Base, Database, create_database
from app.schemas import PrivacyLevel
from app.skills.drafts import SkillDraftReviewTool
from app.skills.generator import SkillProposal
from app.skills.models import SkillDocument
from app.skills.store import SkillDraftView, SkillStore, SkillView
from app.tools.contracts import ToolContext


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value


async def _add_user(database: Database, *, role: str) -> AppUserRecord:
    async with database.sessions() as session:
        user = AppUserRecord(id=uuid4(), display_name=f"user-{role}", role=role)
        session.add(user)
        await session.commit()
        return user


def _proposal(name: str = "company-info-lookup", *, variant: int = 0) -> SkillProposal:
    return SkillProposal(
        document=SkillDocument(
            name=name,
            description="演示技能",
            instructions=f"按公开企业信息查询渠道核对（variant {variant}）",
        ),
        warnings=["w1"],
        evidence=[],
        executable=False,
    )


def _tool(
    store: SkillStore, database: Database, *, callback: object = None
) -> SkillDraftReviewTool:
    return SkillDraftReviewTool(store, database, on_skills_changed=callback)  # type: ignore[arg-type]


def _context(user: AppUserRecord, level: PrivacyLevel = PrivacyLevel.L1) -> ToolContext:
    return ToolContext(privacy_level=level, user_id=user.id, turn_id=uuid4())


async def _draft(
    store: SkillStore, name: str = "company-info-lookup", *, variant: int = 0
) -> SkillDraftView:
    draft = await store.save_draft(
        _proposal(name, variant=variant), system_name="demo-sys", source="research"
    )
    assert draft is not None
    return draft


@pytest.mark.asyncio
async def test_owner_lists_approves_and_enables(database: Database) -> None:
    store = SkillStore(database)
    owner = await _add_user(database, role="owner")
    await _draft(store)
    synced: list[SkillView] = []

    async def callback(skill: SkillView) -> None:
        synced.append(skill)

    tool = _tool(store, database, callback=callback)
    listing = await tool.execute(
        tool.arguments_model.model_validate({"action": "list"}), _context(owner)
    )
    assert listing.ok and listing.data["count"] == 1
    assert listing.data["drafts"][0]["skill_name"] == "company-info-lookup"
    assert listing.data["drafts"][0]["source"] == "research"

    approved = await tool.execute(
        tool.arguments_model.model_validate(
            {"action": "approve", "skill_name": "company-info-lookup", "enable": True}
        ),
        _context(owner),
    )
    assert approved.ok
    assert approved.data["skill_name"] == "company-info-lookup"
    assert approved.data["enabled"] is True
    assert synced and synced[0].enabled is True
    skill = await store.get_by_name("company-info-lookup")
    assert skill is not None and skill.enabled


@pytest.mark.asyncio
async def test_member_and_missing_user_rejected(database: Database) -> None:
    store = SkillStore(database)
    member = await _add_user(database, role="member")
    await _draft(store)
    tool = _tool(store, database)

    rejected = await tool.execute(
        tool.arguments_model.model_validate({"action": "list"}), _context(member)
    )
    assert not rejected.ok and rejected.reason_code == "owner_required"

    stranger = ToolContext(privacy_level=PrivacyLevel.L1, user_id=uuid4(), turn_id=uuid4())
    missing = await tool.execute(tool.arguments_model.model_validate({"action": "list"}), stranger)
    assert not missing.ok and missing.reason_code == "owner_required"


@pytest.mark.asyncio
async def test_approve_by_draft_id_and_dismiss(database: Database) -> None:
    store = SkillStore(database)
    owner = await _add_user(database, role="owner")
    draft = await _draft(store)
    tool = _tool(store, database)

    approved = await tool.execute(
        tool.arguments_model.model_validate(
            {"action": "approve", "draft_id": str(draft.id), "enable": False}
        ),
        _context(owner),
    )
    assert approved.ok and approved.data["enabled"] is False
    skill = await store.get_by_name("company-info-lookup")
    assert skill is not None and not skill.enabled

    # 已审阅的草稿不可再审
    again = await tool.execute(
        tool.arguments_model.model_validate({"action": "approve", "draft_id": str(draft.id)}),
        _context(owner),
    )
    assert not again.ok and again.reason_code == "draft_not_found"

    await _draft(store, name="another-skill")
    dismissed = await tool.execute(
        tool.arguments_model.model_validate({"action": "dismiss", "skill_name": "another-skill"}),
        _context(owner),
    )
    assert dismissed.ok and dismissed.data["status"] == "dismissed"


@pytest.mark.asyncio
async def test_reference_gates(database: Database) -> None:
    store = SkillStore(database)
    owner = await _add_user(database, role="owner")
    await _draft(store, name="dup-skill", variant=1)
    await _draft(store, name="dup-skill", variant=2)
    tool = _tool(store, database)
    context = _context(owner)

    none_ref = await tool.execute(
        tool.arguments_model.model_validate({"action": "approve"}), context
    )
    assert not none_ref.ok and none_ref.reason_code == "draft_reference_required"

    ambiguous = await tool.execute(
        tool.arguments_model.model_validate({"action": "approve", "skill_name": "dup-skill"}),
        context,
    )
    assert not ambiguous.ok and ambiguous.reason_code == "draft_ambiguous"

    not_found = await tool.execute(
        tool.arguments_model.model_validate({"action": "approve", "skill_name": "missing"}),
        context,
    )
    assert not not_found.ok and not_found.reason_code == "draft_not_found"

    bad_id = await tool.execute(
        tool.arguments_model.model_validate({"action": "approve", "draft_id": "not-a-uuid"}),
        context,
    )
    assert not bad_id.ok and bad_id.reason_code == "invalid_draft_id"

    l2 = await tool.execute(
        tool.arguments_model.model_validate({"action": "list"}),
        _context(owner, level=PrivacyLevel.L2),
    )
    assert not l2.ok and l2.reason_code == "review_requires_l1"
