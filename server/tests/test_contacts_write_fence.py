"""Contact patches preserve current ownership, omitted fields and name uniqueness."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import delete, update
from test_domain_delete_fence import storage_mode
from test_job_lifecycle_fence import during_commit
from test_resource_budget import seed

from app.contacts import ContactStore
from app.db import ContactRecord


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("change", ["owner", "deleted", "updated", "unchanged"])
async def test_patch_rechecks_owned_current_row_after_writer_commit(
    backend: str, change: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        db = storage.database
        owner, _, _ = await seed(db)
        other, _, _ = await seed(db)
        contacts = ContactStore(db)
        contact = await contacts.create_contact(
            user_id=owner, display_name="Original", aliases=["Alias"], timezone="UTC"
        )

        async def patch() -> None:
            if change in {"owner", "deleted"}:
                with pytest.raises(LookupError, match="contact not found"):
                    await contacts.update_contact(owner, contact.id, notes="Accepted patch")
            else:
                result = await contacts.update_contact(owner, contact.id, notes="Accepted patch")
                assert result.notes == "Accepted patch"
                assert result.display_name == ("Current" if change == "updated" else "Original")
                assert result.aliases == (["Current alias"] if change == "updated" else ["Alias"])
                assert result.timezone == ("Asia/Shanghai" if change == "updated" else "UTC")

        changes: dict[str, object] = {"user_id": other if change == "owner" else owner}
        if change == "updated":
            changes.update(
                display_name="Current", aliases=["Current alias"], timezone="Asia/Shanghai"
            )
        await during_commit(
            db,
            lambda sql: sql.execute(
                delete(ContactRecord).where(ContactRecord.id == contact.id)
                if change == "deleted"
                else update(ContactRecord).where(ContactRecord.id == contact.id).values(**changes)
            ),
            patch,
        )
        async with db.sessions() as sql:
            row = await sql.get(ContactRecord, contact.id)
            if change == "owner":
                assert row is not None and row.user_id == other and row.notes is None
            elif change == "deleted":
                assert row is None
            else:
                assert row is not None and row.notes == "Accepted patch"


@pytest.mark.parametrize("backend", ["sqlite", "sqlite_explicit", "postgresql"])
@pytest.mark.parametrize("writers", ["create_create", "create_update", "update_update"])
async def test_parallel_name_and_alias_writers_allow_only_one_owner_match(
    backend: str, writers: str, tmp_path: Path
) -> None:
    async with storage_mode(backend, tmp_path) as storage:
        owner, _, _ = await seed(storage.database)
        contacts = ContactStore(storage.database)
        first = await contacts.create_contact(user_id=owner, display_name="First")
        second = await contacts.create_contact(user_id=owner, display_name="Second")
        left = (
            contacts.create_contact(user_id=owner, display_name="Clash")
            if writers.startswith("create")
            else contacts.update_contact(owner, first.id, display_name="Clash")
        )
        right = (
            contacts.create_contact(user_id=owner, display_name="New", aliases=["cLaSh"])
            if writers.endswith("create")
            else contacts.update_contact(owner, second.id, aliases=["cLaSh"])
        )
        results = await asyncio.gather(left, right, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError) and "已被" in str(errors[0])
        matches = [
            contact
            for contact in await contacts.list_contacts(owner)
            if "clash"
            in {contact.display_name.casefold(), *(alias.casefold() for alias in contact.aliases)}
        ]
        assert len(matches) == 1
