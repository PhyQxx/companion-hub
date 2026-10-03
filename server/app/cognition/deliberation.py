"""Legacy constructor and exports; pure policy uses explicit completion ports."""

from __future__ import annotations

from collections.abc import Callable

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import Database

from .deliberation_sql import CompletionBackend as CompletionBackend
from .deliberation_sql import SqlDeliberationCompletion
from .ports import COGNITIVE_POLICY_VERSION as COGNITIVE_POLICY_VERSION
from .ports import Deliberator as Deliberator
from .rule import RuleBasedDeliberator as RuleBasedDeliberator
from .structured import StructuredDeliberator


class RouterDeliberator(StructuredDeliberator):
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        fallback: Deliberator | None = None,
        database: Database | None = None,
    ) -> None:
        super().__init__(
            SqlDeliberationCompletion(
                config_store, database=database, router_builder=router_builder
            ),
            fallback=fallback,
        )
