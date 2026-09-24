from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_pnkx_router
from app.auth import AuthService
from app.db import Base, Database, create_database
from app.pnkx import PnkxApiError, PnkxLifeClient
from app.pnkx.tools import PnkxCreateArgs, PnkxCreateTool, PnkxReadArgs, PnkxReadTool
from app.schemas import PrivacyLevel
from app.tools import ToolContext


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
        self.note_operations: list[dict[str, Any]] = []
        self.diary_operations: list[dict[str, Any]] = []
        self.subscription_operations: list[dict[str, Any]] = []
        self.shopping_list_operations: list[dict[str, Any]] = []
        self.shopping_item_operations: list[dict[str, Any]] = []
        self.recipe_operations: list[dict[str, Any]] = []
        self.meal_plan_operations: list[dict[str, Any]] = []
        self.todo_operations: list[dict[str, Any]] = []
        self.bookkeeping_ids: dict[str, int] = {}
        self.commemoration_ids: dict[str, int] = {}
        self.note_ids: dict[str, int] = {}
        self.diary_ids: dict[str, int] = {}
        self.subscription_ids: dict[str, int] = {}
        self.shopping_list_ids: dict[str, int] = {}
        self.shopping_item_ids: dict[str, int] = {}
        self.recipe_ids: dict[str, int] = {}
        self.meal_plan_ids: dict[str, int] = {}
        self.todo_ids: dict[str, int] = {}

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
        if path == "/note/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [{"id": 21, "title": "旅行清单", "folder": 4}],
                    "total": 1,
                },
            )
        if path == "/note/folder/treeList":
            return httpx.Response(
                200,
                json={"code": 200, "data": [{"id": 4, "name": "生活"}]},
            )
        if path == "/admin/diary/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [{"id": 31, "title": "晴天", "date": "2026-09-03"}],
                    "total": 1,
                },
            )
        if path == "/subscription/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [
                        {
                            "id": 41,
                            "name": "云服务",
                            "amount": 20,
                            "cycle": "monthly",
                        }
                    ],
                    "total": 1,
                },
            )
        if path == "/subscription/forecast":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"monthlyTotal": 20, "yearlyTotal": 240, "count": 1},
                },
            )
        if path == "/subscription" and request.method == "POST":
            body = json.loads(request.content)
            self.subscription_operations.append(body)
            client_uuid = str(body["clientUuid"])
            remote_id = self.subscription_ids.setdefault(client_uuid, 85)
            return httpx.Response(200, json={"code": 200, "data": remote_id})
        if path == "/shoppingList/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [{"id": 51, "name": "日用品", "icon": "cart"}],
                    "total": 1,
                },
            )
        if path == "/shoppingItem/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [
                        {"id": 52, "listId": 51, "name": "纸巾", "checked": False}
                    ],
                    "total": 1,
                },
            )
        if path == "/shoppingList" and request.method == "POST":
            body = json.loads(request.content)
            self.shopping_list_operations.append(body)
            remote_id = self.shopping_list_ids.setdefault(str(body["clientUuid"]), 86)
            return httpx.Response(200, json={"code": 200, "data": remote_id})
        if path == "/shoppingItem" and request.method == "POST":
            body = json.loads(request.content)
            self.shopping_item_operations.append(body)
            remote_id = self.shopping_item_ids.setdefault(str(body["clientUuid"]), 87)
            return httpx.Response(200, json={"code": 200, "data": remote_id})
        if path == "/recipe/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [{"id": 61, "title": "番茄炒蛋", "servings": 2}],
                    "total": 1,
                },
            )
        if path == "/mealPlan/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [
                        {
                            "id": 62,
                            "planDate": "2026-09-04",
                            "mealType": 2,
                            "title": "番茄炒蛋",
                        }
                    ],
                    "total": 1,
                },
            )
        if path == "/recipe" and request.method == "POST":
            body = json.loads(request.content)
            self.recipe_operations.append(body)
            remote_id = self.recipe_ids.setdefault(str(body["clientUuid"]), 88)
            return httpx.Response(200, json={"code": 200, "data": remote_id})
        if path == "/mealPlan" and request.method == "POST":
            body = json.loads(request.content)
            self.meal_plan_operations.append(body)
            remote_id = self.meal_plan_ids.setdefault(str(body["clientUuid"]), 89)
            return httpx.Response(200, json={"code": 200, "data": remote_id})
        if path == "/admin/toDo/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "rows": [
                        {
                            "id": 71,
                            "content": "整理联调清单",
                            "status": False,
                            "priority": 3,
                            "kanbanStatus": 0,
                        }
                    ],
                    "total": 1,
                },
            )
        if path == "/admin/toDo" and request.method == "POST":
            body = json.loads(request.content)
            self.todo_operations.append(body)
            remote_id = self.todo_ids.setdefault(str(body["clientUuid"]), 90)
            return httpx.Response(200, json={"code": 200, "data": remote_id})
        if path == "/offline/batch" and request.method == "POST":
            body = json.loads(request.content)
            operation = body["operations"][0]
            client_uuid = str(operation["clientUuid"])
            if operation["tableName"] == "px_bookkeeping_record":
                self.bookkeeping_operations.append(operation)
                remote_id = self.bookkeeping_ids.setdefault(client_uuid, 81)
            elif operation["tableName"] == "px_commemoration_day":
                self.commemoration_operations.append(operation)
                remote_id = self.commemoration_ids.setdefault(client_uuid, 82)
            elif operation["tableName"] == "px_note":
                self.note_operations.append(operation)
                remote_id = self.note_ids.setdefault(client_uuid, 83)
            else:
                self.diary_operations.append(operation)
                remote_id = self.diary_ids.setdefault(client_uuid, 84)
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
            "/admin/toDo/kanban": {
                "todo": [{"id": 71, "content": "整理联调清单"}],
                "doing": [],
                "done": [],
                "doneTotal": 0,
            },
            "/admin/toDo/getLabelList": ["工作", "生活"],
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


async def test_life_client_reads_and_creates_notes_and_diaries() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    notes = await client.notes(page=2, page_size=10, title="旅行", folder_id=4)
    folders = await client.note_folders()
    diaries = await client.diaries(page=1, page_size=20, month="2026-09")
    note_id = await client.create_note(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d3",
        title="旅行清单",
        content="证件、充电器",
        folder_id=4,
        order=2,
    )
    diary_id = await client.create_diary(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d4",
        title="晴天",
        content="今天很好。",
        entry_date="2026-09-03",
        mood="开心",
        weather="晴",
    )

    assert notes.total == 1
    assert folders[0]["name"] == "生活"
    assert diaries.items[0]["date"] == "2026-09-03"
    assert note_id == "83"
    assert diary_id == "84"
    note = fake.note_operations[0]
    diary = fake.diary_operations[0]
    assert note["payload"]["folder"] == 4
    assert note["payload"]["order"] == 2
    assert diary["payload"]["date"] == "2026-09-03"
    diary_request = next(
        request
        for request in fake.requests
        if request.url.path.endswith("/admin/diary/list")
    )
    assert diary_request.url.params["date"] == "2026-09-01"


async def test_life_client_reads_forecasts_and_creates_subscriptions() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    page = await client.subscriptions(
        page=2, page_size=10, name="云", cycle="monthly", enabled=True
    )
    forecast = await client.subscription_forecast()
    remote_id = await client.create_subscription(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d5",
        name="云服务",
        amount="20.00",
        cycle="monthly",
        cycle_interval=1,
        next_payment_date="2026-10-01",
        account_id=2,
        classification_id=3,
        payment_method="支付宝",
        reminder_lead_days=3,
    )

    assert page.total == 1
    assert forecast["yearlyTotal"] == 240
    assert remote_id == "85"
    request = next(
        item
        for item in fake.requests
        if item.url.path.endswith("/subscription/list")
    )
    assert request.url.params["enabled"] == "true"
    operation = fake.subscription_operations[0]
    assert operation["clientUuid"] == "018f7f4489d27cc8bc198f51f522a4d5"
    assert operation["nextPaymentDate"] == "2026-10-01"


async def test_life_client_reads_and_creates_shopping_data() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    lists = await client.shopping_lists(page=1, page_size=20, name="日用")
    items = await client.shopping_items(list_id=51, checked=False)
    list_id = await client.create_shopping_list(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d6",
        name="日用品",
        icon="cart",
        order_num=2,
    )
    item_id = await client.create_shopping_item(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d7",
        list_id=51,
        name="纸巾",
        quantity="2包",
        checked=False,
        sort_order=3,
    )

    assert lists.total == 1
    assert items.items[0]["name"] == "纸巾"
    assert list_id == "86"
    assert item_id == "87"
    assert fake.shopping_list_operations[0]["orderNum"] == 2
    assert fake.shopping_item_operations[0]["listId"] == 51
    assert fake.shopping_item_operations[0]["quantity"] == "2包"


async def test_life_client_reads_and_creates_recipes_and_meal_plans() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    recipes = await client.recipes(title="番茄", servings=2)
    meals = await client.meal_plans(plan_date="2026-09-04", meal_type=2)
    recipe_id = await client.create_recipe(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d8",
        title="番茄炒蛋",
        servings=2,
        ingredients=[{"name": "番茄", "quantity": "2个"}],
    )
    meal_id = await client.create_meal_plan(
        client_uuid="018f7f4489d27cc8bc198f51f522a4d9",
        plan_date="2026-09-04",
        meal_type=2,
        title="番茄炒蛋",
        recipe_id=61,
        sort_order=1,
    )

    assert recipes.total == 1
    assert meals.items[0]["mealType"] == 2
    assert recipe_id == "88"
    assert meal_id == "89"
    assert fake.recipe_operations[0]["ingredients"][0]["name"] == "番茄"
    assert fake.meal_plan_operations[0]["planDate"] == "2026-09-04"


async def test_life_client_reads_and_creates_todos() -> None:
    fake = FakePnkxLife()
    client = _client(fake)

    todos = await client.todos(
        search="联调", completed=False, label="工作", priority=3, kanban_status=0
    )
    kanban = await client.todo_kanban()
    labels = await client.todo_labels()
    todo_id = await client.create_todo(
        client_uuid="018f7f4489d27cc8bc198f51f522a4da",
        content="整理联调清单",
        plan_start_time="2026-09-04 09:00:00",
        plan_end_time="2026-09-04 10:00:00",
        label="工作",
        priority=3,
        kanban_status=0,
        sort_order=1,
    )

    assert todos.total == 1
    assert kanban["todo"][0]["id"] == 71
    assert labels == ["工作", "生活"]
    assert todo_id == "90"
    request = next(
        item for item in fake.requests if item.url.path.endswith("/admin/toDo/list")
    )
    assert request.url.params["status"] == "false"
    assert request.url.params["kanbanStatus"] == "0"
    operation = fake.todo_operations[0]
    assert operation["clientUuid"] == "018f7f4489d27cc8bc198f51f522a4da"
    assert operation["planStartTime"] == "2026-09-04 09:00:00"


async def test_pnkx_chat_tools_read_and_create_with_turn_idempotency() -> None:
    fake = FakePnkxLife()
    client = _client(fake)
    read_tool = PnkxReadTool(client, runs_local=True)
    create_tool = PnkxCreateTool(client, runs_local=True)
    turn_id = UUID("018f7f44-89d2-7cc8-bc19-8f51f522a4db")
    context = ToolContext(privacy_level=PrivacyLevel.L1, turn_id=turn_id)

    read_result = await read_tool.execute(
        PnkxReadArgs(
            resource="todos",
            query="联调",
            label="工作",
            completed=False,
            priority=3,
            kanban_status=0,
        ),
        context,
    )
    create_result = await create_tool.execute(
        PnkxCreateArgs(
            resource="todo",
            content="整理联调清单",
            plan_start_time="2026-09-04T09:00:00",
            priority=3,
        ),
        context,
    )

    assert read_result.ok is True
    assert read_result.data["items"][0]["id"] == 71
    assert create_result.ok is True
    assert create_result.data == {"resource": "todo", "remote_id": "90"}
    assert fake.todo_operations[0]["clientUuid"] == turn_id.hex
    assert fake.todo_operations[0]["planStartTime"] == "2026-09-04 09:00:00"


async def test_pnkx_create_todo_falls_back_to_name_or_title() -> None:
    fake = FakePnkxLife()
    client = _client(fake)
    create_tool = PnkxCreateTool(client, runs_local=True)
    context = ToolContext(
        privacy_level=PrivacyLevel.L1, turn_id=UUID("018f7f44-89d2-7cc8-bc19-8f51f522a4db")
    )

    # 模型把待办正文填进 title 而漏掉 content 时不再报 pnkx_content_required
    result = await create_tool.execute(
        PnkxCreateArgs(resource="todo", title="明天去潍坊"),
        context,
    )

    assert result.ok is True
    assert fake.todo_operations[0]["content"] == "明天去潍坊"


async def test_pnkx_create_todo_backfills_relative_date_label_and_remark() -> None:
    fake = FakePnkxLife()
    client = _client(fake)
    create_tool = PnkxCreateTool(client, runs_local=True)
    context = ToolContext(
        privacy_level=PrivacyLevel.L1,
        turn_id=UUID("018f7f44-89d2-7cc8-bc19-8f51f522a4dc"),
        user_text="加个待办，明天去潍坊",
        current_time=datetime(2026, 9, 24, 13, 16, 52, tzinfo=UTC),
        timezone_name="Asia/Shanghai",
    )

    result = await create_tool.execute(
        PnkxCreateArgs(resource="todo", content="去潍坊"),
        context,
    )

    assert result.ok is True
    operation = fake.todo_operations[0]
    assert operation["planStartTime"] == "2026-09-25 00:00:00"
    assert operation["planEndTime"] == "2026-09-25 23:59:59"
    assert operation["label"] == "出行"
    assert operation["remark"] == "加个待办，明天去潍坊"


async def test_pnkx_chat_read_requires_list_id_for_shopping_items() -> None:
    tool = PnkxReadTool(_client(FakePnkxLife()), runs_local=True)

    result = await tool.execute(
        PnkxReadArgs(resource="shopping_items"),
        ToolContext(privacy_level=PrivacyLevel.L1),
    )

    assert result.ok is False
    assert result.reason_code == "pnkx_list_id_required"


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
    assert created.json()["client_uuid"] == "018f7f4489d27cc8bc198f51f522a4d1"
    assert fake.bookkeeping_operations[0]["payload"]["payTime"] == "2026-09-02 12:30:00"
    assert invalid.status_code == 422


async def test_pnkx_api_write_gate_defaults_closed(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="safe owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bookkeeping = await client.post(
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
        note = await client.post(
            "/api/v1/pnkx/notes",
            headers={"Authorization": f"Bearer {owner.access_token}"},
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d3",
                "title": "测试",
                "content": "测试",
            },
        )
        diary = await client.post(
            "/api/v1/pnkx/diaries",
            headers={"Authorization": f"Bearer {owner.access_token}"},
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d4",
                "title": "测试",
                "content": "测试",
                "entry_date": "2026-09-03",
            },
        )
        subscription = await client.post(
            "/api/v1/pnkx/subscriptions",
            headers={"Authorization": f"Bearer {owner.access_token}"},
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d5",
                "name": "云服务",
                "amount": "20.00",
                "next_payment_date": "2026-10-01",
                "account_id": 2,
                "classification_id": 3,
            },
        )

    assert bookkeeping.status_code == 503
    assert note.status_code == 503
    assert diary.status_code == 503
    assert subscription.status_code == 503
    assert fake.bookkeeping_operations == []
    assert fake.note_operations == []
    assert fake.diary_operations == []
    assert fake.subscription_operations == []


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
    assert created.json()["client_uuid"] == "018f7f4489d27cc8bc198f51f522a4d2"
    assert fake.commemoration_operations[0]["payload"]["date"] == (
        "2026-09-02 18:30:00"
    )
    assert marked.status_code == 200
    assert marked.json() == {"success": True}


async def test_note_and_diary_api_reads_and_creates(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="content owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        notes = await client.get("/api/v1/pnkx/notes?folder_id=4", headers=headers)
        folders = await client.get("/api/v1/pnkx/notes/folders", headers=headers)
        diaries = await client.get(
            "/api/v1/pnkx/diaries?month=2026-09", headers=headers
        )
        note = await client.post(
            "/api/v1/pnkx/notes",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d3",
                "title": "旅行清单",
                "content": "证件、充电器",
                "folder_id": 4,
                "order": 2,
            },
        )
        diary = await client.post(
            "/api/v1/pnkx/diaries",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d4",
                "title": "晴天",
                "content": "今天很好。",
                "entry_date": "2026-09-03",
                "mood": "开心",
                "weather": "晴",
            },
        )

    assert notes.json()["total"] == 1
    assert folders.json()["items"][0]["id"] == 4
    assert diaries.json()["items"][0]["title"] == "晴天"
    assert note.status_code == 201
    assert note.json()["remote_id"] == "83"
    assert note.json()["client_uuid"] == "018f7f4489d27cc8bc198f51f522a4d3"
    assert diary.status_code == 201
    assert diary.json()["remote_id"] == "84"
    assert fake.diary_operations[0]["payload"]["date"] == "2026-09-03"


async def test_subscription_api_reads_forecast_and_creates(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="subscription owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        subscriptions = await client.get(
            "/api/v1/pnkx/subscriptions?enabled=true", headers=headers
        )
        forecast = await client.get(
            "/api/v1/pnkx/subscriptions/forecast", headers=headers
        )
        created = await client.post(
            "/api/v1/pnkx/subscriptions",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d5",
                "name": "云服务",
                "amount": "20.00",
                "cycle": "monthly",
                "cycle_interval": 1,
                "next_payment_date": "2026-10-01",
                "account_id": 2,
                "classification_id": 3,
                "reminder_lead_days": 3,
            },
        )

    assert subscriptions.json()["total"] == 1
    assert forecast.json()["data"]["monthlyTotal"] == 20
    assert created.status_code == 201
    assert created.json()["remote_id"] == "85"
    assert created.json()["client_uuid"] == "018f7f4489d27cc8bc198f51f522a4d5"


async def test_shopping_api_reads_and_creates_lists_and_items(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="shopping owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        lists = await client.get("/api/v1/pnkx/shopping/lists", headers=headers)
        items = await client.get(
            "/api/v1/pnkx/shopping/lists/51/items?checked=false", headers=headers
        )
        created_list = await client.post(
            "/api/v1/pnkx/shopping/lists",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d6",
                "name": "日用品",
                "icon": "cart",
                "order_num": 2,
            },
        )
        created_item = await client.post(
            "/api/v1/pnkx/shopping/lists/51/items",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d7",
                "name": "纸巾",
                "quantity": "2包",
                "sort_order": 3,
            },
        )

    assert lists.json()["total"] == 1
    assert items.json()["items"][0]["listId"] == 51
    assert created_list.status_code == 201
    assert created_list.json()["remote_id"] == "86"
    assert created_item.status_code == 201
    assert created_item.json()["remote_id"] == "87"


async def test_recipe_and_meal_plan_api_reads_and_creates(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="meal owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        recipes = await client.get("/api/v1/pnkx/recipes?servings=2", headers=headers)
        meals = await client.get(
            "/api/v1/pnkx/meal-plans?plan_date=2026-09-04&meal_type=2",
            headers=headers,
        )
        recipe = await client.post(
            "/api/v1/pnkx/recipes",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d8",
                "title": "番茄炒蛋",
                "servings": 2,
                "ingredients": [{"name": "番茄", "quantity": "2个"}],
            },
        )
        meal = await client.post(
            "/api/v1/pnkx/meal-plans",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4d9",
                "plan_date": "2026-09-04",
                "meal_type": 2,
                "title": "番茄炒蛋",
                "recipe_id": 61,
            },
        )

    assert recipes.status_code == 200, recipes.text
    assert meals.status_code == 200, meals.text
    assert recipe.status_code == 201, recipe.text
    assert meal.status_code == 201, meal.text
    assert recipes.json()["total"] == 1
    assert meals.json()["items"][0]["planDate"] == "2026-09-04"
    assert recipe.status_code == 201
    assert recipe.json()["remote_id"] == "88"
    assert meal.status_code == 201
    assert meal.json()["remote_id"] == "89"


async def test_todo_api_reads_kanban_labels_and_creates(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="todo owner", password="correct horse")
    fake = FakePnkxLife()
    app = FastAPI()
    app.include_router(create_pnkx_router(_client(fake), auth, writes_enabled=True))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        todos = await client.get(
            "/api/v1/pnkx/todos?completed=false&priority=3&kanban_status=0",
            headers=headers,
        )
        kanban = await client.get("/api/v1/pnkx/todos/kanban", headers=headers)
        labels = await client.get("/api/v1/pnkx/todos/labels", headers=headers)
        created = await client.post(
            "/api/v1/pnkx/todos",
            headers=headers,
            json={
                "idempotency_key": "018f7f44-89d2-7cc8-bc19-8f51f522a4da",
                "content": "整理联调清单",
                "plan_start_time": "2026-09-04T09:00:00",
                "plan_end_time": "2026-09-04T10:00:00",
                "label": "工作",
                "priority": 3,
                "kanban_status": 0,
                "sort_order": 1,
            },
        )

    assert todos.status_code == 200, todos.text
    assert kanban.status_code == 200, kanban.text
    assert labels.status_code == 200, labels.text
    assert created.status_code == 201, created.text
    assert todos.json()["items"][0]["content"] == "整理联调清单"
    assert kanban.json()["data"]["todo"][0]["id"] == 71
    assert labels.json()["items"] == ["工作", "生活"]
    assert created.json()["remote_id"] == "90"
    assert fake.todo_operations[0]["planEndTime"] == "2026-09-04 10:00:00"
