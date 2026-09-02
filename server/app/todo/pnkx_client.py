"""TODO-01 pnkx 待办 HTTP 客户端：集成令牌鉴权、分页拉取、幂等创建与更新。

pnkx（Spring Boot/RuoYi 系）契约：
- 鉴权：请求头 X-Integration-Token 与 pnkx.integration.token 匹配，
  由 pnkx 侧 IntegrationTokenFilter 绑定配置用户身份（无会话、无密码）；
- GET  /admin/toDo/list?pageNum&pageSize → TableDataInfo {code, rows, total}
- POST /admin/toDo (PxToDo JSON) → AjaxResult {code, data=id}
- PUT  /admin/toDo (PxToDo JSON) → AjaxResult
- 时间字符串格式 yyyy-MM-dd HH:mm:ss（本地时区 Asia/Shanghai）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("app.todo.pnkx")

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 200
MAX_PAGES = 50

DEFAULT_TIMEOUT_SECONDS = 15.0
INTEGRATION_TOKEN_HEADER = "X-Integration-Token"


class PnkxTodoError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(detail or reason_code)


@dataclass(frozen=True, slots=True)
class PnkxTodo:
    """pnkx PxToDo 的最小投影；子任务（parentId≠0）在同步时跳过。"""

    external_id: str
    content: str
    status: bool
    priority: int | None
    label: str | None
    plan_start: datetime | None
    plan_end: datetime | None
    kanban_status: int | None
    client_uuid: str | None
    updated_at: datetime | None
    is_subtask: bool


def _parse_time(raw: Any, tz: ZoneInfo) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, TIME_FORMAT).replace(tzinfo=tz)
    except ValueError:
        return None


class PnkxTodoClient:
    def __init__(
        self,
        *,
        base_url: str,
        integration_token: str,
        timezone_name: str = "Asia/Shanghai",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = integration_token
        self._tz = ZoneInfo(timezone_name)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.request(
            method,
            f"{self._base_url}{path}",
            json=json,
            params=params,
            headers={INTEGRATION_TOKEN_HEADER: self._token},
        )
        if response.status_code == 401:
            raise PnkxTodoError("integration_token_rejected")
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("code") == 401:
            raise PnkxTodoError("integration_token_rejected")
        if payload.get("code") != 200:
            raise PnkxTodoError("pnkx_rejected", str(payload.get("msg")))
        return payload

    async def list_all(self) -> list[PnkxTodo]:
        """全量分页拉取（增量接口未暴露；个人数据量下全量+本地比对即可）。"""
        items: list[PnkxTodo] = []
        for page in range(1, MAX_PAGES + 1):
            payload = await self._request(
                "GET",
                "/admin/toDo/list",
                params={"pageNum": page, "pageSize": PAGE_SIZE},
            )
            rows = payload.get("rows")
            if not isinstance(rows, list):
                raise PnkxTodoError("list_invalid", "rows is not a list")
            items.extend(self._parse_row(row) for row in rows if isinstance(row, dict))
            total = int(payload.get("total") or 0)
            if page * PAGE_SIZE >= total or not rows:
                break
        return items

    def _parse_row(self, row: dict[str, Any]) -> PnkxTodo:
        parent_id = row.get("parentId")
        return PnkxTodo(
            external_id=str(row.get("id")),
            content=str(row.get("content") or ""),
            status=bool(row.get("status")),
            priority=int(row["priority"]) if row.get("priority") is not None else None,
            label=str(row["label"]) if row.get("label") else None,
            plan_start=_parse_time(row.get("planStartTime"), self._tz),
            plan_end=_parse_time(row.get("planEndTime"), self._tz),
            kanban_status=(
                int(row["kanbanStatus"]) if row.get("kanbanStatus") is not None else None
            ),
            client_uuid=str(row["clientUuid"]) if row.get("clientUuid") else None,
            updated_at=_parse_time(row.get("updateTime"), self._tz)
            or _parse_time(row.get("createTime"), self._tz),
            is_subtask=bool(parent_id not in (None, 0, "0")),
        )

    async def create(
        self,
        *,
        content: str,
        client_uuid: str,
        plan_start: datetime | None = None,
        plan_end: datetime | None = None,
        priority: int | None = None,
        label: str | None = None,
    ) -> str:
        """创建待办并返回外部 id；clientUuid 由调用方保证幂等语义。"""
        body: dict[str, Any] = {"content": content, "clientUuid": client_uuid}
        if plan_start is not None:
            body["planStartTime"] = plan_start.astimezone(self._tz).strftime(TIME_FORMAT)
        if plan_end is not None:
            body["planEndTime"] = plan_end.astimezone(self._tz).strftime(TIME_FORMAT)
        if priority is not None:
            body["priority"] = priority
        if label:
            body["label"] = label
        payload = await self._request("POST", "/admin/toDo", json=body)
        external_id = payload.get("data") or payload.get("msg")
        if external_id is None:
            raise PnkxTodoError("create_no_id")
        return str(external_id)

    async def update(
        self,
        external_id: str,
        *,
        status: bool | None = None,
        finish_time: datetime | None = None,
        plan_start: datetime | None = None,
        plan_end: datetime | None = None,
        priority: int | None = None,
        content: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"id": int(external_id)}
        if status is not None:
            body["status"] = status
        if finish_time is not None:
            body["finishTime"] = finish_time.astimezone(self._tz).strftime(TIME_FORMAT)
        if plan_start is not None:
            body["planStartTime"] = plan_start.astimezone(self._tz).strftime(TIME_FORMAT)
        if plan_end is not None:
            body["planEndTime"] = plan_end.astimezone(self._tz).strftime(TIME_FORMAT)
        if priority is not None:
            body["priority"] = priority
        if content is not None:
            body["content"] = content
        await self._request("PUT", "/admin/toDo", json=body)
