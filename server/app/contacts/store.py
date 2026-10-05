"""CONTACT-01 联系人存储：CRUD、名称/别名唯一裁决与按名查找。

名称唯一性在应用层裁决（JSON 别名列无法建唯一索引）：同一用户内
display_name 与全部别名按 casefold 比对，冲突抛 ValueError 而不是
静默合并两个联系人。查找同样按 casefold 精确匹配，不做模糊猜测。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AppUserRecord, ContactRecord, Database
from app.ids import uuid7

from .models import ContactImportantDate, ContactPreference, ContactView

MAX_LIST_LIMIT = 200


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def normalize_timezone(raw: str | None) -> str | None:
    """校验并规范化 IANA 时区名；无法识别时抛 ValueError。"""
    if raw is None or not raw.strip():
        return None
    candidate = raw.strip()
    try:
        return str(ZoneInfo(candidate))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"未知时区：{candidate}") from error


def normalize_names(display_name: str, aliases: list[str] | None) -> tuple[str, list[str]]:
    name = display_name.strip()
    if not name:
        raise ValueError("联系人名称不能为空")
    cleaned: list[str] = []
    seen: set[str] = set()
    for alias in aliases or []:
        item = alias.strip()
        if not item:
            continue
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        cleaned.append(item)
    if name.casefold() in seen:
        raise ValueError("别名不能与主名称重复")
    return name, cleaned


def _to_view(record: ContactRecord) -> ContactView:
    return ContactView(
        id=record.id,
        user_id=record.user_id,
        display_name=record.display_name,
        aliases=[str(item) for item in record.aliases or []],
        relationship=record.relationship,
        timezone=record.timezone,
        important_dates=[
            ContactImportantDate.model_validate(item) for item in record.important_dates or []
        ],
        preferences=[ContactPreference.model_validate(item) for item in record.preferences or []],
        notes=record.notes,
        created_at=_aware(record.created_at),
        updated_at=_aware(record.updated_at),
    )


class ContactStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def create_contact(
        self,
        *,
        user_id: UUID,
        display_name: str,
        aliases: list[str] | None = None,
        relationship: str | None = None,
        timezone: str | None = None,
        important_dates: list[ContactImportantDate] | None = None,
        preferences: list[ContactPreference] | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> ContactView:
        name, cleaned_aliases = normalize_names(display_name, aliases)
        zone = normalize_timezone(timezone)
        moment = now or datetime.now(UTC)
        record = ContactRecord(
            id=uuid7(),
            user_id=user_id,
            display_name=name,
            aliases=cleaned_aliases,
            relationship=relationship,
            timezone=zone,
            important_dates=[item.model_dump(mode="json") for item in important_dates or []],
            preferences=[item.model_dump(mode="json") for item in preferences or []],
            notes=notes,
            created_at=moment,
            updated_at=moment,
        )
        async with self._database.sessions.begin() as session:
            await self._lock_owner(session, user_id)
            await self._assert_names_free(session, user_id, name, cleaned_aliases)
            session.add(record)
        return _to_view(record)

    async def get_contact(self, user_id: UUID, contact_id: UUID) -> ContactRecord:
        async with self._database.sessions() as session:
            record = await session.get(ContactRecord, contact_id)
        if record is None or record.user_id != user_id:
            raise LookupError("contact not found")
        return record

    async def get_contact_view(self, user_id: UUID, contact_id: UUID) -> ContactView:
        return _to_view(await self.get_contact(user_id, contact_id))

    async def list_contacts(
        self,
        user_id: UUID,
        *,
        query: str | None = None,
        limit: int = MAX_LIST_LIMIT,
    ) -> list[ContactView]:
        records = await self._user_records(user_id, limit=MAX_LIST_LIMIT)
        views = [_to_view(record) for record in records]
        keyword = (query or "").strip().casefold()
        if not keyword:
            return views[:limit]
        matched = [
            view
            for view in views
            if keyword in view.display_name.casefold()
            or any(keyword in alias.casefold() for alias in view.aliases)
        ]
        return matched[:limit]

    async def find_by_name(self, user_id: UUID, name: str) -> ContactView | None:
        """按主名称或别名 casefold 精确匹配；歧义由唯一性裁决排除。"""
        keyword = name.strip().casefold()
        if not keyword:
            return None
        for record in await self._user_records(user_id, limit=MAX_LIST_LIMIT):
            if record.display_name.casefold() == keyword or any(
                alias.casefold() == keyword for alias in record.aliases or []
            ):
                return _to_view(record)
        return None

    async def update_contact(
        self,
        user_id: UUID,
        contact_id: UUID,
        *,
        display_name: str | None = None,
        aliases: list[str] | None = None,
        relationship: str | None = None,
        timezone: str | None = None,
        important_dates: list[ContactImportantDate] | None = None,
        preferences: list[ContactPreference] | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> ContactView:
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            await self._lock_owner(session, user_id)
            managed = await session.scalar(
                update(ContactRecord)
                .where(ContactRecord.id == contact_id, ContactRecord.user_id == user_id)
                .values(updated_at=ContactRecord.updated_at)
                .returning(ContactRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            if managed is None:
                raise LookupError("contact not found")
            name, cleaned_aliases = normalize_names(
                display_name if display_name is not None else managed.display_name,
                aliases if aliases is not None else list(managed.aliases),
            )
            # Derive omitted fields from the row after its write lock, not a
            # preflight snapshot that may overwrite another accepted patch.
            zone = managed.timezone if timezone is None else normalize_timezone(timezone)
            await self._assert_names_free(
                session, user_id, name, cleaned_aliases, exclude_id=contact_id
            )
            managed.display_name = name
            managed.aliases = cleaned_aliases
            if relationship is not None:
                managed.relationship = relationship
            managed.timezone = zone
            if important_dates is not None:
                managed.important_dates = [item.model_dump(mode="json") for item in important_dates]
            if preferences is not None:
                managed.preferences = [item.model_dump(mode="json") for item in preferences]
            if notes is not None:
                managed.notes = notes
            managed.updated_at = moment
        return _to_view(managed)

    async def delete_contact(self, user_id: UUID, contact_id: UUID) -> None:
        async with self._database.sessions.begin() as session:
            removed = await session.scalar(
                delete(ContactRecord)
                .where(ContactRecord.id == contact_id, ContactRecord.user_id == user_id)
                .returning(ContactRecord.id)
            )
            if removed is None:
                raise LookupError("contact not found")

    async def contacts_with_date(self, user_id: UUID, *, month: int, day: int) -> list[ContactView]:
        """重要日期落在指定月/日的联系人（简报事实采集用）。"""
        matched = [
            view
            for view in (
                _to_view(record) for record in await self._user_records(user_id, limit=None)
            )
            if any(item.month == month and item.day == day for item in view.important_dates)
        ]
        return matched

    async def _user_records(self, user_id: UUID, *, limit: int | None) -> list[ContactRecord]:
        query = (
            select(ContactRecord)
            .where(ContactRecord.user_id == user_id)
            .order_by(ContactRecord.display_name)
        )
        if limit is not None:
            query = query.limit(limit)
        async with self._database.sessions() as session:
            return list((await session.execute(query)).scalars().all())

    async def _lock_owner(self, session: AsyncSession, user_id: UUID) -> None:
        # All name/alias writers serialize on the same owner before reading.
        # This first write also avoids SQLite read-to-write transaction upgrades.
        owner = await session.scalar(
            update(AppUserRecord)
            .where(AppUserRecord.id == user_id)
            .values(id=AppUserRecord.id)
            .returning(AppUserRecord.id)
        )
        if owner is None:
            raise LookupError("contact owner not found")

    async def _assert_names_free(
        self,
        session: AsyncSession,
        user_id: UUID,
        display_name: str,
        aliases: list[str],
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        wanted: set[str] = {display_name.casefold()}
        wanted.update(alias.casefold() for alias in aliases)
        for record in await session.scalars(
            select(ContactRecord).where(ContactRecord.user_id == user_id)
        ):
            if exclude_id is not None and record.id == exclude_id:
                continue
            taken = {record.display_name.casefold()}
            taken.update(str(item).casefold() for item in record.aliases or [])
            clash = wanted & taken
            if clash:
                raise ValueError(f"名称或别名已被「{record.display_name}」占用：{sorted(clash)[0]}")
