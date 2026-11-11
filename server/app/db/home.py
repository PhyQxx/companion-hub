"""迁移期兼容重导出：home_scene/safety 映射已迁至各自领域 persistence 包。"""

from app.home_scene.persistence import HomeSceneRecord
from app.safety.persistence import (
    SafetyAlertEscalationRecord,
    SafetyAlertRecord,
    SafetyAuthorizationRecord,
)

__all__ = [
    "HomeSceneRecord",
    "SafetyAlertEscalationRecord",
    "SafetyAlertRecord",
    "SafetyAuthorizationRecord",
]
