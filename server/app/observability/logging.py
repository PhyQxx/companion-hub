from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .redaction import redact_fields


class StructuredLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(
        self,
        level: int,
        event: str,
        *,
        trace_id: UUID | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {"event": event}
        if trace_id is not None:
            record["trace_id"] = str(trace_id)
        record.update(redact_fields(fields or {}))
        self._logger.log(level, json.dumps(record, sort_keys=True, default=str))
