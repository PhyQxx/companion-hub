"""迁移期兼容重导出：skills 映射已迁至 app.skills.persistence。"""

from app.skills.persistence import (
    SkillConnectionRecord,
    SkillCredentialRecord,
    SkillDraftRecord,
    SkillRecord,
    SkillRunRecord,
    SkillSuggestionRecord,
    SkillVersionRecord,
)

__all__ = [
    "SkillConnectionRecord",
    "SkillCredentialRecord",
    "SkillDraftRecord",
    "SkillRecord",
    "SkillRunRecord",
    "SkillSuggestionRecord",
    "SkillVersionRecord",
]
