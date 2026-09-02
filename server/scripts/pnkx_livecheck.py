"""pnkx TODO-01 真实联调脚本：真实 API + 本地 SQLite，验证全链契约。

用法：cd server && uv run python scripts/pnkx_livecheck.py
覆盖：令牌鉴权、分页拉取、镜像建立、新建推送（含绑定身份核对）、
完成推送、远端清理。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.db import Base, create_database
from app.tasks.models import TaskKind, TaskTrigger
from app.tasks.store import TaskStore
from app.todo import PnkxTodoClient, TodoSyncService

BASE_URL = "https://admin.pnkx.top:8/prod-api"
TOKEN = "REMOVED_FROM_HISTORY"
DB_PATH = Path("/tmp/aria_pnkx_livecheck.db")


async def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    database = create_database(f"sqlite+aiosqlite:///{DB_PATH}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    raw = httpx.AsyncClient()
    client = PnkxTodoClient(base_url=BASE_URL, integration_token=TOKEN)
    service = TodoSyncService(database, client)

    from sqlalchemy import select

    from app.db import AppUserRecord, TaskItemRecord
    from app.ids import uuid7

    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=uuid7(), display_name="联调", status="active"))

    print("== 1. 全量拉取与镜像建立")
    stats = await service.sync_once()
    from dataclasses import asdict

    print(f"   stats: {json.dumps(asdict(stats), ensure_ascii=False, default=str)}")
    async with database.sessions() as session:
        mirrors = (await session.scalars(select(TaskItemRecord))).all()
    active = [m for m in mirrors if m.status == "active"]
    print(f"   本地镜像总数: {len(mirrors)}（active {len(active)}）")
    sample = sorted(active, key=lambda m: m.created_at or datetime.now(UTC))[:3]
    for m in sample:
        print(
            f"   · [{m.status}] {m.title[:40]!r} priority={m.priority} "
            f"label={m.group_label!r} ref={m.source_ref}"
        )

    print("== 2. 新建推送（验证绑定身份 + clientUuid 幂等）")
    tasks = TaskStore(database)
    local = await tasks.create(
        user_id=mirrors[0].user_id,
        kind=TaskKind.TASK,
        title="Aria 联调测试任务（可删除）",
        trigger=TaskTrigger(type="time", at=datetime.now(UTC) + timedelta(days=1)),
    )
    stats2 = await service.sync_once()
    print(f"   stats: {json.dumps(asdict(stats2), ensure_ascii=False, default=str)}")
    listing = await raw.get(
        f"{BASE_URL}/admin/toDo/list",
        params={"pageNum": 1, "pageSize": 200},
        headers={"X-Integration-Token": TOKEN},
    )
    created = [r for r in listing.json()["rows"] if r.get("clientUuid") == f"aria:{local.id}"]
    assert created, "远端未找到推送的任务"
    remote = created[0]
    print(
        f"   远端 id={remote['id']} createBy={remote['createBy']!r} content={remote['content']!r}"
    )

    print("== 3. 完成推送")
    await tasks.complete_task(local.user_id, local.id)
    stats3 = await service.sync_once()
    print(f"   stats: completions_pushed={stats3.completions_pushed}")
    recheck = await raw.get(
        f"{BASE_URL}/admin/toDo/{remote['id']}",
        headers={"X-Integration-Token": TOKEN},
    ).json()
    remote_after = recheck.get("data") or {}
    print(
        f"   远端 status={remote_after.get('status')} finishTime={remote_after.get('finishTime')!r}"
    )

    print("== 4. 清理远端测试任务")
    deleted = await raw.delete(
        f"{BASE_URL}/admin/toDo/{remote['id']}",
        headers={"X-Integration-Token": TOKEN},
    ).json()
    print(f"   delete: {deleted.get('code')} {deleted.get('msg')!r}")

    await client.close()
    await raw.aclose()
    await database.close()
    print("== 联调完成")


if __name__ == "__main__":
    asyncio.run(main())
