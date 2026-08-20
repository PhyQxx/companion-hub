from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command


def _alembic_config(database_path: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "server" / "alembic"))
    config.set_main_option("prepend_sys_path", str(root / "server"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def test_0011_timeline_backfills_existing_sources_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "timeline-migration.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0010_memory_subject_scope")

    user_id = "11" * 16
    conversation_id = "22" * 16
    message_l1 = "33" * 16
    message_l2 = "44" * 16
    turn_id = "55" * 16
    event_id = "66" * 16
    correlation_id = "77" * 16
    occurred_at = "2026-08-18 21:30:00"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO app_user (id, display_name) VALUES (?, ?)",
            (user_id, "Migration User"),
        )
        connection.execute(
            "INSERT INTO conversation (id, user_id, title) VALUES (?, ?, ?)",
            (conversation_id, user_id, "before timeline"),
        )
        connection.execute(
            """
            INSERT INTO message (
                id, conversation_id, turn_id, seq, role, content,
                privacy_level, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_l1,
                conversation_id,
                turn_id,
                1,
                "user",
                "升级前聊过星际穿越",
                "L1",
                occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO message (
                id, conversation_id, turn_id, seq, role, content,
                privacy_level, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_l2,
                conversation_id,
                turn_id,
                2,
                "assistant",
                "这是不应复制到 Timeline 的 L2 正文",
                "L2",
                occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO event (
                event_id, proto_version, schema_ref, correlation_id, user_id,
                source, type, payload, priority, privacy_level,
                occurred_at, received_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                1,
                "aria.input-envelope/1",
                correlation_id,
                user_id,
                "{}",
                "device.power_on",
                "{}",
                "normal",
                "L1",
                occurred_at,
                occurred_at,
                occurred_at,
            ),
        )
        connection.commit()

    command.upgrade(config, "0011_timeline_event")

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT source_type, source_id, actor, event_type, summary, privacy_level
            FROM timeline_event
            ORDER BY source_type, source_id
            """
        ).fetchall()

    assert len(rows) == 3
    by_source = {(row[0], row[1]): row for row in rows}
    l1 = by_source[("message", message_l1)]
    l2 = by_source[("message", message_l2)]
    event = by_source[("event", event_id)]
    assert l1[2] == "user"
    assert l1[3] == "conversation.message"
    assert l1[4] == "升级前聊过星际穿越"
    assert l2[2] == "assistant"
    assert l2[4] == "L2 对话消息"
    assert "不应复制" not in l2[4]
    assert event[2] == "device"
    assert event[3] == "device.power_on"

    command.downgrade(config, "0010_memory_subject_scope")
    with sqlite3.connect(database_path) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='timeline_event'"
        ).fetchone()
    assert table is None
