"""Legacy meeting constructor composed from detached policy and SQL/model adapter."""

from collections.abc import Callable

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import Database

from .summary_core import MAX_SUMMARY_INPUT_CHARS as MAX_SUMMARY_INPUT_CHARS
from .summary_core import MeetingSummarizer as MeetingSummarizer
from .summary_core import RuleBasedMeetingSummarizer as RuleBasedMeetingSummarizer
from .summary_core import StructuredMeetingSummarizer
from .summary_sql import CompletionBackend as CompletionBackend
from .summary_sql import SqlMeetingSummaryCompletion


class LlmMeetingSummarizer(StructuredMeetingSummarizer):
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        database: Database | None = None,
        fallback: MeetingSummarizer | None = None,
    ) -> None:
        super().__init__(
            SqlMeetingSummaryCompletion(
                config_store, router_builder=router_builder, database=database
            ),
            fallback=fallback,
        )
