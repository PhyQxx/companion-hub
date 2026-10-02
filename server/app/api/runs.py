from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import AuthService, ChatPrincipal
from app.chat import ChatService
from app.schemas.billing import BillingEvidence, BillingReport, CostPeriod, CostSnapshot
from app.schemas.costs import CostSummaryView
from app.schemas.runs import RunEventView, RunView

from .auth import ChatSessionGuard


def create_runs_router(service: ChatService, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

    @router.get("", response_model=list[RunView])
    async def list_runs(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        before_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[RunView]:
        return await service.runs.list_runs(
            user_id=principal.user_id, before_id=before_id, limit=limit
        )

    @router.get("/costs", response_model=CostSummaryView)
    async def costs(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        days: Annotated[int, Query(ge=1, le=366)] = 30,
    ) -> CostSummaryView:
        return await service.runs.cost_summary(user_id=principal.user_id, days=days)

    @router.post("/costs/snapshot", response_model=CostSnapshot)
    async def cost_snapshot(
        period: CostPeriod,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> CostSnapshot:
        try:
            return await service.runs.cost_snapshot(user_id=principal.user_id, period=period)
        except ValueError as error:
            raise HTTPException(422, "billing_period_too_large") from error

    @router.post("/costs/reconcile", response_model=BillingReport)
    async def reconcile_bill(
        evidence: BillingEvidence,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> BillingReport:
        try:
            return await service.runs.reconcile_bill(user_id=principal.user_id, evidence=evidence)
        except ValueError as error:
            raise HTTPException(422, "billing_period_too_large") from error

    @router.get("/{run_id}", response_model=RunView)
    async def get_run(run_id: UUID, principal: Annotated[ChatPrincipal, Depends(guard)]) -> RunView:
        try:
            return await service.runs.get(run_id, user_id=principal.user_id)
        except LookupError as error:
            raise HTTPException(404, "run not found") from error

    @router.get("/{run_id}/events", response_model=list[RunEventView])
    async def events(
        run_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
        after_seq: Annotated[int, Query(ge=0)] = 0,
    ) -> list[RunEventView]:
        try:
            return await service.runs.events(run_id, user_id=principal.user_id, after_seq=after_seq)
        except LookupError as error:
            raise HTTPException(404, "run not found") from error

    @router.post("/{run_id}/cancel", response_model=RunView)
    async def cancel(run_id: UUID, principal: Annotated[ChatPrincipal, Depends(guard)]) -> RunView:
        try:
            await service.cancel_run(run_id, user_id=principal.user_id)
            return await service.runs.get(run_id, user_id=principal.user_id)
        except LookupError as error:
            raise HTTPException(404, "run not found") from error

    return router
