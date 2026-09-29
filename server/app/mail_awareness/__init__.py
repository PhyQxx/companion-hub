"""Mail awareness: periodic unread-mail observation feeding the cognition loop."""
from .loop import (
    LlmMailAnalyzer,
    MailAnalysis,
    MailAwarenessError,
    MailAwarenessLoop,
    parse_analysis,
)

__all__ = [
    "LlmMailAnalyzer",
    "MailAnalysis",
    "MailAwarenessError",
    "MailAwarenessLoop",
    "parse_analysis",
]
