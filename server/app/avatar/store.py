from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from app.db import (
    AvatarInstanceRecord,
    AvatarPackRecord,
    Database,
    PersonaAvatarBindingRecord,
    PersonaVersionRecord,
)
from app.ids import uuid7

logger = logging.getLogger("app.avatar.store")


@dataclass(frozen=True, slots=True)
class AvatarPackView:
    id: str
    name: str
    archetype: str
    engine: str
    manifest: dict[str, Any]
    content_hash: str
    license: dict[str, Any]
    built_in: bool
    installed_at: datetime


@dataclass(frozen=True, slots=True)
class AvatarInstanceView:
    id: UUID
    owner: str
    name: str
    pack_id: str
    customization: dict[str, Any]
    voice_profile_id: str | None
    theme_id: UUID | None
    version: int
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AvatarBindingView:
    persona_id: int
    avatar: AvatarInstanceView
    is_default: bool
    created_at: datetime


class AvatarStore:
    """伴侣形象存储。

    v1 提供形象包管理、实例创建和人格绑定。
    不包含 Live2D/VRM 运行时加载(由前端/桌面端处理)。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ #
    # 内置种子包
    # ------------------------------------------------------------------ #

    async def load_builtin_packs(self) -> int:
        """将内置种子包写入数据库(幂等)。返回新增数量。"""
        builtins: list[dict[str, Any]] = [
            {
                "id": "warm-daily",
                "name": "Aria 日常",
                "archetype": "warm-daily",
                "engine": "static",
                "manifest": {
                    "emotions": ["neutral", "happy", "tender", "concerned", "sad", "sleepy"],
                    "gestures": ["nod", "wave", "lean_in", "look_aside", "comfort"],
                    "outfits": ["daily", "relaxed", "outing"],
                    "touch_regions": ["head", "hand", "accessory"],
                    "supports_viseme": False,
                    "fallbacks": {"comfort": "nod", "hand_heart": "tender"},
                    "customizable_slots": [],
                    "assets": {
                        "thumbnail": "/api/v1/avatar-assets/warm-daily/neutral.png",
                        "emotions": {
                            "neutral": "/api/v1/avatar-assets/warm-daily/neutral.png",
                            "happy": "/api/v1/avatar-assets/warm-daily/happy.png",
                            "tender": "/api/v1/avatar-assets/warm-daily/happy.png",
                            "concerned": "/api/v1/avatar-assets/warm-daily/concerned.png",
                            "sad": "/api/v1/avatar-assets/warm-daily/concerned.png",
                        },
                    },
                },
                "content_hash": "builtin-warm-daily-v2",
                "license": {
                    "author": "Aria Project",
                    "usage": "personal",
                    "attribution": "Aria Companion Hub",
                    "generated_with": "OpenAI built-in image generation",
                    "asset_hashes": {
                        "neutral": (
                            "sha256:da092d7f72466884a6ec7a72f90c8c5edc9ca793ba12df00ac4932d0bded60c3"
                        ),
                        "happy": (
                            "sha256:8c33ccc0ebcefb4fbe041d6947961a1a19744e4d69e3d3f35d2e9a9ddaa8b089"
                        ),
                        "concerned": (
                            "sha256:11f3accd0dfcf97fb7ee47811d204e927e9b8cdb1dd9a83a927328f7cdf67f6f"
                        ),
                    },
                },
            },
            {
                "id": "light-core",
                "name": "光核助手",
                "archetype": "light-core",
                "engine": "abstract",
                "manifest": {
                    "emotions": ["neutral", "happy", "concerned", "surprised", "sleepy"],
                    "gestures": ["pulse", "glow", "dim", "ripple"],
                    "outfits": [],
                    "touch_regions": [],
                    "supports_viseme": False,
                    "fallbacks": {},
                    "customizable_slots": ["palette", "glow_intensity"],
                },
                "content_hash": "builtin-light-core-v1",
                "license": {
                    "author": "Aria Project",
                    "usage": "personal",
                    "attribution": "Aria Companion Hub",
                    "source_hash": "sha256:builtin",
                },
            },
        ]
        count = 0
        async with self._database.sessions.begin() as session:
            for data in builtins:
                existing = await session.get(AvatarPackRecord, data["id"])
                if existing is None:
                    session.add(
                        AvatarPackRecord(
                            id=data["id"],
                            name=data["name"],
                            archetype=data["archetype"],
                            engine=data["engine"],
                            manifest=data["manifest"],
                            content_hash=data["content_hash"],
                            license=data["license"],
                            built_in=True,
                        )
                    )
                    count += 1
                elif existing.built_in and existing.content_hash != data["content_hash"]:
                    existing.name = data["name"]
                    existing.archetype = data["archetype"]
                    existing.engine = data["engine"]
                    existing.manifest = data["manifest"]
                    existing.content_hash = data["content_hash"]
                    existing.license = data["license"]
        return count

    # ------------------------------------------------------------------ #
    # Pack 查询
    # ------------------------------------------------------------------ #

    async def list_packs(self) -> list[AvatarPackView]:
        async with self._database.sessions() as session:
            records = list(await session.scalars(select(AvatarPackRecord)))
            return [self._pack_to_view(r) for r in records]

    async def get_pack(self, pack_id: str) -> AvatarPackView | None:
        async with self._database.sessions() as session:
            record = await session.get(AvatarPackRecord, pack_id)
            return self._pack_to_view(record) if record is not None else None

    async def install_pack(
        self,
        *,
        pack_id: str,
        name: str,
        archetype: str,
        engine: str,
        manifest: dict[str, Any],
        content_hash: str,
        license_info: dict[str, Any],
    ) -> AvatarPackView:
        """Install an already validated local pack, idempotently by content-derived id."""
        if engine not in {"static", "live2d", "vrm", "abstract"}:
            raise ValueError(f"unsupported avatar engine: {engine}")
        if not pack_id or len(pack_id) > 64:
            raise ValueError("avatar pack id must be between 1 and 64 characters")
        async with self._database.sessions.begin() as session:
            record = await session.get(AvatarPackRecord, pack_id)
            if record is None:
                record = AvatarPackRecord(
                    id=pack_id,
                    name=name,
                    archetype=archetype,
                    engine=engine,
                    manifest=manifest,
                    content_hash=content_hash,
                    license=license_info,
                    built_in=False,
                )
                session.add(record)
                await session.flush()
                await session.refresh(record)
            elif record.content_hash != content_hash or record.engine != engine:
                raise ValueError(f"avatar pack id conflict: {pack_id}")
            return self._pack_to_view(record)

    # ------------------------------------------------------------------ #
    # Instance 生命周期
    # ------------------------------------------------------------------ #

    async def create_instance(
        self,
        pack_id: str,
        name: str,
        *,
        owner: str = "local-user",
        customization: dict[str, Any] | None = None,
        voice_profile_id: str | None = None,
        theme_id: UUID | None = None,
    ) -> AvatarInstanceView:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("avatar instance name must not be empty")
        pack = await self.get_pack(pack_id)
        if pack is None:
            raise LookupError(f"avatar pack not found: {pack_id}")
        normalized_customization = customization or {}
        self._validate_customization(pack, normalized_customization)

        instance = AvatarInstanceRecord(
            id=uuid7(),
            owner=owner,
            name=normalized_name,
            pack_id=pack_id,
            customization=normalized_customization,
            voice_profile_id=voice_profile_id,
            theme_id=theme_id,
        )
        async with self._database.sessions.begin() as session:
            session.add(instance)
        return self._instance_to_view(instance)

    async def list_instances(
        self,
        *,
        owner: str | None = None,
        status: str | None = None,
    ) -> list[AvatarInstanceView]:
        async with self._database.sessions() as session:
            stmt = select(AvatarInstanceRecord)
            if owner is not None:
                stmt = stmt.where(AvatarInstanceRecord.owner == owner)
            if status is not None:
                stmt = stmt.where(AvatarInstanceRecord.status == status)
            records = list(await session.scalars(stmt))
            return [self._instance_to_view(r) for r in records]

    async def get_instance(self, instance_id: UUID) -> AvatarInstanceView | None:
        async with self._database.sessions() as session:
            record = await session.get(AvatarInstanceRecord, instance_id)
            return self._instance_to_view(record) if record is not None else None

    async def update_instance(
        self,
        instance_id: UUID,
        *,
        name: str | None = None,
        customization: dict[str, Any] | None = None,
        voice_profile_id: str | None = None,
        theme_id: UUID | None = None,
        status: str | None = None,
        update_voice_profile: bool = False,
        update_theme: bool = False,
    ) -> AvatarInstanceView | None:
        async with self._database.sessions.begin() as session:
            record = await session.get(AvatarInstanceRecord, instance_id)
            if record is None:
                return None
            if name is not None:
                normalized_name = name.strip()
                if not normalized_name:
                    raise ValueError("avatar instance name must not be empty")
                record.name = normalized_name
            if customization is not None:
                pack = await session.get(AvatarPackRecord, record.pack_id)
                if pack is None:
                    raise LookupError(f"avatar pack not found: {record.pack_id}")
                self._validate_customization(self._pack_to_view(pack), customization)
                record.customization = customization
            if update_voice_profile:
                record.voice_profile_id = voice_profile_id
            if update_theme:
                record.theme_id = theme_id
            if status is not None:
                if status not in {"active", "preview", "archived"}:
                    raise ValueError(f"unsupported avatar status: {status}")
                record.status = status
            record.version += 1
            return self._instance_to_view(record)

    async def delete_instance(self, instance_id: UUID) -> bool:
        async with self._database.sessions.begin() as session:
            record = await session.get(AvatarInstanceRecord, instance_id)
            if record is None:
                return False
            current_binding = await session.scalar(
                select(PersonaAvatarBindingRecord).where(
                    PersonaAvatarBindingRecord.avatar_instance_id == instance_id,
                    PersonaAvatarBindingRecord.is_default.is_(True),
                )
            )
            if current_binding is not None:
                raise ValueError("default avatar cannot be deleted; switch the default first")
            await session.delete(record)
            return True

    # ------------------------------------------------------------------ #
    # 人格绑定
    # ------------------------------------------------------------------ #

    async def bind_to_persona(
        self,
        persona_id: int,
        instance_id: UUID,
        *,
        is_default: bool = False,
    ) -> bool:
        async with self._database.sessions.begin() as session:
            persona = await session.get(PersonaVersionRecord, persona_id)
            if persona is None:
                raise LookupError(f"persona version not found: {persona_id}")
            instance = await session.get(AvatarInstanceRecord, instance_id)
            if instance is None:
                raise LookupError(f"avatar instance not found: {instance_id}")
            if is_default:
                await session.execute(
                    update(PersonaAvatarBindingRecord)
                    .where(PersonaAvatarBindingRecord.persona_id == persona_id)
                    .values(is_default=False)
                )
            existing = await session.scalar(
                select(PersonaAvatarBindingRecord).where(
                    PersonaAvatarBindingRecord.persona_id == persona_id,
                    PersonaAvatarBindingRecord.avatar_instance_id == instance_id,
                )
            )
            if existing is not None:
                existing.is_default = is_default or existing.is_default
                return True
            session.add(
                PersonaAvatarBindingRecord(
                    persona_id=persona_id,
                    avatar_instance_id=instance_id,
                    is_default=is_default,
                )
            )
            return True

    async def unbind_from_persona(self, persona_id: int, instance_id: UUID) -> bool:
        async with self._database.sessions.begin() as session:
            record = await session.scalar(
                select(PersonaAvatarBindingRecord).where(
                    PersonaAvatarBindingRecord.persona_id == persona_id,
                    PersonaAvatarBindingRecord.avatar_instance_id == instance_id,
                )
            )
            if record is None:
                return False
            await session.delete(record)
            return True

    async def list_bindings(self, *, persona_id: int | None = None) -> list[AvatarBindingView]:
        async with self._database.sessions() as session:
            stmt = select(PersonaAvatarBindingRecord, AvatarInstanceRecord).join(
                AvatarInstanceRecord,
                AvatarInstanceRecord.id == PersonaAvatarBindingRecord.avatar_instance_id,
            )
            if persona_id is not None:
                stmt = stmt.where(PersonaAvatarBindingRecord.persona_id == persona_id)
            rows = (await session.execute(stmt)).all()
            return [
                AvatarBindingView(
                    persona_id=binding.persona_id,
                    avatar=self._instance_to_view(instance),
                    is_default=binding.is_default,
                    created_at=binding.created_at,
                )
                for binding, instance in rows
            ]

    async def get_default_for_persona(self, persona_id: int) -> AvatarInstanceView | None:
        async with self._database.sessions() as session:
            binding = await session.scalar(
                select(PersonaAvatarBindingRecord).where(
                    PersonaAvatarBindingRecord.persona_id == persona_id,
                    PersonaAvatarBindingRecord.is_default.is_(True),
                )
            )
            if binding is None:
                return None
            record = await session.get(AvatarInstanceRecord, binding.avatar_instance_id)
            return self._instance_to_view(record) if record is not None else None

    async def list_bindings_for_persona(self, persona_id: int) -> list[AvatarInstanceView]:
        async with self._database.sessions() as session:
            bindings = list(
                await session.scalars(
                    select(PersonaAvatarBindingRecord).where(
                        PersonaAvatarBindingRecord.persona_id == persona_id
                    )
                )
            )
            results: list[AvatarInstanceView] = []
            for b in bindings:
                record = await session.get(AvatarInstanceRecord, b.avatar_instance_id)
                if record is not None:
                    results.append(self._instance_to_view(record))
            return results

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_customization(pack: AvatarPackView, customization: dict[str, Any]) -> None:
        allowed = set(pack.manifest.get("customizable_slots", []))
        unsupported = sorted(set(customization) - allowed)
        if unsupported:
            raise ValueError(
                f"unsupported customization slots for {pack.id}: {', '.join(unsupported)}"
            )

    @staticmethod
    def _pack_to_view(record: AvatarPackRecord) -> AvatarPackView:
        return AvatarPackView(
            id=record.id,
            name=record.name,
            archetype=record.archetype,
            engine=record.engine,
            manifest=record.manifest,
            content_hash=record.content_hash,
            license=record.license,
            built_in=record.built_in,
            installed_at=record.installed_at,
        )

    @staticmethod
    def _instance_to_view(record: AvatarInstanceRecord) -> AvatarInstanceView:
        return AvatarInstanceView(
            id=record.id,
            owner=record.owner,
            name=record.name,
            pack_id=record.pack_id,
            customization=record.customization,
            voice_profile_id=record.voice_profile_id,
            theme_id=record.theme_id,
            version=record.version,
            status=record.status,
            created_at=record.created_at,
        )
