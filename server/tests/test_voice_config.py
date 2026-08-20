# ruff: noqa: RUF002, RUF003
"""语音配置中心化测试：voice 节校验、提供方工厂与保存即生效链路。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.models import HubConfig, VoiceAsrConfig, VoiceTtsProviderConfig
from app.config.store import load_config_file
from app.voice import (
    EdgeTtsSynthesizer,
    MiMoAsrRecognizer,
    MiMoTtsSynthesizer,
    build_voice_providers,
)

BASE_CONFIG = """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
"""


def _config_with(voice_yaml: str) -> HubConfig:
    path = Path("/tmp/aria-voice-config-test.yaml")
    path.write_text(BASE_CONFIG + voice_yaml, encoding="utf-8")
    config, _ = load_config_file(path)
    return config


def test_voice_section_defaults_to_disabled() -> None:
    config = _config_with("")
    assert config.voice.asr is None
    assert config.voice.tts == []
    recognizer, chain = build_voice_providers(config)
    assert recognizer is None
    assert chain is None


def test_voice_asr_requires_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        VoiceAsrConfig()
    assert VoiceAsrConfig(secret_value="k").secret_value == "k"
    assert VoiceAsrConfig(secret_ref="env:MIMO_API_KEY").secret_ref == "env:MIMO_API_KEY"


def test_voice_tts_provider_rules() -> None:
    # edge_tts 无需 base_url 与密钥
    edge = VoiceTtsProviderConfig(provider="edge_tts", voice="zh-CN-XiaoxiaoNeural")
    assert edge.provider == "edge_tts"
    # mimo 必须给 base_url + 密钥
    with pytest.raises(ValueError, match="base_url"):
        VoiceTtsProviderConfig(provider="mimo", secret_value="k")
    with pytest.raises(ValueError, match="secret"):
        VoiceTtsProviderConfig(provider="mimo", base_url="https://api.xiaomimimo.com/v1")
    mimo = VoiceTtsProviderConfig(
        provider="mimo", base_url="https://api.xiaomimimo.com/v1", secret_value="k"
    )
    assert mimo.voice == "冰糖"


def test_build_voice_providers_from_config() -> None:
    config = _config_with(
        """
voice:
  asr:
    provider: mimo
    secret_value: asr-key
    language: zh
  tts:
    - provider: mimo
      base_url: https://api.xiaomimimo.com/v1
      secret_value: tts-key
      voice: 茉莉
    - provider: edge_tts
      voice: zh-CN-YunxiNeural
    - provider: edge_tts
      voice: zh-CN-XiaoxiaoNeural
      enabled: false
"""
    )

    recognizer, chain = build_voice_providers(config)

    assert isinstance(recognizer, MiMoAsrRecognizer)
    assert recognizer.runs_local is False
    # 禁用项被剔除，链按声明顺序保留两家
    providers = chain.providers if chain is not None else ()
    assert len(providers) == 2
    assert isinstance(providers[0], MiMoTtsSynthesizer)
    assert isinstance(providers[1], EdgeTtsSynthesizer)


def test_build_voice_providers_skips_entries_with_unresolvable_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    config = _config_with(
        """
voice:
  asr:
    secret_ref: env:MIMO_API_KEY
  tts:
    - provider: mimo
      base_url: https://api.xiaomimimo.com/v1
      secret_ref: env:MIMO_API_KEY
    - provider: edge_tts
      voice: zh-CN-XiaoxiaoNeural
"""
    )

    recognizer, chain = build_voice_providers(config)

    # 密钥解析失败的条目跳过：ASR 不可用，TTS 退到 edge-tts
    assert recognizer is None
    assert chain is not None
    assert len(chain.providers) == 1
    assert isinstance(chain.providers[0], EdgeTtsSynthesizer)
