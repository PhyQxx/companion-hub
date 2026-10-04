"""Durable, content-free quota provenance for a nested voice/chat trace."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config.models import RunBudgetConfig
from app.harness.budget import BudgetDenied
from app.harness.time import utc

if TYPE_CHECKING:
    from app.db import Database

    from .budget import RunModelBudget


class ParentBudgetScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    user_id: UUID
    config: RunBudgetConfig
    phase: Literal["interactive", "maintenance"]
    allow_active_parent: bool
    delivery_deadline: datetime
    quota_conversation_id: UUID | None = None
    source_actor: Literal["browser", "satellite", "avatar.chat", "voice.satellite"] | None = None
    source_id: UUID | None = None
    conversation_id: UUID | None = None

    @classmethod
    def capture(cls, budget: RunModelBudget) -> ParentBudgetScope:
        return cls(
            run_id=budget.run_id,
            user_id=budget.owner_id,
            # The port's enabled flag is not its authority: reserve() fences
            # the SQL root's enabled snapshot. A live inherited port can carry
            # the current globally disabled config while still charging it.
            config=budget.budget_config.model_copy(update={"enabled": True}, deep=True),
            phase=budget.phase,
            allow_active_parent=budget.allow_active_parent,
            delivery_deadline=budget.delivery_deadline,
        )

    @classmethod
    def read(cls, value: object) -> ParentBudgetScope:
        try:
            scope = cls.model_validate(value)
        except ValidationError as error:
            raise BudgetDenied("chat_parent_scope_invalid") from error
        if not scope.config.enabled or scope.delivery_deadline.tzinfo is None:
            raise BudgetDenied("chat_parent_scope_invalid")
        return scope

    def restore(
        self, database: Database, current: RunBudgetConfig, *, maintenance: bool = False
    ) -> RunModelBudget:
        from .budget import RunModelBudget

        if utc(self.delivery_deadline) <= datetime.now(UTC):
            raise BudgetDenied("run_deadline_exceeded")
        # A disabled global switch does not erase an already accepted parent's
        # quota. Active monetary limits must keep their original currency.
        configs = (self.config, current)
        capped = [
            config
            for config in configs
            if config.max_daily_cost is not None or config.max_monthly_cost is not None
        ]
        currencies = {config.cost_currency for config in capped}
        if len(currencies) > 1:
            raise BudgetDenied("budget_currency_mismatch")
        values = self.config.model_dump()
        values["cost_currency"] = (
            next(iter(currencies))
            if currencies
            else self.config.cost_currency or current.cost_currency
        )
        for name in (
            "max_tool_attempts",
            "max_llm_attempts",
            "max_concurrent_llm_calls",
            "max_tokens",
            "interactive_deadline_seconds",
            "maintenance_deadline_seconds",
        ):
            values[name] = min(getattr(config, name) for config in configs)
        for name in ("max_daily_cost", "max_monthly_cost"):
            limits = [
                getattr(config, name) for config in configs if getattr(config, name) is not None
            ]
            values[name] = min(limits) if limits else None
        phase = "maintenance" if maintenance else self.phase
        return RunModelBudget(
            database,
            run_id=self.run_id,
            user_id=self.user_id,
            config=RunBudgetConfig.model_validate(values),
            phase=phase,
            allow_active_parent=(
                True if maintenance and self.phase == "interactive" else self.allow_active_parent
            ),
            delivery_deadline=self.delivery_deadline,
        )
