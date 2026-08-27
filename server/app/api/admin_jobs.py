from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.jobs import JobEngine, JobView
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class JobListItem(StrictModel):
    id: str
    kind: str
    owner: str
    status: str
    priority: int
    progress: float
    current_step: str | None
    resource_class: str
    attempts: int
    max_attempts: int
    error_code: str | None
    lease_owner: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobListResponse(StrictModel):
    jobs: list[JobListItem]
    total: int


class JobDetailResponse(StrictModel):
    id: str
    kind: str
    owner: str
    status: str
    priority: int
    progress: float
    current_step: str | None
    resource_class: str
    attempts: int
    max_attempts: int
    error_code: str | None
    lease_owner: str | None
    input: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


def _to_list_item(view: JobView) -> JobListItem:
    return JobListItem(
        id=str(view.id),
        kind=view.kind,
        owner=view.owner,
        status=view.status,
        priority=view.priority,
        progress=view.progress,
        current_step=view.current_step,
        resource_class=view.resource_class,
        attempts=view.attempts,
        max_attempts=view.max_attempts,
        error_code=view.error_code,
        lease_owner=view.lease_owner,
        created_at=view.created_at,
        started_at=view.started_at,
        completed_at=view.completed_at,
    )


def create_admin_jobs_router(
    job_engine: JobEngine,
    *,
    admin_token: str | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/jobs",
        tags=["admin-jobs"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=JobListResponse)
    async def list_jobs(
        status: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JobListResponse:
        jobs = await job_engine.list_jobs(
            status=status,  # type: ignore[arg-type]
            kind=kind,
            limit=limit,
            offset=offset,
        )
        return JobListResponse(
            jobs=[_to_list_item(j) for j in jobs],
            total=len(jobs),
        )

    @router.get("/{job_id}", response_model=JobDetailResponse)
    async def get_job(job_id: UUID) -> JobDetailResponse:
        job = await job_engine.get(job_id)
        if job is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="job not found")
        return JobDetailResponse(
            id=str(job.id),
            kind=job.kind,
            owner=job.owner,
            status=job.status,
            priority=job.priority,
            progress=job.progress,
            current_step=job.current_step,
            resource_class=job.resource_class,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            error_code=job.error_code,
            lease_owner=job.lease_owner,
            input={},
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
        )

    @router.post("/{job_id}/cancel")
    async def cancel_job(job_id: UUID) -> dict[str, bool]:
        ok = await job_engine.cancel(job_id)
        return {"cancelled": ok}

    @router.post("/system/expire-leases")
    async def expire_leases() -> dict[str, int]:
        count = await job_engine.expire_stale_leases()
        return {"expired": count}

    return router
