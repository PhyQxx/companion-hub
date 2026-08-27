from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Literal, TypedDict

from .contracts import ToolResult


@dataclass(slots=True)
class ToolLedgerEntry:
    """脱敏后的工具调用摘要。不保留 Key、原始地址、精确坐标或完整查询结果。"""

    tool_name: str
    ok: bool
    provider: str | None
    latency_ms: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    reason_code: str | None = None
    cache_hit: bool = False
    location_source: Literal["explicit", "ephemeral", "default_city"] | None = None
    result_count: int | None = None


class ToolMetrics(TypedDict):
    total_calls: int
    success_rate: float | None
    p50_latency_ms: float | None
    p90_latency_ms: float | None
    cache_hit_rate: float | None
    failures: dict[str, int]


class ToolLedger:
    """内存滑窗工具台账，最多保留最近 200 次调用摘要。"""

    _instance: ClassVar[ToolLedger | None] = None
    _max_entries: ClassVar[int] = 200
    _entries: deque[ToolLedgerEntry]

    def __new__(cls) -> ToolLedger:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._entries = deque(maxlen=cls._max_entries)
        return cls._instance

    def record(self, result: ToolResult) -> None:
        """从 ToolResult 提取脱敏摘要并记录。"""
        result_count: int | None = None
        data = result.data
        if isinstance(data, dict):
            # 天气: current + forecast 条目数
            if "current" in data or "forecast" in data:
                forecast = data.get("forecast")
                result_count = (1 if data.get("current") is not None else 0) + (
                    len(forecast) if isinstance(forecast, list) else 0
                )
            # 附近搜索: results 列表长度
            elif "results" in data:
                results = data.get("results")
                result_count = len(results) if isinstance(results, list) else None
            # 路线: steps 列表长度
            elif "steps" in data:
                steps = data.get("steps")
                result_count = len(steps) if isinstance(steps, list) else None
            # Home Assistant: current state/history/logbook rows
            elif "entities" in data:
                entities = data.get("entities")
                result_count = len(entities) if isinstance(entities, list) else None
            elif "states" in data or "logbook" in data:
                states = data.get("states")
                logbook = data.get("logbook")
                result_count = (
                    len(states) if isinstance(states, list) else 0
                ) + (len(logbook) if isinstance(logbook, list) else 0)

        entry = ToolLedgerEntry(
            tool_name=result.tool_name,
            ok=result.ok,
            provider=result.provider,
            latency_ms=result.latency_ms,
            reason_code=result.reason_code,
            cache_hit=result.cache_hit,
            location_source=result.location_source,
            result_count=result_count,
        )
        self._entries.append(entry)

    def snapshot(self, *, provider: str | None = None) -> list[ToolLedgerEntry]:
        """返回当前台账的快照（ newest first ）。"""
        entries = reversed(self._entries)
        if provider is None:
            return list(entries)
        return [entry for entry in entries if entry.provider == provider]

    def metrics(self) -> ToolMetrics:
        """基于当前台账聚合延迟报告。"""
        entries = list(self._entries)
        total = len(entries)
        if total == 0:
            return {
                "total_calls": 0,
                "success_rate": None,
                "p50_latency_ms": None,
                "p90_latency_ms": None,
                "cache_hit_rate": None,
                "failures": {},
            }

        successes = [e for e in entries if e.ok]
        success_rate = len(successes) / total
        latencies = sorted(e.latency_ms for e in entries)
        p50 = latencies[int(total * 0.5)]
        p90 = latencies[int(total * 0.9)] if total >= 10 else latencies[-1]
        cache_hits = sum(1 for e in entries if e.cache_hit)
        cache_hit_rate = cache_hits / total

        failures: dict[str, int] = {}
        for e in entries:
            if not e.ok and e.reason_code:
                failures[e.reason_code] = failures.get(e.reason_code, 0) + 1

        return {
            "total_calls": total,
            "success_rate": round(success_rate, 4),
            "p50_latency_ms": round(p50, 2),
            "p90_latency_ms": round(p90, 2),
            "cache_hit_rate": round(cache_hit_rate, 4),
            "failures": failures,
        }

    def clear(self) -> None:
        self._entries.clear()
