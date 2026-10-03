"""Long-term memory system: retrieval, traceable sources and correction."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .consolidation import ConsolidateOutcome, ConsolidationPolicy, MemoryIngester
    from .consolidation_core import CandidateConsolidator
    from .embeddings import (
        EmbeddingProvider,
        HashingEmbeddingProvider,
        HttpEmbeddingProvider,
        build_embedding_provider,
        cosine_similarity,
        lexical_cosine,
        probe_embedding_provider,
        text_tokens,
    )
    from .extraction import (
        LLM_EXTRACTOR_VERSION,
        RULE_EXTRACTOR_VERSION,
        ExtractionBackend,
        LlmMemoryExtractor,
        MemoryExtractor,
        RuleBasedExtractor,
        TurnMemoryExtractor,
        extract_assistant_fact_assertions,
        extraction_instruction,
    )
    from .extraction_core import StructuredMemoryExtractor
    from .models import (
        ConsolidateDecision,
        DeletionLedgerEntry,
        DeletionReceipt,
        ExtractedCandidates,
        MemoryCandidate,
        MemoryEntry,
        MemoryOriginKind,
        MemorySourceEntry,
        MemorySourceKind,
        MemorySourceRef,
        MemoryStatus,
        MemorySubjectKind,
        MemoryType,
        SimilarMemory,
    )
    from .replay import ReplayReport, replay_deletions
    from .retrieval import (
        DEFAULT_SUBJECT_SCOPES,
        DEFAULT_TYPE_QUOTAS,
        RETRIEVAL_POLICY_VERSION,
        MemoryHit,
        MemoryRetriever,
        RetrievalPolicy,
        RetrievalResult,
    )
    from .store import MemoryStore
    from .turn_core import CompletedTurnMemoryExtractor

_EXPORTS = {
    "DEFAULT_SUBJECT_SCOPES": ("app.memory.retrieval", "DEFAULT_SUBJECT_SCOPES"),
    "DEFAULT_TYPE_QUOTAS": ("app.memory.retrieval", "DEFAULT_TYPE_QUOTAS"),
    "LLM_EXTRACTOR_VERSION": ("app.memory.extraction", "LLM_EXTRACTOR_VERSION"),
    "RETRIEVAL_POLICY_VERSION": ("app.memory.retrieval", "RETRIEVAL_POLICY_VERSION"),
    "RULE_EXTRACTOR_VERSION": ("app.memory.extraction", "RULE_EXTRACTOR_VERSION"),
    "CandidateConsolidator": ("app.memory.consolidation_core", "CandidateConsolidator"),
    "CompletedTurnMemoryExtractor": ("app.memory.turn_core", "CompletedTurnMemoryExtractor"),
    "ConsolidateDecision": ("app.memory.models", "ConsolidateDecision"),
    "ConsolidateOutcome": ("app.memory.models", "ConsolidateOutcome"),
    "ConsolidationPolicy": ("app.memory.consolidation_core", "ConsolidationPolicy"),
    "DeletionLedgerEntry": ("app.memory.models", "DeletionLedgerEntry"),
    "DeletionReceipt": ("app.memory.models", "DeletionReceipt"),
    "EmbeddingProvider": ("app.memory.embeddings", "EmbeddingProvider"),
    "ExtractedCandidates": ("app.memory.models", "ExtractedCandidates"),
    "ExtractionBackend": ("app.memory.extraction", "ExtractionBackend"),
    "HashingEmbeddingProvider": ("app.memory.embeddings", "HashingEmbeddingProvider"),
    "HttpEmbeddingProvider": ("app.memory.embeddings", "HttpEmbeddingProvider"),
    "LlmMemoryExtractor": ("app.memory.extraction", "LlmMemoryExtractor"),
    "MemoryCandidate": ("app.memory.models", "MemoryCandidate"),
    "MemoryEntry": ("app.memory.models", "MemoryEntry"),
    "MemoryExtractor": ("app.memory.extraction", "MemoryExtractor"),
    "MemoryHit": ("app.memory.retrieval_models", "MemoryHit"),
    "MemoryIngester": ("app.memory.consolidation", "MemoryIngester"),
    "MemoryOriginKind": ("app.memory.models", "MemoryOriginKind"),
    "MemoryRetriever": ("app.memory.retrieval", "MemoryRetriever"),
    "MemorySourceEntry": ("app.memory.models", "MemorySourceEntry"),
    "MemorySourceKind": ("app.memory.models", "MemorySourceKind"),
    "MemorySourceRef": ("app.memory.models", "MemorySourceRef"),
    "MemoryStatus": ("app.memory.models", "MemoryStatus"),
    "MemoryStore": ("app.memory.store", "MemoryStore"),
    "MemorySubjectKind": ("app.memory.models", "MemorySubjectKind"),
    "MemoryType": ("app.memory.models", "MemoryType"),
    "ReplayReport": ("app.memory.replay", "ReplayReport"),
    "RetrievalPolicy": ("app.memory.retrieval", "RetrievalPolicy"),
    "RetrievalResult": ("app.memory.retrieval_models", "RetrievalResult"),
    "RuleBasedExtractor": ("app.memory.extraction", "RuleBasedExtractor"),
    "SimilarMemory": ("app.memory.models", "SimilarMemory"),
    "StructuredMemoryExtractor": ("app.memory.extraction_core", "StructuredMemoryExtractor"),
    "TurnMemoryExtractor": ("app.memory.extraction", "TurnMemoryExtractor"),
    "build_embedding_provider": ("app.memory.embeddings", "build_embedding_provider"),
    "cosine_similarity": ("app.memory.similarity", "cosine_similarity"),
    "extract_assistant_fact_assertions": (
        "app.memory.extraction",
        "extract_assistant_fact_assertions",
    ),
    "extraction_instruction": ("app.memory.extraction", "extraction_instruction"),
    "lexical_cosine": ("app.memory.similarity", "lexical_cosine"),
    "probe_embedding_provider": ("app.memory.embeddings", "probe_embedding_provider"),
    "replay_deletions": ("app.memory.replay", "replay_deletions"),
    "text_tokens": ("app.memory.similarity", "text_tokens"),
}

__all__ = [
    "DEFAULT_SUBJECT_SCOPES",
    "DEFAULT_TYPE_QUOTAS",
    "LLM_EXTRACTOR_VERSION",
    "RETRIEVAL_POLICY_VERSION",
    "RULE_EXTRACTOR_VERSION",
    "CandidateConsolidator",
    "CompletedTurnMemoryExtractor",
    "ConsolidateDecision",
    "ConsolidateOutcome",
    "ConsolidationPolicy",
    "DeletionLedgerEntry",
    "DeletionReceipt",
    "EmbeddingProvider",
    "ExtractedCandidates",
    "ExtractionBackend",
    "HashingEmbeddingProvider",
    "HttpEmbeddingProvider",
    "LlmMemoryExtractor",
    "MemoryCandidate",
    "MemoryEntry",
    "MemoryExtractor",
    "MemoryHit",
    "MemoryIngester",
    "MemoryOriginKind",
    "MemoryRetriever",
    "MemorySourceEntry",
    "MemorySourceKind",
    "MemorySourceRef",
    "MemoryStatus",
    "MemoryStore",
    "MemorySubjectKind",
    "MemoryType",
    "ReplayReport",
    "RetrievalPolicy",
    "RetrievalResult",
    "RuleBasedExtractor",
    "SimilarMemory",
    "StructuredMemoryExtractor",
    "TurnMemoryExtractor",
    "build_embedding_provider",
    "cosine_similarity",
    "extract_assistant_fact_assertions",
    "extraction_instruction",
    "lexical_cosine",
    "probe_embedding_provider",
    "replay_deletions",
    "text_tokens",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
