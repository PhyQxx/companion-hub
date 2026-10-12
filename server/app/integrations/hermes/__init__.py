"""HRM（docs/06 Hermes 外部 Agent 集成）：hermes api_server 受控客户端。

边界约定与 MCP 层一致：本包是外部执行适配层，不携带 Hub 权限模型；
httpx 只出现在 client.py；密钥只经 config secret_ref/secret_value 注入。
"""

from app.integrations.hermes.client import (
    HermesClientError,
    HermesHttpClient,
    hermes_client_factory,
)
from app.integrations.hermes.handler import HermesTaskHandler
from app.integrations.hermes.manager import HermesManager, HermesManagerError

__all__ = [
    "HermesClientError",
    "HermesHttpClient",
    "HermesManager",
    "HermesManagerError",
    "HermesTaskHandler",
    "hermes_client_factory",
]
