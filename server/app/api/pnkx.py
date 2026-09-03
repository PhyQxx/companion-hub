"""pnkx 生活数据实时查询与受控写入 API。"""

# 注意：不要加 `from __future__ import annotations`，原因同其他懒路由模块。

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.pnkx import PnkxApiError, PnkxLifeClient
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class PnkxObjectResponse(StrictModel):
    data: dict[str, Any]


class PnkxItemsResponse(StrictModel):
    items: list[dict[str, Any]]


class PnkxUnreadCountResponse(StrictModel):
    count: int


class PnkxActionResponse(StrictModel):
    success: bool


class PnkxTodayResponse(StrictModel):
    cockpit: dict[str, Any]
    reminders: dict[str, Any]
    unread_count: int


class PnkxBookkeepingPageResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int
    inflow: str | None
    outflow: str | None


class PnkxBookkeepingCreatePayload(StrictModel):
    idempotency_key: UUID
    account_id: Annotated[int, Field(gt=0)]
    classification_id: Annotated[int, Field(gt=0)]
    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
    pay_time: datetime
    remark: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


class PnkxBookkeepingCreateResponse(StrictModel):
    remote_id: str
    client_uuid: str


class PnkxNotificationReadPayload(StrictModel):
    ids: list[Annotated[int, Field(gt=0)]] | None = None


class PnkxCommemorationPageResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int


class PnkxCommemorationCreatePayload(StrictModel):
    idempotency_key: UUID
    name: Annotated[str, Field(min_length=1, max_length=100)]
    event_time: datetime
    repeat: bool = True
    icon: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    order_num: Annotated[int, Field(ge=0)] | None = None
    remark: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


class PnkxCommemorationCreateResponse(StrictModel):
    remote_id: str
    client_uuid: str


def create_pnkx_router(
    client: PnkxLifeClient,
    auth_service: AuthService,
    *,
    writes_enabled: bool = False,
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/pnkx", tags=["pnkx"])

    def unavailable(error: Exception) -> HTTPException:
        detail = (
            "pnkx authentication failed"
            if isinstance(error, PnkxApiError)
            and error.reason_code == "integration_token_rejected"
            else "pnkx service unavailable"
        )
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    @router.get("/today", response_model=PnkxTodayResponse)
    async def today(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxTodayResponse:
        try:
            cockpit, reminders, unread_count = await asyncio.gather(
                client.cockpit(), client.today_reminders(), client.unread_count()
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxTodayResponse(
            cockpit=cockpit, reminders=reminders, unread_count=unread_count
        )

    @router.get("/calendar/cockpit", response_model=PnkxObjectResponse)
    async def cockpit(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxObjectResponse:
        try:
            return PnkxObjectResponse(data=await client.cockpit())
        except Exception as error:
            raise unavailable(error) from error

    @router.get("/calendar/month", response_model=PnkxItemsResponse)
    async def month_events(
        _: Annotated[ChatPrincipal, Depends(guard)],
        start_date: Annotated[date | None, Query()] = None,
        end_date: Annotated[date | None, Query()] = None,
    ) -> PnkxItemsResponse:
        if start_date is not None and end_date is not None and start_date > end_date:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="start_date must be on or before end_date",
            )
        try:
            items = await client.month_events(start_date=start_date, end_date=end_date)
        except Exception as error:
            raise unavailable(error) from error
        return PnkxItemsResponse(items=items)

    @router.get("/reminders/today", response_model=PnkxObjectResponse)
    async def reminders(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxObjectResponse:
        try:
            return PnkxObjectResponse(data=await client.today_reminders())
        except Exception as error:
            raise unavailable(error) from error

    @router.get("/notifications", response_model=PnkxItemsResponse)
    async def notifications(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxItemsResponse:
        try:
            return PnkxItemsResponse(items=await client.notifications())
        except Exception as error:
            raise unavailable(error) from error

    @router.get("/notifications/unread-count", response_model=PnkxUnreadCountResponse)
    async def unread_count(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxUnreadCountResponse:
        try:
            return PnkxUnreadCountResponse(count=await client.unread_count())
        except Exception as error:
            raise unavailable(error) from error

    @router.put("/notifications/read", response_model=PnkxActionResponse)
    async def mark_notifications_read(
        body: PnkxNotificationReadPayload,
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxActionResponse:
        if not writes_enabled:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pnkx writes are disabled",
            )
        try:
            await client.mark_notifications_read(body.ids)
        except Exception as error:
            raise unavailable(error) from error
        return PnkxActionResponse(success=True)

    @router.get(
        "/commemorations", response_model=PnkxCommemorationPageResponse
    )
    async def commemoration_days(
        _: Annotated[ChatPrincipal, Depends(guard)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        name: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    ) -> PnkxCommemorationPageResponse:
        try:
            result = await client.commemoration_days(
                page=page, page_size=page_size, name=name
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxCommemorationPageResponse(items=result.items, total=result.total)

    @router.post(
        "/commemorations",
        response_model=PnkxCommemorationCreateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_commemoration_day(
        body: PnkxCommemorationCreatePayload,
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxCommemorationCreateResponse:
        if not writes_enabled:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pnkx writes are disabled",
            )
        client_uuid = f"aria:commemoration:{body.idempotency_key}"
        event_time = body.event_time.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            remote_id = await client.create_commemoration_day(
                client_uuid=client_uuid,
                name=body.name,
                event_time=event_time,
                repeat=body.repeat,
                icon=body.icon,
                order_num=body.order_num,
                remark=body.remark,
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxCommemorationCreateResponse(
            remote_id=remote_id, client_uuid=client_uuid
        )

    @router.get("/bookkeeping/accounts", response_model=PnkxItemsResponse)
    async def bookkeeping_accounts(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxItemsResponse:
        try:
            return PnkxItemsResponse(items=await client.bookkeeping_accounts())
        except Exception as error:
            raise unavailable(error) from error

    @router.get("/bookkeeping/classifications", response_model=PnkxItemsResponse)
    async def bookkeeping_classifications(
        _: Annotated[ChatPrincipal, Depends(guard)],
        type_difference: Annotated[Literal["0", "1"] | None, Query()] = None,
    ) -> PnkxItemsResponse:
        try:
            items = await client.bookkeeping_classifications(
                type_difference=type_difference
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxItemsResponse(items=items)

    @router.get("/bookkeeping/records", response_model=PnkxBookkeepingPageResponse)
    async def bookkeeping_records(
        _: Annotated[ChatPrincipal, Depends(guard)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
        search: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    ) -> PnkxBookkeepingPageResponse:
        try:
            result = await client.bookkeeping_records(
                page=page, page_size=page_size, month=month, search=search
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxBookkeepingPageResponse(
            items=result.items,
            total=result.total,
            inflow=result.inflow,
            outflow=result.outflow,
        )

    @router.post(
        "/bookkeeping/records",
        response_model=PnkxBookkeepingCreateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_bookkeeping_record(
        body: PnkxBookkeepingCreatePayload,
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PnkxBookkeepingCreateResponse:
        if not writes_enabled:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pnkx writes are disabled",
            )
        client_uuid = f"aria:bookkeeping:{body.idempotency_key}"
        pay_time = body.pay_time.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            remote_id = await client.create_bookkeeping_record(
                client_uuid=client_uuid,
                account_id=body.account_id,
                classification_id=body.classification_id,
                amount=format(body.amount, "f"),
                pay_time=pay_time,
                remark=body.remark,
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxBookkeepingCreateResponse(
            remote_id=remote_id, client_uuid=client_uuid
        )

    @router.get(
        "/bookkeeping/statistics/categories", response_model=PnkxItemsResponse
    )
    async def bookkeeping_category_statistics(
        _: Annotated[ChatPrincipal, Depends(guard)],
        month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
        type_difference: Annotated[Literal["0", "1"], Query()] = "1",
    ) -> PnkxItemsResponse:
        try:
            items = await client.bookkeeping_primary_statistics(
                month=month, type_difference=type_difference
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxItemsResponse(items=items)

    @router.get("/bookkeeping/statistics/monthly", response_model=PnkxItemsResponse)
    async def bookkeeping_monthly_statistics(
        _: Annotated[ChatPrincipal, Depends(guard)],
        year: Annotated[int, Query(ge=2000, le=2200)],
        type_difference: Annotated[Literal["0", "1"], Query()] = "1",
    ) -> PnkxItemsResponse:
        try:
            items = await client.bookkeeping_monthly_statistics(
                year=year, type_difference=type_difference
            )
        except Exception as error:
            raise unavailable(error) from error
        return PnkxItemsResponse(items=items)

    return router
