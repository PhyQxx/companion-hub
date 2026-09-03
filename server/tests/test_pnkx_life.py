from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_pnkx_router
from app.auth import AuthService
from app.db import Base, Database, create_database
from app.pnkx import PnkxApiError, PnkxLifeClient


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


class FakePnkxLife:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bookkeeping_operations: list[dict[str, Any]] = []
        self.commemoration_operations: list[dict[str, Any]] = []
        self.bookkeeping_ids: dict[str, int] = {}
        self.commemoration_ids: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-Integration-Token") != "integration-secret":
            # pnkx 的认证失败使用 HTTP 200 + 业务 code 401。
            return httpx.Response(200, json={"code": 401, "msg": "unauthorized"})
        path = request.url.path.removeprefix("/prod-api")
        if path == "/bookkeeping/record/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "msg": "120.00,38.00",
                    "rows": [{"id": 7, "money": "38.00", "remark": "午饭"}],
                    "total": 1,
                },
            )
        if path == "/commemorationDay/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "msg": "查询成功",
                    "rows": [
                        {
                            "id": 12,
                            "name": "纪念日",
                            "date": "2026-09-02 18:30:00",
                            "repeat": True,
                        }
                    ],
                    "total": 1,
                },
            )
        if path == "/reminder/notifications/read" and request.method == "PUT":
            return httpx.Response(200, json={"code": 200, "msg": "操作成功"})
        if path == "/offline/batch" and request.method == "POST":
            body = json.loads(request.content)
            operation = body["operations"][0]
            client_uuid = str(operation["clientUuid"])
            if operation["tableName"] == "px_bookkeeping_record":
                self.bookkeeping_operations.append(operation)
                remote_id = self.bookkeeping_ids.setdefault(client_uuid, 81)
            else:
                self.commemoration_operations.append(operation)
                remote_id = self.commemoration_ids.setdefault(client_uuid, 82)
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "total": 1,
                        "success": 1,
                        "skip": 0,
                        "fail": 0,
                        "results": [
                            {
                                "clientUuid": client_uuid,
                                "status": "success",
                                "id": remote_id,
                            }
                        ],
                    },
                },
            )
        data_by_path: dict[str, object] = {
            "/calendar/cockpit": {
                "todayTodos": [{"id": 1, "content": "测试任务"}],
                "todoCount": 1,
                "nextCommemoration": None,
            },
            "/calendar/month": [{"type": "todo", "title": "测试任务"}],
            "/reminder/today": {"todo": [{"id": 1}], "commemorationDays": []},
            "/reminder/notifications": [{"id": 9, "title": "提醒"}],
            "/reminder/unread/count": 2,
            "/bookkeeping/account/getAccountList": [
                {"accountName": "现金", "children": [{"id": 2, "accountName": "钱包"}]}
            ],
            "/bookkeeping/classification/getClassificationList": [
                {"typeName": "餐饮", "children": [{"id": 3, "typeName": "午餐"}]}
            ],
            "/bookkeeping/statistics/getPrimaryStatistics": [
                {"id": 3, "name": "餐饮", "value": 38}
            ],
            "/bookkeeping/statistics/getMonthlyStatistics": [
                {"name": "2026-09", "value": 38}
            ],
        }
        if path not in data_by_path:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        return httpx.Response(
            200,
            json={"code": 200, "msg": "操作成功", "data": data_by_path[path]},
        )


def _client(fake: FakePnkxLife, *, token: str = "integration-secret") -> PnkxLifeClient:
    return PnkxLifeClient(
        base_url="https://pnkx.test/prod-api",
        integration_token=token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )


async def test_life_client_reads_dashboard_and_forwards_month_range() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    cockpit = await client.cockpit()
    events = await client.month_events(
        start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
    )
    reminders = await client.today_reminders()
    notifications = await client.notifications()
    unread_count = await client.unread_count()

    assert cockpit["todoCount"] == 1
    assert events[0]["type"] == "todo"
    assert reminders["commemorationDays"] == []
    assert notifications[0]["id"] == 9
    assert unread_count == 2
    month_request = next(
        request for request in fake.requests if request.url.path.endswith("/calendar/month")
    )
    assert month_request.url.params["startDate"] == "2026-09-01"
    assert month_request.url.params["endDate"] == "2026-09-30"


async def test_life_client_controls_notifications_and_commemorations() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    page = await client.commemoration_days(page=2, page_size=10, name="纪念")
    await client.mark_notifications_read([9])
    remote_id = await client.create_commemoration_day(
        client_uuid="aria:commemoration:stable",
        name="纪念日",
        event_time="2026-09-02 18:30:00",
        repeat=True,
        icon="heart",
        order_num=3,
        remark="测试",
    )

    assert page.total == 1
    assert page.items[0]["name"] == "纪念日"
    list_request = next(
        request
        for request in fake.requests
        if request.url.path.endswith("/commemorationDay/list")
    )
    assert list_request.url.params["pageNum"] == "2"
    assert list_request.url.params["name"] == "纪念"
    read_request = next(
        request
        for request in fake.requests
        if request.url.path.endswith("/reminder/notifications/read")
    )
    assert json.loads(read_request.content) == [9]
    assert remote_id == "82"
    operation = fake.commemoration_operations[0]
    assert operation["clientUuid"] == "aria:commemoration:stable"
    assert operation["payload"]["date"] == "2026-09-02 18:30:00"
    assert operation["payload"]["orderNum"] == 3


async def test_life_client_recognizes_pnkx_business_401() -> None:
    fake = FakePnkxLife()
    client = _client(fake, token="wrong")

    with pytest.raises(PnkxApiError) as captured:
        await client.cockpit()

    assert captured.value.reason_code == "integration_token_rejected"


async def test_bookkeeping_client_reads_and_creates_idempotently() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    accounts = await client.bookkeeping_accounts()
    classifications = await client.bookkeeping_classifications(type_difference="1")
    page = await client.bookkeeping_records(page=1, page_size=20, month="2026-09")
    remote_id = await client.create_bookkeeping_record(
        client_uuid="aria:bookkeeping:stable",
        account_id=2,
        classification_id=3,
        amount="38.00",
        pay_time="2026-09-02 12:30:00",
        remark="午饭",
    )
    categories = await client.bookkeeping_primary_statistics(
        month="2026-09", type_difference="1"
    )

    assert accounts[0]["children"][0]["id"] == 2
    assert classifications[0]["children"][0]["id"] == 3
    assert page.items[0]["remark"] == "午饭"
    assert page.inflow == "120.00"
    assert page.outflow == "38.00"
    assert remote_id == "81"
    operation = fake.bookkeeping_operations[0]
    assert operation["clientUuid"] == "aria:bookkeeping:stable"
    assert operation["payload"]["payTime"] == "2026-09-02 12:30:00"
    assert categories[0]["name"] == "餐饮"


async def test_pnkx_api_requires_auth_and_aggregates_today(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="pnkx owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/pnkx/today")
        authorized = await client.get(
            "/api/v1/pnkx/today",
            headers={"Authorization": f"Bearer {owner.access_token}"},
        )
        invalid_range = await client.get(
            "/api/v1/pnkx/calendar/month?start_date=2026-09-30&end_date=2026-09-01",
            headers={"Authorization": f"Bearer {owner.access_token}"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["cockpit"]["todoCount"] == 1
    assert authorized.json()["unread_count"] == 2
    assert invalid_range.status_code == 422


async def test_bookkeeping_api_validates_and_creates(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="bookkeeping owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    body = {
        "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d1",
        "account_id": 2,
        "classification_id": 3,
        "amount": "38.00",
        "pay_time": "2026-09-02T04:30:00Z",
        "remark": "午饭",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accounts = await client.get("/api/v1/pnkx/bookkeeping/accounts", headers=headers)
        records = await client.get(
            "/api/v1/pnkx/bookkeeping/records?month=2026-09", headers=headers
        )
        created = await client.post(
            "/api/v1/pnkx/bookkeeping/records", headers=headers, json=body
        )
        invalid = await client.post(
            "/api/v1/pnkx/bookkeeping/records",
            headers=headers,
            json={**body, "amount": "0"},
        )

    assert accounts.status_code == 200
    assert records.json()["items"][0]["money"] == "38.00"
    assert created.status_code == 201
    assert created.json()["remote_id"] == "81"
    assert created.json()["client_uuid"].startswith("aria:bookkeeping:")
    assert fake.bookkeeping_operations[0]["payload"]["payTime"] == "2026-09-02 12:30:00"
    assert invalid.status_code == 422


async def test_bookkeeping_api_write_gate_defaults_closed(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="safe owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/pnkx/bookkeeping/records",
            headers={"Authorization": f"Bearer {owner.access_token}"},
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d1",
                "account_id": 2,
                "classification_id": 3,
                "amount": "38.00",
                "pay_time": "2026-09-02T04:30:00Z",
            },
        )

    assert response.status_code == 503
    assert fake.bookkeeping_operations == []


async def test_commemoration_and_notification_api_controls(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="life owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get(
            "/api/v1/pnkx/commemorations?page=1&page_size=20", headers=headers
        )
        created = await client.post(
            "/api/v1/pnkx/commemorations",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d2",
                "name": "纪念日",
                "event_time": "2026-09-02T10:30:00Z",
                "repeat": True,
                "order_num": 3,
            },
        )
        marked = await client.put(
            "/api/v1/pnkx/notifications/read",
            headers=headers,
            json={"ids": [9]},
        )

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert created.status_code == 201
    assert created.json()["remote_id"] == "82"
    assert created.json()["client_uuid"].startswith("aria:commemoration:")
    assert fake.commemoration_operations[0]["payload"]["date"] == (
        "2026-09-02 18:30:00"
    )
    assert marked.status_code == 200
    assert marked.json() == {"success": True}
