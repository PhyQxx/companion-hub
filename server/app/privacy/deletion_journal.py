"""Identifier-only deletion intent, durable outside a database backup rollback."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import StrictModel


class DeletionIntent(StrictModel):
    version: Literal[1] = 1
    entity_kind: Literal["memory", "message"]
    entity_id: str = Field(min_length=1, max_length=200)
    deleted_ids: list[int]
    created_at: datetime

    @model_validator(mode="after")
    def identifiers_only(self) -> DeletionIntent:
        if self.entity_kind == "message":
            UUID(self.entity_id)
        elif not self.entity_id.isdecimal() or int(self.entity_id) < 1:
            raise ValueError("invalid_memory_identifier")
        if any(item < 1 for item in self.deleted_ids):
            raise ValueError("invalid_deleted_identifier")
        if self.created_at.tzinfo is None:
            raise ValueError("journal_timestamp_requires_timezone")
        return self


class DeletionJournal:
    """Append under an OS lock and fsync before the database deletion commits.

    Entries are deletion intent: if the later transaction rolls back, replay
    still honors the user's request. A corrupt journal blocks restore/startup;
    it must never be silently skipped. Keep this file with the same installation.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    async def append(self, intent: DeletionIntent) -> None:
        await asyncio.to_thread(self._append, intent)

    def _append(self, intent: DeletionIntent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            # A previous process dying halfway through append must not create
            # an apparently valid continuation. Reading validates every line.
            data = (intent.model_dump_json() + "\n").encode()
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("deletion_journal_short_write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Persist the directory entry as well on the first file creation.
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def read(self) -> list[DeletionIntent]:
        return await asyncio.to_thread(self._read)

    def _read(self) -> list[DeletionIntent]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            intents: list[DeletionIntent] = []
            for line_number, line in enumerate(handle, start=1):
                try:
                    if not line.endswith("\n"):
                        raise ValueError("incomplete_entry")
                    intents.append(DeletionIntent.model_validate(json.loads(line)))
                except (ValueError, TypeError) as error:
                    # Do not put untrusted line contents in diagnostic output.
                    raise ValueError(f"deletion_journal_invalid_line:{line_number}") from error
            return intents
