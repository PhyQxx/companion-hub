"""迁移期兼容重导出：devices 映射已迁至 app.devices.persistence。"""

from app.devices.persistence import (
    DeviceClientRecord,
    DeviceCommandRecord,
    DevicePairingCodeRecord,
)

__all__ = [
    "DeviceClientRecord",
    "DeviceCommandRecord",
    "DevicePairingCodeRecord",
]
