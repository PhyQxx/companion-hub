from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update

from app.db import AssetDerivationRecord, AssetRecord, AssetReferenceRecord, Database
from app.ids import uuid7

logger = logging.getLogger("app.jobs.assets")

AssetState = str  # staging / active / unreferenced / deleting / deleted / quarantined


@dataclass(frozen=True, slots=True)
class AssetView:
    id: UUID
    content_hash: str
    byte_size: int
    media_type: str
    privacy_level: str
    storage_key: str
    state: AssetState
    created_at: datetime
    verified_at: datetime | None


class AssetStore:
    """内容寻址资产存储。

    v1 使用本地文件系统：
    - 写入临时文件 → fsync → 校验 hash → 原子移动到内容地址路径
    - 数据库保存元数据、引用和派生关系
    """

    def __init__(self, database: Database, root_dir: Path | str) -> None:
        self._database = database
        self._root = Path(root_dir)
        self._staging = self._root / "staging"
        self._objects = self._root / "objects"
        self._staging.mkdir(parents=True, exist_ok=True)
        self._objects.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 写入与提交
    # ------------------------------------------------------------------ #

    async def store(
        self,
        data: bytes,
        *,
        media_type: str,
        privacy_level: str = "L2",
        encryption_key_ref: str | None = None,
    ) -> AssetView:
        """存储资产，返回元数据。"""
        content_hash = hashlib.sha256(data).hexdigest()
        byte_size = len(data)

        # 检查是否已存在相同 hash
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(AssetRecord).where(AssetRecord.content_hash == content_hash)
            )
            if existing is not None:
                return self._to_view(existing)

        # 写入临时文件
        temp_path = self._staging / f"{content_hash}.tmp"
        try:
            with open(temp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            logger.exception("failed to write staging file")
            raise

        # 计算存储路径
        storage_key = self._storage_key(content_hash)
        dest_path = self._objects / storage_key
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # 原子移动
        shutil.move(str(temp_path), str(dest_path))

        now = datetime.now(UTC)
        asset_id = uuid7()
        record = AssetRecord(
            id=asset_id,
            content_hash=content_hash,
            byte_size=byte_size,
            media_type=media_type,
            privacy_level=privacy_level,
            storage_key=storage_key,
            state="staging",
            encryption_key_ref=encryption_key_ref,
            created_at=now,
            verified_at=now,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return self._to_view(record)

    async def commit(self, asset_id: UUID) -> bool:
        """将 staging 资产提交为 active。"""
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(AssetRecord)
                .where(AssetRecord.id == asset_id, AssetRecord.state == "staging")
                .values(state="active")
            )
            return result.rowcount > 0

    # ------------------------------------------------------------------ #
    # 引用管理
    # ------------------------------------------------------------------ #

    async def add_reference(
        self,
        asset_id: UUID,
        owner_kind: str,
        owner_id: str,
        role: str,
    ) -> bool:
        async with self._database.sessions.begin() as session:
            existing = await session.scalar(
                select(AssetReferenceRecord).where(
                    AssetReferenceRecord.asset_id == asset_id,
                    AssetReferenceRecord.owner_kind == owner_kind,
                    AssetReferenceRecord.owner_id == owner_id,
                    AssetReferenceRecord.role == role,
                )
            )
            if existing is not None:
                return False
            session.add(
                AssetReferenceRecord(
                    asset_id=asset_id,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    role=role,
                )
            )
            # 若资产处于 unreferenced，重新激活
            await session.execute(
                update(AssetRecord)
                .where(AssetRecord.id == asset_id, AssetRecord.state == "unreferenced")
                .values(state="active")
            )
            return True

    async def remove_reference(
        self,
        asset_id: UUID,
        owner_kind: str,
        owner_id: str,
        role: str,
    ) -> bool:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(AssetReferenceRecord).where(
                    AssetReferenceRecord.asset_id == asset_id,
                    AssetReferenceRecord.owner_kind == owner_kind,
                    AssetReferenceRecord.owner_id == owner_id,
                    AssetReferenceRecord.role == role,
                )
            )
            if result.rowcount == 0:
                return False
            # 检查是否还有引用
            count = await session.scalar(
                select(func.count())
                .select_from(AssetReferenceRecord)
                .where(AssetReferenceRecord.asset_id == asset_id)
            )
            if count == 0:
                await session.execute(
                    update(AssetRecord)
                    .where(AssetRecord.id == asset_id, AssetRecord.state == "active")
                    .values(state="unreferenced")
                )
            return True

    # ------------------------------------------------------------------ #
    # 读取与查询
    # ------------------------------------------------------------------ #

    async def get(self, asset_id: UUID) -> AssetView | None:
        async with self._database.sessions() as session:
            record = await session.get(AssetRecord, asset_id)
            return self._to_view(record) if record is not None else None

    async def get_by_hash(self, content_hash: str) -> AssetView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(AssetRecord).where(AssetRecord.content_hash == content_hash)
            )
            return self._to_view(record) if record is not None else None

    async def read_bytes(self, asset_id: UUID) -> bytes | None:
        """读取资产原始字节。"""
        async with self._database.sessions() as session:
            record = await session.get(AssetRecord, asset_id)
            if record is None:
                return None
        path = self._objects / record.storage_key
        if not path.exists():
            return None
        return path.read_bytes()

    async def list_references(
        self, asset_id: UUID
    ) -> list[dict[str, str]]:
        async with self._database.sessions() as session:
            rows = await session.scalars(
                select(AssetReferenceRecord).where(AssetReferenceRecord.asset_id == asset_id)
            )
            return [
                {
                    "owner_kind": r.owner_kind,
                    "owner_id": r.owner_id,
                    "role": r.role,
                }
                for r in rows.all()
            ]

    # ------------------------------------------------------------------ #
    # 派生关系
    # ------------------------------------------------------------------ #

    async def add_derivation(
        self,
        parent_asset_id: UUID,
        child_asset_id: UUID,
        operation: str,
        *,
        model_version: str | None = None,
        params_hash: str | None = None,
    ) -> None:
        async with self._database.sessions.begin() as session:
            existing = await session.scalar(
                select(AssetDerivationRecord).where(
                    AssetDerivationRecord.parent_asset_id == parent_asset_id,
                    AssetDerivationRecord.child_asset_id == child_asset_id,
                    AssetDerivationRecord.operation == operation,
                )
            )
            if existing is not None:
                return
            session.add(
                AssetDerivationRecord(
                    parent_asset_id=parent_asset_id,
                    child_asset_id=child_asset_id,
                    operation=operation,
                    model_version=model_version,
                    params_hash=params_hash,
                )
            )

    # ------------------------------------------------------------------ #
    # 清理（GC）
    # ------------------------------------------------------------------ #

    async def gc_staging(self, max_age_hours: float = 24.0) -> int:
        """清理超过指定时间的 staging 资产。"""
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        async with self._database.sessions.begin() as session:
            records = list(
                await session.scalars(
                    select(AssetRecord).where(
                        AssetRecord.state == "staging",
                        AssetRecord.created_at < cutoff,
                    )
                )
            )
            for record in records:
                self._delete_file(record.storage_key)
                await session.delete(record)
            return len(records)

    async def gc_unreferenced(self, grace_days: float = 7.0) -> int:
        """清理超过宽限期的未引用资产。"""
        cutoff = datetime.now(UTC) - timedelta(days=grace_days)
        async with self._database.sessions.begin() as session:
            records = list(
                await session.scalars(
                    select(AssetRecord).where(
                        AssetRecord.state == "unreferenced",
                        AssetRecord.created_at < cutoff,
                    )
                )
            )
            for record in records:
                self._delete_file(record.storage_key)
                record.state = "deleted"
            return len(records)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _storage_key(content_hash: str) -> str:
        return f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"

    def _delete_file(self, storage_key: str) -> None:
        path = self._objects / storage_key
        try:
            if path.exists():
                path.unlink()
                # 清理空目录
                for parent in [path.parent, path.parent.parent]:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
        except Exception:
            logger.exception("failed to delete asset file: %s", storage_key)

    @staticmethod
    def _to_view(record: AssetRecord) -> AssetView:
        return AssetView(
            id=record.id,  # type: ignore[arg-type]
            content_hash=record.content_hash,
            byte_size=record.byte_size,
            media_type=record.media_type,
            privacy_level=record.privacy_level,
            storage_key=record.storage_key,
            state=record.state,
            created_at=record.created_at,
            verified_at=record.verified_at,
        )
