"""迁移期兼容重导出：delivery 映射已拆分至三个领域 persistence 包。"""

from app.home_assistant.persistence import HomeAssistantProactiveLogRecord
from app.output.persistence import ProactiveDeliveryReceiptRecord
from app.push.persistence import PushSubscriptionRecord

__all__ = [
    "HomeAssistantProactiveLogRecord",
    "ProactiveDeliveryReceiptRecord",
    "PushSubscriptionRecord",
]
