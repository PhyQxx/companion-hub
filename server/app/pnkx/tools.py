"""Chat tools for reading and creating pnkx life data."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from typing import Annotated, Any, Literal, TypeVar, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition
from app.schemas import PrivacyLevel
from app.tools import ToolContext, ToolResult

from .client import PnkxApiError, PnkxLifeClient

T = TypeVar("T")

PnkxReadResource = Literal[
    "cockpit",
    "reminders",
    "notifications",
    "commemoration_days",
    "notes",
    "note_folders",
    "diaries",
    "subscriptions",
    "subscription_forecast",
    "shopping_lists",
    "shopping_items",
    "recipes",
    "meal_plans",
    "todos",
    "todo_kanban",
    "todo_labels",
    "bookkeeping_accounts",
    "bookkeeping_classifications",
    "bookkeeping_records",
]
PnkxCreateResource = Literal[
    "commemoration_day",
    "note",
    "diary",
    "subscription",
    "shopping_list",
    "shopping_item",
    "recipe",
    "meal_plan",
    "todo",
    "bookkeeping_record",
]


class PnkxReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: PnkxReadResource
    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=50)] = 20
    query: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    label: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    month: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")] | None = None
    plan_date: date | None = None
    meal_type: Annotated[int, Field(ge=1, le=4)] | None = None
    list_id: Annotated[int, Field(gt=0)] | None = None
    checked: bool | None = None
    enabled: bool | None = None
    completed: bool | None = None
    priority: Annotated[int, Field(ge=0, le=4)] | None = None
    kanban_status: Annotated[int, Field(ge=0, le=2)] | None = None
    folder_id: Annotated[int, Field(gt=0)] | None = None
    servings: Annotated[int, Field(gt=0, le=100)] | None = None
    type_difference: Literal["0", "1"] | None = None


class PnkxIngredientArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=255)]
    quantity: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    classification_id: Annotated[int, Field(gt=0)] | None = None


class PnkxCreateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: PnkxCreateResource
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    title: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    content: Annotated[str, Field(min_length=1, max_length=100_000)] | None = None
    event_time: datetime | None = None
    entry_date: date | None = None
    plan_date: date | None = None
    pay_time: datetime | None = None
    next_payment_date: date | None = None
    plan_start_time: datetime | None = None
    plan_end_time: datetime | None = None
    repeat: bool = True
    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)] | None = None
    account_id: Annotated[int, Field(gt=0)] | None = None
    classification_id: Annotated[int, Field(gt=0)] | None = None
    folder_id: Annotated[int, Field(gt=0)] | None = None
    list_id: Annotated[int, Field(gt=0)] | None = None
    recipe_id: Annotated[int, Field(gt=0)] | None = None
    meal_type: Annotated[int, Field(ge=1, le=4)] | None = None
    servings: Annotated[int, Field(gt=0, le=100)] = 1
    ingredients: Annotated[list[PnkxIngredientArgs], Field(max_length=100)] = Field(
        default_factory=list
    )
    quantity: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    cycle: Literal["daily", "weekly", "monthly", "yearly"] = "monthly"
    cycle_interval: Annotated[int, Field(gt=0, le=365)] = 1
    reminder_lead_days: Annotated[int, Field(ge=0, le=365)] = 0
    enabled: bool = True
    checked: bool = False
    completed: bool = False
    label: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    priority: Annotated[int, Field(ge=0, le=4)] = 0
    kanban_status: Annotated[int, Field(ge=0, le=2)] = 0
    sort_order: Annotated[int, Field(ge=0)] | None = None
    order_num: Annotated[int, Field(ge=0)] | None = None
    mood: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    weather: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    url: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    notes: Annotated[str, Field(max_length=10_000)] | None = None
    remark: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


class PnkxReadTool:
    name = "pnkx_read_life"
    description = (
        "读取用户在 pnkx 中的生活数据。resource 可选择待办、待办看板/标签、菜谱、餐食计划、"
        "购物清单/条目、订阅、账本、笔记/文件夹、日记、纪念日、提醒或通知。query 是名称、"
        "标题或内容关键词；读取购物条目时必须提供 list_id。"
    )
    arguments_model: type[BaseModel] = PnkxReadArgs
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, client: PnkxLifeClient, *, runs_local: bool) -> None:
        self._client = client
        self.runs_local = runs_local

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=PnkxReadArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        del context
        started = perf_counter()
        args = cast(PnkxReadArgs, arguments)
        try:
            data = await self._read(args)
        except PnkxApiError as error:
            return _failure(self.name, error.reason_code, started)
        except ValueError as error:
            return _failure(self.name, str(error), started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="pnkx",
            data={"resource": args.resource, **data},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def _read(self, args: PnkxReadArgs) -> dict[str, Any]:
        if args.resource == "cockpit":
            return {"value": await self._client.cockpit()}
        if args.resource == "reminders":
            return {"value": await self._client.today_reminders()}
        if args.resource == "notifications":
            return {"items": await self._client.notifications()}
        if args.resource == "note_folders":
            return {"items": await self._client.note_folders()}
        if args.resource == "subscription_forecast":
            return {"value": await self._client.subscription_forecast()}
        if args.resource == "todo_kanban":
            return {"value": await self._client.todo_kanban()}
        if args.resource == "todo_labels":
            return {"labels": await self._client.todo_labels()}
        if args.resource == "bookkeeping_accounts":
            return {"items": await self._client.bookkeeping_accounts()}
        if args.resource == "bookkeeping_classifications":
            return {
                "items": await self._client.bookkeeping_classifications(
                    type_difference=args.type_difference
                )
            }
        if args.resource == "commemoration_days":
            commemoration_page = await self._client.commemoration_days(
                page=args.page, page_size=args.page_size, name=args.query
            )
            return {
                "items": commemoration_page.items,
                "total": commemoration_page.total,
            }
        elif args.resource == "notes":
            notes_page = await self._client.notes(
                page=args.page,
                page_size=args.page_size,
                title=args.query,
                folder_id=args.folder_id,
            )
            return {"items": notes_page.items, "total": notes_page.total}
        elif args.resource == "diaries":
            diaries_page = await self._client.diaries(
                page=args.page,
                page_size=args.page_size,
                title=args.query,
                month=args.month,
            )
            return {"items": diaries_page.items, "total": diaries_page.total}
        elif args.resource == "subscriptions":
            subscriptions_page = await self._client.subscriptions(
                page=args.page,
                page_size=args.page_size,
                name=args.query,
                enabled=args.enabled,
            )
            return {
                "items": subscriptions_page.items,
                "total": subscriptions_page.total,
            }
        elif args.resource == "shopping_lists":
            shopping_lists_page = await self._client.shopping_lists(
                page=args.page, page_size=args.page_size, name=args.query
            )
            return {
                "items": shopping_lists_page.items,
                "total": shopping_lists_page.total,
            }
        elif args.resource == "shopping_items":
            if args.list_id is None:
                raise ValueError("pnkx_list_id_required")
            shopping_items_page = await self._client.shopping_items(
                list_id=args.list_id,
                page=args.page,
                page_size=args.page_size,
                checked=args.checked,
            )
            return {
                "items": shopping_items_page.items,
                "total": shopping_items_page.total,
            }
        elif args.resource == "recipes":
            recipes_page = await self._client.recipes(
                page=args.page,
                page_size=args.page_size,
                title=args.query,
                servings=args.servings,
            )
            return {"items": recipes_page.items, "total": recipes_page.total}
        elif args.resource == "meal_plans":
            meal_plans_page = await self._client.meal_plans(
                page=args.page,
                page_size=args.page_size,
                plan_date=args.plan_date.isoformat() if args.plan_date else None,
                meal_type=args.meal_type,
            )
            return {"items": meal_plans_page.items, "total": meal_plans_page.total}
        elif args.resource == "todos":
            todos_page = await self._client.todos(
                page=args.page,
                page_size=args.page_size,
                search=args.query,
                completed=args.completed,
                label=args.label,
                priority=args.priority,
                kanban_status=args.kanban_status,
            )
            return {"items": todos_page.items, "total": todos_page.total}
        elif args.resource == "bookkeeping_records":
            records = await self._client.bookkeeping_records(
                page=args.page,
                page_size=args.page_size,
                month=args.month,
                search=args.query,
            )
            return {
                "items": records.items,
                "total": records.total,
                "inflow": records.inflow,
                "outflow": records.outflow,
            }
        else:
            raise ValueError("pnkx_resource_unsupported")


class PnkxCreateTool:
    name = "pnkx_create_life"
    description = (
        "仅在用户明确要求新增时，向 pnkx 创建生活数据。resource 支持待办、菜谱、餐食计划、"
        "购物清单/条目、订阅、账本、笔记、日记和纪念日。根据资源填写对应字段；不要猜测"
        "账户、分类、清单或菜谱 ID，缺少这些 ID 时先用 pnkx_read_life 查询。"
    )
    arguments_model: type[BaseModel] = PnkxCreateArgs
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, client: PnkxLifeClient, *, runs_local: bool) -> None:
        self._client = client
        self.runs_local = runs_local

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=PnkxCreateArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(PnkxCreateArgs, arguments)
        if context.turn_id is None:
            return _failure(self.name, "pnkx_idempotency_key_missing", started)
        try:
            remote_id = await self._create(args, context.turn_id.hex)
        except PnkxApiError as error:
            return _failure(self.name, error.reason_code, started)
        except ValueError as error:
            return _failure(self.name, str(error), started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="pnkx",
            data={"resource": args.resource, "remote_id": remote_id},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def _create(self, args: PnkxCreateArgs, client_uuid: str) -> str:
        if args.resource == "todo":
            return await self._client.create_todo(
                client_uuid=client_uuid,
                content=_required(args.content, "pnkx_content_required"),
                plan_start_time=(
                    _format_datetime(args.plan_start_time)
                    if args.plan_start_time is not None
                    else None
                ),
                plan_end_time=(
                    _format_datetime(args.plan_end_time)
                    if args.plan_end_time is not None
                    else None
                ),
                completed=args.completed,
                label=args.label,
                priority=args.priority,
                kanban_status=args.kanban_status,
                sort_order=args.sort_order,
                remark=args.remark,
            )
        if args.resource == "recipe":
            return await self._client.create_recipe(
                client_uuid=client_uuid,
                title=_required(args.title, "pnkx_title_required"),
                servings=args.servings,
                ingredients=[
                    {
                        key: value
                        for key, value in {
                            "name": item.name,
                            "quantity": item.quantity,
                            "classificationId": item.classification_id,
                        }.items()
                        if value is not None
                    }
                    for item in args.ingredients
                ],
                url=args.url,
                notes=args.notes,
                remark=args.remark,
            )
        if args.resource == "meal_plan":
            return await self._client.create_meal_plan(
                client_uuid=client_uuid,
                plan_date=_required(args.plan_date, "pnkx_plan_date_required").isoformat(),
                meal_type=_required(args.meal_type, "pnkx_meal_type_required"),
                title=_required(args.title, "pnkx_title_required"),
                recipe_id=args.recipe_id,
                notes=args.notes,
                sort_order=args.sort_order,
                remark=args.remark,
            )
        if args.resource == "shopping_list":
            return await self._client.create_shopping_list(
                client_uuid=client_uuid,
                name=_required(args.name, "pnkx_name_required"),
                order_num=args.order_num,
                remark=args.remark,
            )
        if args.resource == "shopping_item":
            return await self._client.create_shopping_item(
                client_uuid=client_uuid,
                list_id=_required(args.list_id, "pnkx_list_id_required"),
                name=_required(args.name, "pnkx_name_required"),
                quantity=args.quantity,
                classification_id=args.classification_id,
                checked=args.checked,
                sort_order=args.sort_order,
                remark=args.remark,
            )
        if args.resource == "subscription":
            return await self._client.create_subscription(
                client_uuid=client_uuid,
                name=_required(args.name, "pnkx_name_required"),
                amount=str(_required(args.amount, "pnkx_amount_required")),
                cycle=args.cycle,
                cycle_interval=args.cycle_interval,
                next_payment_date=_required(
                    args.next_payment_date, "pnkx_next_payment_date_required"
                ).isoformat(),
                account_id=_required(args.account_id, "pnkx_account_id_required"),
                classification_id=_required(
                    args.classification_id, "pnkx_classification_id_required"
                ),
                reminder_lead_days=args.reminder_lead_days,
                enabled=args.enabled,
                remark=args.remark,
            )
        if args.resource == "note":
            return await self._client.create_note(
                client_uuid=client_uuid,
                title=_required(args.title, "pnkx_title_required"),
                content=_required(args.content, "pnkx_content_required"),
                folder_id=args.folder_id,
                remark=args.remark,
            )
        if args.resource == "diary":
            return await self._client.create_diary(
                client_uuid=client_uuid,
                title=_required(args.title, "pnkx_title_required"),
                content=_required(args.content, "pnkx_content_required"),
                entry_date=_required(args.entry_date, "pnkx_entry_date_required").isoformat(),
                mood=args.mood,
                weather=args.weather,
                remark=args.remark,
            )
        if args.resource == "commemoration_day":
            return await self._client.create_commemoration_day(
                client_uuid=client_uuid,
                name=_required(args.name, "pnkx_name_required"),
                event_time=_format_datetime(
                    _required(args.event_time, "pnkx_event_time_required")
                ),
                repeat=args.repeat,
                order_num=args.order_num,
                remark=args.remark,
            )
        if args.resource == "bookkeeping_record":
            return await self._client.create_bookkeeping_record(
                client_uuid=client_uuid,
                account_id=_required(args.account_id, "pnkx_account_id_required"),
                classification_id=_required(
                    args.classification_id, "pnkx_classification_id_required"
                ),
                amount=str(_required(args.amount, "pnkx_amount_required")),
                pay_time=_format_datetime(_required(args.pay_time, "pnkx_pay_time_required")),
                remark=args.remark,
            )
        raise ValueError("pnkx_resource_unsupported")


def pnkx_runs_local(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _required(value: T | None, reason_code: str) -> T:
    if value is None:
        raise ValueError(reason_code)
    return value


def _format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _failure(tool_name: str, reason_code: str, started: float) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        provider="pnkx",
        reason_code=reason_code,
        latency_ms=(perf_counter() - started) * 1_000,
    )
