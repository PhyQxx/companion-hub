"""Terminal source failures cross the tool boundary even if cleanup also fails."""

import asyncio
from typing import cast
from uuid import UUID

import pytest
from test_commute_query_boundary import Calendar

from app.calendar.models import CalendarEventView
from app.commute.service import CommutePlan, CommuteService
from app.commute.tools import CommuteCheckArgs, CommuteCheckTool
from app.harness.budget import BudgetDenied
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext


@pytest.mark.parametrize("stage", ["query", "plan"])
@pytest.mark.parametrize("failure", ["budget", "cancel", "ordinary"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_tool_preserves_terminal_failures_and_closes_once(
    stage: str,
    failure: str,
    cleanup_fails: bool,
) -> None:
    calendar, calls = Calendar(), []
    error: BaseException = (
        BudgetDenied("synthetic_budget_denied")
        if failure == "budget"
        else asyncio.CancelledError()
        if failure == "cancel"
        else RuntimeError("fixture unavailable")
    )

    class Source:
        async def next_outing(self, user_id: UUID, *, within_hours: int) -> CalendarEventView:
            assert user_id == calendar.owner
            calls.append("query")
            if stage == "query":
                raise error
            return calendar.event

        async def plan_commute(self, user_id: UUID, event: CalendarEventView) -> CommutePlan:
            calls.append("plan")
            raise error

        async def aclose(self) -> None:
            calls.append("close")
            if cleanup_fails:
                raise RuntimeError("fixture cleanup failed")

    tool = CommuteCheckTool(lambda: cast(CommuteService, Source()))
    context = ToolContext(user_id=calendar.owner, privacy_level=PrivacyLevel.L1)
    if failure == "ordinary" and not cleanup_fails:
        result = await tool.execute(CommuteCheckArgs(), context)
        assert not result.ok and result.reason_code == "calendar_unavailable"
    elif failure == "ordinary":
        with pytest.raises(RuntimeError, match="fixture cleanup failed"):
            await tool.execute(CommuteCheckArgs(), context)
    else:
        with pytest.raises(type(error)) as captured:
            await tool.execute(CommuteCheckArgs(), context)
        assert captured.value is error
    assert calls == (["query", "close"] if stage == "query" else ["query", "plan", "close"])
