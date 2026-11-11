"""迁移期兼容重导出：avatar/appearance 映射已迁至各自领域 persistence 包。"""

from app.appearance.persistence import UiPreferenceRecord, UiThemeRecord
from app.avatar.persistence import (
    AvatarInstanceRecord,
    AvatarPackRecord,
    PersonaAvatarBindingRecord,
)

__all__ = [
    "AvatarInstanceRecord",
    "AvatarPackRecord",
    "PersonaAvatarBindingRecord",
    "UiPreferenceRecord",
    "UiThemeRecord",
]
