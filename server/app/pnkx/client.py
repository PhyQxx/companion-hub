"""pnkx 生活数据查询与受控写入客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

INTEGRATION_TOKEN_HEADER = "X-Integration-Token"
DEFAULT_TIMEOUT_SECONDS = 15.0


class PnkxApiError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(detail or reason_code)


@dataclass(frozen=True, slots=True)
class PnkxBookkeepingPage:
    items: list[dict[str, Any]]
    total: int
    inflow: str | None
    outflow: str | None


@dataclass(frozen=True, slots=True)
class PnkxCommemorationPage:
    items: list[dict[str, Any]]
    total: int


@dataclass(frozen=True, slots=True)
class PnkxContentPage:
    items: list[dict[str, Any]]
    total: int


class PnkxLifeClient:
    def __init__(
        self,
        *,
        base_url: str,
        integration_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = integration_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_payload(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        response = await self._client.request(
            method,
            f"{self._base_url}{path}",
            params=params,
            json=json,
            headers={INTEGRATION_TOKEN_HEADER: self._token},
        )
        if response.status_code == 401:
            raise PnkxApiError("integration_token_rejected")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PnkxApiError("pnkx_invalid_response")
        if payload.get("code") == 401:
            raise PnkxApiError("integration_token_rejected")
        if payload.get("code") != 200:
            raise PnkxApiError("pnkx_rejected", str(payload.get("msg") or ""))
        return payload

    async def _get_data(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> Any:
        return (await self._request_payload("GET", path, params=params)).get("data")

    async def _post_data(self, path: str, *, json: dict[str, Any]) -> Any:
        return (await self._request_payload("POST", path, json=json)).get("data")

    async def cockpit(self) -> dict[str, Any]:
        data = await self._get_data("/calendar/cockpit")
        if not isinstance(data, dict):
            raise PnkxApiError("cockpit_invalid")
        return data

    async def month_events(
        self, *, start_date: date | None = None, end_date: date | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {}
        if start_date is not None:
            params["startDate"] = start_date.isoformat()
        if end_date is not None:
            params["endDate"] = end_date.isoformat()
        data = await self._get_data("/calendar/month", params=params or None)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise PnkxApiError("calendar_invalid")
        return data

    async def today_reminders(self) -> dict[str, Any]:
        data = await self._get_data("/reminder/today")
        if not isinstance(data, dict):
            raise PnkxApiError("reminders_invalid")
        return data

    async def notifications(self) -> list[dict[str, Any]]:
        data = await self._get_data("/reminder/notifications")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise PnkxApiError("notifications_invalid")
        return data

    async def unread_count(self) -> int:
        data = await self._get_data("/reminder/unread/count")
        if isinstance(data, bool) or not isinstance(data, int):
            raise PnkxApiError("unread_count_invalid")
        return int(data)

    async def mark_notifications_read(self, ids: list[int] | None = None) -> None:
        await self._request_payload("PUT", "/reminder/notifications/read", json=ids)

    async def commemoration_days(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        name: str | None = None,
    ) -> PnkxCommemorationPage:
        params: dict[str, str | int] = {"pageNum": page, "pageSize": page_size}
        if name is not None:
            params["name"] = name
        payload = await self._request_payload(
            "GET", "/commemorationDay/list", params=params
        )
        items = self._object_list(
            payload.get("rows"), "commemoration_days_invalid"
        )
        total = payload.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise PnkxApiError("commemoration_total_invalid")
        return PnkxCommemorationPage(items=items, total=total)

    async def create_commemoration_day(
        self,
        *,
        client_uuid: str,
        name: str,
        event_time: str,
        repeat: bool,
        icon: str | None = None,
        order_num: int | None = None,
        remark: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "name": name,
            "date": event_time,
            "isRepeat": repeat,
        }
        if icon is not None:
            payload["icon"] = icon
        if order_num is not None:
            payload["orderNum"] = order_num
        if remark is not None:
            payload["remark"] = remark
        return await self._offline_create(
            table_name="px_commemoration_day",
            client_uuid=client_uuid,
            payload=payload,
            reason_prefix="commemoration_create",
        )

    async def notes(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        title: str | None = None,
        folder_id: int | None = None,
    ) -> PnkxContentPage:
        params: dict[str, str | int] = {"pageNum": page, "pageSize": page_size}
        if title is not None:
            params["title"] = title
        if folder_id is not None:
            params["folder"] = folder_id
        return await self._content_page(
            "/note/list", params=params, reason_prefix="notes"
        )

    async def note_folders(self) -> list[dict[str, Any]]:
        data = await self._get_data("/note/folder/treeList")
        return self._object_list(data, "note_folders_invalid")

    async def create_note(
        self,
        *,
        client_uuid: str,
        title: str,
        content: str,
        rich_text: str | None = None,
        folder_id: int | None = None,
        order: int | None = None,
        remark: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"title": title, "content": content}
        if rich_text is not None:
            payload["richText"] = rich_text
        if folder_id is not None:
            payload["folder"] = folder_id
        if order is not None:
            payload["order"] = order
        if remark is not None:
            payload["remark"] = remark
        return await self._offline_create(
            table_name="px_note",
            client_uuid=client_uuid,
            payload=payload,
            reason_prefix="note_create",
        )

    async def diaries(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        title: str | None = None,
        mood: str | None = None,
        weather: str | None = None,
        month: str | None = None,
    ) -> PnkxContentPage:
        params: dict[str, str | int] = {"pageNum": page, "pageSize": page_size}
        if title is not None:
            params["title"] = title
        if mood is not None:
            params["mood"] = mood
        if weather is not None:
            params["weather"] = weather
        if month is not None:
            params["date"] = f"{month}-01"
        return await self._content_page(
            "/admin/diary/list", params=params, reason_prefix="diaries"
        )

    async def create_diary(
        self,
        *,
        client_uuid: str,
        title: str,
        content: str,
        entry_date: str,
        mood: str | None = None,
        weather: str | None = None,
        rich_text: str | None = None,
        remark: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "date": entry_date,
        }
        if mood is not None:
            payload["mood"] = mood
        if weather is not None:
            payload["weather"] = weather
        if rich_text is not None:
            payload["richText"] = rich_text
        if remark is not None:
            payload["remark"] = remark
        return await self._offline_create(
            table_name="px_diary",
            client_uuid=client_uuid,
            payload=payload,
            reason_prefix="diary_create",
        )

    async def _content_page(
        self,
        path: str,
        *,
        params: dict[str, str | int],
        reason_prefix: str,
    ) -> PnkxContentPage:
        payload = await self._request_payload("GET", path, params=params)
        items = self._object_list(payload.get("rows"), f"{reason_prefix}_invalid")
        total = payload.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise PnkxApiError(f"{reason_prefix}_total_invalid")
        return PnkxContentPage(items=items, total=total)

    async def bookkeeping_accounts(self) -> list[dict[str, Any]]:
        data = await self._get_data("/bookkeeping/account/getAccountList")
        return self._object_list(data, "bookkeeping_accounts_invalid")

    async def bookkeeping_classifications(
        self, *, type_difference: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] | None = (
            {"typeDifference": type_difference} if type_difference is not None else None
        )
        data = await self._get_data(
            "/bookkeeping/classification/getClassificationList", params=params
        )
        return self._object_list(data, "bookkeeping_classifications_invalid")

    async def bookkeeping_records(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        month: str | None = None,
        search: str | None = None,
    ) -> PnkxBookkeepingPage:
        params: dict[str, str | int] = {"pageNum": page, "pageSize": page_size}
        if month is not None:
            params["payTime"] = month
        if search is not None:
            params["searchValue"] = search
        payload = await self._request_payload(
            "GET", "/bookkeeping/record/list", params=params
        )
        items = self._object_list(payload.get("rows"), "bookkeeping_records_invalid")
        total = payload.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise PnkxApiError("bookkeeping_total_invalid")
        raw_totals = str(payload.get("msg") or "").split(",", maxsplit=1)
        inflow = raw_totals[0] if raw_totals and raw_totals[0] else None
        outflow = raw_totals[1] if len(raw_totals) == 2 and raw_totals[1] else None
        return PnkxBookkeepingPage(
            items=items, total=total, inflow=inflow, outflow=outflow
        )

    async def create_bookkeeping_record(
        self,
        *,
        client_uuid: str,
        account_id: int,
        classification_id: int,
        amount: str,
        pay_time: str,
        remark: str | None = None,
    ) -> str:
        record: dict[str, Any] = {
            "account": account_id,
            "type": classification_id,
            "money": amount,
            "payTime": pay_time,
        }
        if remark:
            record["remark"] = remark
        return await self._offline_create(
            table_name="px_bookkeeping_record",
            client_uuid=client_uuid,
            payload=record,
            reason_prefix="bookkeeping_create",
        )

    async def _offline_create(
        self,
        *,
        table_name: str,
        client_uuid: str,
        payload: dict[str, Any],
        reason_prefix: str,
    ) -> str:
        data = await self._post_data(
            "/offline/batch",
            json={
                "operations": [
                    {
                        "tableName": table_name,
                        "method": "POST",
                        "clientUuid": client_uuid,
                        "payload": payload,
                    }
                ]
            },
        )
        if not isinstance(data, dict):
            raise PnkxApiError(f"{reason_prefix}_invalid")
        results = data.get("results")
        if not isinstance(results, list) or len(results) != 1:
            raise PnkxApiError(f"{reason_prefix}_invalid")
        result = results[0]
        if not isinstance(result, dict) or result.get("status") not in {"success", "skip"}:
            raise PnkxApiError(f"{reason_prefix}_failed", str(result))
        remote_id = result.get("id")
        if remote_id is None:
            raise PnkxApiError(f"{reason_prefix}_no_id")
        return str(remote_id)

    async def bookkeeping_primary_statistics(
        self, *, month: str, type_difference: str
    ) -> list[dict[str, Any]]:
        data = await self._post_data(
            "/bookkeeping/statistics/getPrimaryStatistics",
            json={"date": month, "typeDifference": type_difference},
        )
        return self._object_list(data, "bookkeeping_statistics_invalid")

    async def bookkeeping_monthly_statistics(
        self, *, year: int, type_difference: str
    ) -> list[dict[str, Any]]:
        data = await self._post_data(
            "/bookkeeping/statistics/getMonthlyStatistics",
            json={"date": str(year), "typeDifference": type_difference},
        )
        return self._object_list(data, "bookkeeping_statistics_invalid")

    @staticmethod
    def _object_list(data: Any, reason_code: str) -> list[dict[str, Any]]:
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise PnkxApiError(reason_code)
        return data
