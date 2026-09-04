from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from app.config import ConfigStore, DatabaseConfigStore, XiaoAiConfig
from app.llm import EnvSecretProvider


class XiaoAiConfigMaterializer:
    """Materialize the published XiaoAI config into the gateway's private volume."""

    def __init__(self, store: ConfigStore | DatabaseConfigStore, path: Path) -> None:
        self._store = store
        self._path = path

    def credentials(self) -> tuple[str, UUID] | None:
        config = self._store.current.config.integrations.xiaoai
        token = _resolve_secret(
            config.gateway_token_secret_value,
            config.gateway_token_secret_ref,
        )
        if not config.enabled or token is None or config.owner_user_id is None:
            return None
        return token, config.owner_user_id

    async def write(self) -> None:
        config = self._store.current.config.integrations.xiaoai
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = _gateway_payload(config)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self._path)


def _gateway_payload(config: XiaoAiConfig) -> dict[str, object]:
    if not config.enabled:
        return {"enabled": False}
    return {
        "enabled": config.enabled,
        "xiaomi_user_id": config.xiaomi_user_id,
        "xiaomi_password": _resolve_secret(
            config.xiaomi_password_secret_value,
            config.xiaomi_password_secret_ref,
        ),
        "xiaomi_pass_token": _resolve_secret(
            config.xiaomi_pass_token_secret_value,
            config.xiaomi_pass_token_secret_ref,
        ),
        "speaker_name": config.speaker_name,
        "ha_device_id": config.ha_device_id,
        "model": config.model,
        "owner_user_id": str(config.owner_user_id) if config.owner_user_id else None,
        "gateway_token": _resolve_secret(
            config.gateway_token_secret_value,
            config.gateway_token_secret_ref,
        ),
        "trigger_prefix": config.trigger_prefix,
        "tts_siid": config.tts_siid,
        "tts_aiid": config.tts_aiid,
    }


def _resolve_secret(value: str | None, reference: str | None) -> str | None:
    if value is not None:
        return value
    if reference is None:
        return None
    return EnvSecretProvider().resolve(reference)
