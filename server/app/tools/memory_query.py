"""只读长期记忆核对工具：按编号、分区、关键词或事实槽位定向查询（批次九 D2）。

背景：业主在对话里问“你记得我吗/我是谁/记忆#2 是什么”时，语义检索可能给不出
证据级命中，且系统此前没有任何按编号或分区定向核对的通道，导致助手一口咬死
“没有记录”。本工具与管理后台共用同一 MemoryStore，编号即后台列表的 #id。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm import ToolDefinition
from app.memory.models import MemoryEntry, MemoryStatus, MemorySubjectKind
from app.memory.retrieval import DEFAULT_SUBJECT_SCOPES
from app.memory.store import MemoryStore
from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel, persistent_privacy_levels
from app.tools.contracts import ToolContext, ToolResult

# 关键词扫描窗口：按重要性取回后本地子串过滤，与管理检索候选上限同量级。
_SCAN_WINDOW = 500

_SCOPE_SUBJECTS: dict[str, tuple[MemorySubjectKind, str]] = {
    "user": (MemorySubjectKind.USER, "user:self"),
    "assistant": (MemorySubjectKind.ASSISTANT, "assistant:primary"),
    "shared": (MemorySubjectKind.SHARED, "shared:user-assistant"),
}
_SCOPE_LABELS = {
    MemorySubjectKind.USER.value: "关于用户",
    MemorySubjectKind.ASSISTANT.value: "关于你自己",
    MemorySubjectKind.SHARED.value: "关于你们",
}


class MemoryLookupArgs(StrictModel):
    memory_id: int | None = Field(default=None, ge=1)
    scope: Literal["user", "assistant", "shared"] | None = None
    keyword: str | None = Field(default=None, min_length=1, max_length=100)
    fact_key: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=10, ge=1, le=20)


class MemoryLookupTool:
    name = "memory_lookup"
    description = (
        "只读查询长期记忆库，按编号、分区、关键词或事实槽位定向核对。"
        "memory_id 即记忆编号（用户说“记忆#2”就传 2，与管理后台一致）；"
        "scope 分区：user=关于用户 / assistant=关于你自己 / shared=关于你们。"
        "用户问“你记得我吗/我是谁/你的记忆里有什么”，或对已注入记忆、记忆编号有疑问时，"
        "先用本工具核对再回答。只读，不写不改。"
    )
    arguments_model: type[BaseModel] = MemoryLookupArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=MemoryLookupArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = MemoryLookupArgs.model_validate(arguments.model_dump())
        if context.user_id is None:
            return self.result(False, reason="memory_lookup_unavailable")
        allowed = persistent_privacy_levels(context.privacy_level)
        if not allowed:
            return self.result(False, reason="privacy_no_persistent")
        if args.memory_id is not None:
            try:
                entry = await self._store.get(args.memory_id, user_id=context.user_id)
            except LookupError:
                return self.result(False, reason="memory_not_found")
            if not self._eligible(entry, allowed):
                # 隐私/分区外的条目对会话不可见，与检索层同样静默排除。
                return self.result(False, reason="memory_not_found")
            return self.result(True, {"items": [self._view(entry)], "total": 1})
        subject = _SCOPE_SUBJECTS.get(args.scope) if args.scope is not None else None
        entries = await self._store.list_memories(
            user_id=context.user_id,
            subject_kind=subject[0] if subject is not None else None,
            subject_key=subject[1] if subject is not None else None,
            fact_key=args.fact_key,
            status=MemoryStatus.ACTIVE,
            limit=_SCAN_WINDOW,
        )
        entries = [entry for entry in entries if self._eligible(entry, allowed)]
        if args.keyword is not None:
            keyword = args.keyword.strip().lower()
            entries = [
                entry
                for entry in entries
                if keyword in entry.content.lower()
                or (entry.summary is not None and keyword in entry.summary.lower())
            ]
        items = [self._view(entry) for entry in entries[: args.limit]]
        data: dict[str, Any] = {"items": items, "total": len(items)}
        if not items:
            data["note"] = (
                "未检索到符合条件的记录：如实告知未找到，不得断言从未记录过；"
                "可更换关键词或分区再查（关键词扫描按重要性前500条）。"
            )
        return self.result(True, data)

    def result(
        self, ok: bool, data: dict[str, Any] | None = None, *, reason: str | None = None
    ) -> ToolResult:
        return ToolResult(
            ok=ok, tool_name=self.name, data=data or {}, reason_code=reason, latency_ms=0
        )

    @staticmethod
    def _eligible(entry: MemoryEntry, allowed: Sequence[PrivacyLevel]) -> bool:
        return (
            entry.privacy_level in allowed
            and (entry.subject_kind, entry.subject_key) in DEFAULT_SUBJECT_SCOPES
        )

    @staticmethod
    def _view(entry: MemoryEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "scope": entry.subject_kind,
            "scope_label": _SCOPE_LABELS.get(entry.subject_kind, entry.subject_kind),
            "fact_key": entry.fact_key,
            "type": entry.type,
            "content": entry.content,
            "summary": entry.summary,
            "status": entry.status,
            "tier": entry.tier,
            "pin": entry.pin,
            "confidence": entry.confidence,
            "importance": entry.importance,
            "origin_kind": entry.origin_kind,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
