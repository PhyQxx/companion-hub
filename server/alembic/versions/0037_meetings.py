"""MEET-01 authorized meeting transcript and summary.

Revision ID: 0037_meetings
Revises: 0036_home_scenes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0037_meetings"
down_revision: str | None = "0036_home_scenes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "meeting",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_event_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("briefing", sa.JSON(), nullable=False),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transcript_segments", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("action_items", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared','recording','consent_revoked','completed','cancelled')",
            name="ck_meeting_status",
        ),
        sa.CheckConstraint(
            "privacy_level IN ('L1','L2')",
            name="ck_meeting_privacy_level",
        ),
        sa.ForeignKeyConstraint(["calendar_event_id"], ["calendar_event.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_user_created", "meeting", ["user_id", "created_at"])
    op.create_table(
        "meeting_action_claim",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('creating','created','unknown_outcome')",
            name="ck_meeting_action_claim_status",
        ),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task_item.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("meeting_id", "action_index", name="uq_meeting_action_claim"),
    )


def downgrade() -> None:
    op.drop_table("meeting_action_claim")
    op.drop_index("ix_meeting_user_created", table_name="meeting")
    op.drop_table("meeting")
