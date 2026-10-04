"""Durable, content-free quota provenance for a nested voice/chat trace."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config.models import RunBudgetConfig
from app.harness.budget import BudgetDenied, current_budget, current_tool_budget
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


def current_parent_budget(database: Database, user_id: UUID) -> RunModelBudget | None:
    """Freeze a coherent SQL-backed quota from model and explicit tool facets.

    The later owned-run admission still proves the SQL root's authority. A tool
    facade carries limits and an absolute deadline, never new quota credit.
    """
    from .budget import RunModelBudget
    from .resources import RunToolBudget

    model, tool = current_budget(), current_tool_budget()
    if model is not None and (not isinstance(model, RunModelBudget) or model.owner_id != user_id):
        raise BudgetDenied("budget_owner_invalid")
    if tool is not None and (not isinstance(tool, RunToolBudget) or tool.owner_id != user_id):
        raise BudgetDenied("budget_owner_invalid")
    if model is None and tool is None:
        return None
    if model is None:
        assert isinstance(tool, RunToolBudget)
        model = RunModelBudget(
            database,
            run_id=tool.run_id,
            user_id=user_id,
            config=tool.budget_config.model_copy(deep=True),
            phase="maintenance" if tool.maintenance else "interactive",
            allow_active_parent=tool.allow_active_model_parent,
            delivery_deadline=tool.delivery_deadline,
        )
    assert isinstance(model, RunModelBudget)
    scope = ParentBudgetScope.capture(model)
    if tool is not None:
        assert isinstance(tool, RunToolBudget)
        if model.run_id != tool.run_id or (model.phase == "maintenance") != tool.maintenance:
            raise BudgetDenied("budget_parent_scope_invalid")
        scope = scope.model_copy(
            update={
                "delivery_deadline": min(utc(model.delivery_deadline), utc(tool.delivery_deadline)),
                "allow_active_parent": model.allow_active_parent and tool.allow_active_model_parent,
            }
        )
        return scope.restore(database, tool.budget_config)
    return scope.restore(database, model.budget_config)
