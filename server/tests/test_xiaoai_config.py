from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.config import ConfigStore, XiaoAiConfig
from app.xiaoai_config import XiaoAiConfigMaterializer


async def test_xiaoai_config_is_materialized_for_gateway(tmp_path: Path) -> None:
    owner = UUID("00000000-0000-0000-0000-000000000001")
    config = XiaoAiConfig(
        enabled=True,
        xiaomi_user_id="123456",
        xiaomi_password_secret_value="xiaomi-password",
        speaker_name="主卧小爱",
        ha_device_id="ha-device-1",
        model="xiaomi.wifispeaker.lx06",
        owner_user_id=owner,
        gateway_token_secret_value="gateway-token-at-least-20-chars",
    )
    store = SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(integrations=SimpleNamespace(xiaoai=config)))
    )
    target = tmp_path / "xiaoai" / "config.json"
    materializer = XiaoAiConfigMaterializer(cast(ConfigStore, cast(Any, store)), target)

    await materializer.write()

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["speaker_name"] == "主卧小爱"
    assert payload["model"] == "xiaomi.wifispeaker.lx06"
    assert payload["xiaomi_password"] == "xiaomi-password"
    assert payload["xiaomi_pass_token"] is None
    assert payload["gateway_token"] == "gateway-token-at-least-20-chars"
    assert materializer.credentials() == ("gateway-token-at-least-20-chars", owner)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_enabled_xiaoai_requires_complete_visual_config() -> None:
    with pytest.raises(ValidationError, match="enabled xiaoai requires"):
        XiaoAiConfig(enabled=True)


def test_enabled_xiaoai_accepts_pass_token_login() -> None:
    XiaoAiConfig(
        enabled=True,
        xiaomi_pass_token_secret_value="xiaomi-pass-token",
        speaker_name="主卧小爱",
        owner_user_id=UUID("00000000-0000-0000-0000-000000000001"),
        gateway_token_secret_value="gateway-token-at-least-20-chars",
    )


def test_xiaoai_tts_action_ids_must_be_paired() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        XiaoAiConfig(tts_siid=5)
