"""迁移期兼容重导出：identity 映射已迁至 auth/context 领域 persistence 包。"""

from app.auth.persistence import (
    AppUserRecord,
    AuthCredentialRecord,
    AuthSessionRecord,
)
from app.context.persistence import ObservationOwnerBindingRecord

__all__ = [
    "AppUserRecord",
    "AuthCredentialRecord",
    "AuthSessionRecord",
    "ObservationOwnerBindingRecord",
]
