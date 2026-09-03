"""Web Push 发送器：VAPID 签名 + RFC8291 加密，推送服务 HTTP 调用。

requests 是同步客户端，适配器在 asyncio.to_thread 中调用，不阻塞事件循环。
推送服务返回 404/410 表示订阅已失效，调用方应立即删除该订阅。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pywebpush import WebPushException, webpush  # type: ignore[import-untyped]

logger = logging.getLogger("app.push.sender")

PUSH_TTL_SECONDS = 86_400
PUSH_TIMEOUT_SECONDS = 10.0


class PushSendStatus(StrEnum):
    DELIVERED = "delivered"
    GONE = "gone"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PushSendResult:
    status: PushSendStatus
    status_code: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class VapidCredentials:
    public_key: str
    private_key: str
    subject: str


def resolve_vapid_credentials(config: Any) -> VapidCredentials | None:
    """从 `integrations.push` 配置解析凭证；未启用或缺私钥返回 None。

    私钥 secret_value 优先，secret_ref 找不到环境变量时返回 None 并告警。
    """
    from app.config.models import WebPushConfig

    if not isinstance(config, WebPushConfig) or not config.enabled:
        return None
    private_key = config.vapid_private_key_secret_value
    if private_key is None and config.vapid_private_key_secret_ref is not None:
        import os

        private_key = os.environ.get(
            config.vapid_private_key_secret_ref.removeprefix("env:"), None
        )
        if private_key is None:
            logger.warning(
                "web push private key env unavailable: %s",
                config.vapid_private_key_secret_ref,
            )
    if config.vapid_public_key is None or private_key is None:
        return None
    return VapidCredentials(
        public_key=config.vapid_public_key,
        private_key=private_key,
        subject=config.vapid_subject,
    )


class WebPushSender:
    def send(
        self,
        subscription: dict[str, Any],
        payload: bytes,
        credentials: VapidCredentials,
    ) -> PushSendResult:
        """同步发送；一个订阅一次调用。"""
        try:
            response = webpush(
                subscription_info=subscription,
                data=payload,
                ttl=PUSH_TTL_SECONDS,
                timeout=PUSH_TIMEOUT_SECONDS,
                vapid_private_key=credentials.private_key,
                vapid_claims={"sub": credentials.subject},
            )
            code = int(getattr(response, "status_code", 0) or 0)
        except WebPushException as error:
            code = int(
                getattr(getattr(error, "response", None), "status_code", 0) or 0
            )
            if code in {404, 410}:
                return PushSendResult(PushSendStatus.GONE, code)
            logger.warning("web push send failed: %s", error)
            return PushSendResult(PushSendStatus.FAILED, code or None, "push_send_failed")
        except Exception as error:
            logger.warning("web push send raised: %s", error)
            return PushSendResult(PushSendStatus.FAILED, None, "push_send_failed")
        if code in {404, 410}:
            return PushSendResult(PushSendStatus.GONE, code)
        if 200 <= code <= 202:
            return PushSendResult(PushSendStatus.DELIVERED, code)
        return PushSendResult(PushSendStatus.FAILED, code, f"push_service_{code}")
