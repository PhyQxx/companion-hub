"""Resolve goal visibility from owned source evidence, never from goal titles."""

from sqlalchemy import String, and_, false, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.sql.elements import ColumnElement

from app.db import CognitiveGoalRecord, ConversationRecord, DeletionLedgerRecord, MessageRecord
from app.schemas.common import PrivacyLevel


def goal_visibility(privacy: PrivacyLevel | None) -> ColumnElement[bool]:
    if privacy is None:
        return ~false()  # Owner-facing queries retain private goals.
    if privacy == PrivacyLevel.L3:
        return false()
    levels = {PrivacyLevel.L0.value}
    if privacy in {PrivacyLevel.L1, PrivacyLevel.L2}:
        levels.add(PrivacyLevel.L1.value)
    if privacy == PrivacyLevel.L2:
        levels.add(PrivacyLevel.L2.value)
    # PostgreSQL renders UUID with hyphens; SQLite stores its hex form.
    source_id = func.lower(func.replace(CognitiveGoalRecord.source_id, "-", ""))
    message_id = func.lower(func.replace(sql_cast(MessageRecord.id, String), "-", ""))
    tombstone_id = func.lower(func.replace(DeletionLedgerRecord.entity_id, "-", ""))
    conversation_id = func.lower(func.replace(sql_cast(ConversationRecord.id, String), "-", ""))
    deleted = (
        select(DeletionLedgerRecord.id)
        .where(
            DeletionLedgerRecord.entity_kind == "message",
            or_(tombstone_id == message_id, tombstone_id == conversation_id),
        )
        .correlate(MessageRecord, ConversationRecord)
        .exists()
    )
    message_visible = (
        select(MessageRecord.id)
        .join(
            ConversationRecord,
            ConversationRecord.id == MessageRecord.conversation_id,
        )
        .where(
            CognitiveGoalRecord.source_kind == "message",
            source_id == message_id,
            ConversationRecord.user_id == CognitiveGoalRecord.user_id,
            MessageRecord.privacy_level.in_(levels),
            ~deleted,
        )
        .exists()
    )
    declared_visible = or_(
        CognitiveGoalRecord.privacy_level.is_(None),
        CognitiveGoalRecord.privacy_level.in_(levels),
    )
    manual_visible = CognitiveGoalRecord.source_kind == "manual"
    if privacy == PrivacyLevel.L0:
        manual_visible = and_(manual_visible, CognitiveGoalRecord.privacy_level == "L0")
    # Unknown origins have no trustworthy inherited privacy classification.
    return and_(declared_visible, or_(message_visible, manual_visible))
