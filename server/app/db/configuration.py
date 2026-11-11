"""迁移期兼容重导出：config/persona 映射已迁至各自领域 persistence 包。"""

from app.config.persistence import ConfigPointerRecord, ConfigVersionRecord
from app.persona.persistence import PersonaPointerRecord, PersonaVersionRecord

__all__ = [
    "ConfigPointerRecord",
    "ConfigVersionRecord",
    "PersonaPointerRecord",
    "PersonaVersionRecord",
]
