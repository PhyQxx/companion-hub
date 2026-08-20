# ruff: noqa: RUF002, RUF003
"""记忆系统的领域模型：类型、状态、候选与视图对象。

分层约定：
- Pydantic 模型（StrictModel）用于 API 边界与提取器输出，禁止多余字段；
- frozen dataclass 用于服务层只读视图，避免 ORM 对象泄漏到业务逻辑之外。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.schemas.common import PrivacyLevel, StrictModel


class MemoryType(StrEnum):
    """长期记忆的五种内容类型；主体由 MemorySubjectKind 独立表达。"""

    EPISODIC = "episodic"  # 情景记忆：带时间戳的对话要点、事件经历
    SEMANTIC = "semantic"  # 语义记忆：主体的稳定事实
    PREFERENCE = "preference"  # 偏好记忆：主体的长期偏好
    COMMITMENT = "commitment"  # 承诺记忆：用户/助手/双方的约定（可带有效期）
    EMOTIONAL = "emotional"  # 情感记忆：关系里程碑、共同情绪事件


class MemorySubjectKind(StrEnum):
    """记忆描述的主体；user_id 仍然是数据所有权/权限边界。"""

    USER = "user"
    ASSISTANT = "assistant"
    SHARED = "shared"


class MemoryOriginKind(StrEnum):
    """记忆事实的原始证据类型。"""

    USER_STATEMENT = "user_statement"
    ASSISTANT_STATEMENT = "assistant_statement"
    SHARED_TURN = "shared_turn"
    SYSTEM_EVENT = "system_event"
    MANUAL = "manual"


class MemoryStatus(StrEnum):
    """记忆状态机：

    - active：参与检索的正常状态；
    - archived：归档退出检索，可恢复；
    - superseded：已被纠错版本替代，仅保留用于溯源；
    - conflict：与现有记忆疑似冲突，挂起等待用户裁决（adopt / keep）。
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"


class MemorySourceKind(StrEnum):
    """记忆来源的种类：来源只存引用 ID 与哈希，不复制原文。"""

    MESSAGE = "message"  # 源自某条聊天消息
    EVENT = "event"  # 源自某个事件（预留给感知引擎）
    MEMORY = "memory"  # 源自另一条记忆（纠错链路的衍生关系）
    MANUAL = "manual"  # 管理后台手动添加


class ConsolidateDecision(StrEnum):
    """沉淀判定结果：新增 / 支持 / 冲突；"替代"只由显式操作触发。"""

    CREATED = "created"
    SUPPORTED = "supported"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"


class MemorySourceRef(StrictModel):
    """候选记忆携带的来源引用；excerpt 只用于计算哈希，不落库明文。"""

    source_kind: MemorySourceKind
    source_id: Annotated[str, Field(min_length=1, max_length=200)]
    excerpt: Annotated[str, Field(max_length=2_000)] | None = None


class MemoryCandidate(StrictModel):
    """待沉淀的记忆候选：由提取器（规则或 LLM）产出，经判定后入库。"""

    subject_kind: MemorySubjectKind = MemorySubjectKind.USER
    subject_key: Annotated[str, Field(min_length=1, max_length=160)] = "user:self"
    fact_key: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    origin_kind: MemoryOriginKind = MemoryOriginKind.USER_STATEMENT
    type: MemoryType
    content: Annotated[str, Field(min_length=2, max_length=2_000)]
    privacy_level: PrivacyLevel
    sources: list[MemorySourceRef] = Field(default_factory=list, max_length=16)
    importance: float = 0.5  # 重要性 0~1，参与检索重排
    pin: bool = False  # 置顶：检索时获得加分，不受类型配额挤占
    confidence: float | None = None  # 提取器给出的置信度
    summary: Annotated[str, Field(max_length=2_000)] | None = None
    extractor_version: Annotated[str, Field(max_length=64)] = "rule-v1"  # 提取器版本，用于溯源
    valid_from: datetime | None = None  # 有效期起点，缺省为入库时间
    valid_to: datetime | None = None  # 有效期终点，过期后退出检索


class ExtractedCandidate(StrictModel):
    """LLM 提取器输出 JSON 中单条候选的 schema（严格校验）。"""

    type: MemoryType
    content: Annotated[str, Field(min_length=2, max_length=200)]
    importance: float = 0.5
    confidence: float | None = None


class ExtractedCandidates(StrictModel):
    """LLM 提取器输出的整体结构：{"candidates": [...]}。"""

    candidates: list[ExtractedCandidate] = Field(default_factory=list, max_length=16)


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """记忆的服务层只读视图（不含嵌入向量本体，避免大对象传递）。"""

    id: int
    user_id: UUID
    subject_kind: str
    subject_key: str
    fact_key: str | None
    origin_kind: str
    type: str
    content: str
    summary: str | None
    privacy_level: str
    importance: float
    pin: bool
    status: str
    confidence: float | None
    valid_from: datetime | None
    valid_to: datetime | None
    superseded_by: int | None  # 指向替代自己的新版本（纠错链）
    supersede_reason: str | None  # 被替代 / 归档的原因
    conflict_with: int | None  # 冲突挂起时指向的对立记忆 ID
    extractor_version: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    access_count: int
    # 嵌入三元组：检索时按版本隔离，绝不跨模型比较向量
    embedding_model: str | None
    embedding_dimension: int | None
    embedding_version: str | None


@dataclass(frozen=True, slots=True)
class MemorySourceEntry:
    """已落库的来源行视图。"""

    source_kind: str
    source_id: str
    excerpt_hash: str | None


@dataclass(frozen=True, slots=True)
class SimilarMemory:
    """同类型相似检索的命中项及其相似度。"""

    entry: MemoryEntry
    similarity: float


@dataclass(frozen=True, slots=True)
class ConsolidateOutcome:
    """一次沉淀判定的结果：决策、最终记忆与相关（支持/冲突）对象。"""

    decision: ConsolidateDecision
    memory: MemoryEntry
    related: MemoryEntry | None


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """硬删除回执：台账行 ID、入口实体与被删除的全部版本 ID。"""

    ledger_id: int
    entity_id: str
    deleted_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeletionLedgerEntry:
    """删除台账行视图：只含标识符与原因，绝不含被删内容。"""

    id: int
    entity_kind: str
    entity_id: str
    deleted_ids: tuple[int, ...]
    requested_by: str
    reason: str | None
    created_at: datetime
