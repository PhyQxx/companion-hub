"""语音配置中心化测试：voice 节校验、提供方工厂与保存即生效链路。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.models import HubConfig, VoiceAsrConfig, VoiceTtsProviderConfig
from app.config.store import ConfigStore, load_config_file
from app.voice import (
    ConfigVoiceSource,
    EdgeTtsSynthesizer,
    FasterWhisperRecognizer,
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


async def test_config_voice_source_reuses_local_asr_until_voice_config_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(
        BASE_CONFIG
        + """
voice:
  asr:
    provider: faster_whisper
    model: small
    base_url: null
    runs_local: true
    device: cpu
    compute_type: int8
""",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    await store.load()
    source = ConfigVoiceSource(store)

    first, _ = await source.resolve()
    second, _ = await source.resolve()

    assert isinstance(first, FasterWhisperRecognizer)
    assert second is first

    updated = path.read_text(encoding="utf-8").replace("model: small", "model: medium")
    path.write_text(updated, encoding="utf-8")
    await store.reload(force=True)
    third, _ = await source.resolve()

    assert isinstance(third, FasterWhisperRecognizer)
    assert third is not first


def test_voice_asr_requires_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        VoiceAsrConfig()
    assert VoiceAsrConfig(secret_value="k").secret_value == "k"
    assert VoiceAsrConfig(secret_ref="env:MIMO_API_KEY").secret_ref == "env:MIMO_API_KEY"


def test_faster_whisper_asr_is_local_and_requires_no_secret() -> None:
    local = VoiceAsrConfig(
        provider="faster_whisper",
        model="small",
        base_url=None,
        runs_local=True,
        device="cpu",
        compute_type="int8",
        initial_prompt="这是与小艾的普通话对话。",
        hotwords="小艾 Aria 只回答",
    )
    assert local.secret_ref is None
    assert local.secret_value is None
    assert local.runs_local is True
    assert local.hotwords == "小艾 Aria 只回答"
    with pytest.raises(ValueError, match="must run locally"):
        VoiceAsrConfig(provider="faster_whisper", model="small", base_url=None)


def test_mimo_asr_rejects_faster_whisper_bias_options() -> None:
    with pytest.raises(ValueError, match="only supported by faster_whisper"):
        VoiceAsrConfig(secret_value="k", hotwords="小艾")


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


def test_build_faster_whisper_from_config_without_loading_model() -> None:
    config = _config_with(
        """
voice:
  asr:
    provider: faster_whisper
    model: small
    base_url: null
    runs_local: true
    device: cpu
    compute_type: int8
    initial_prompt: 这是与小艾的普通话对话。
    hotwords: 小艾 Aria 只回答
"""
    )

    recognizer, chain = build_voice_providers(config)

    assert isinstance(recognizer, FasterWhisperRecognizer)
    assert recognizer.runs_local is True
    assert recognizer._initial_prompt == "这是与小艾的普通话对话。"
    assert recognizer._hotwords == "小艾 Aria 只回答"
    assert chain is None


def test_build_faster_whisper_applies_project_term_bias_by_default() -> None:
    config = _config_with(
        """
voice:
  asr:
    provider: faster_whisper
    model: base
    base_url: null
    language: zh
    runs_local: true
"""
    )

    recognizer, _ = build_voice_providers(config)

    assert isinstance(recognizer, FasterWhisperRecognizer)
    assert recognizer._initial_prompt == "这是一段与智能伴侣小艾（Aria）的普通话对话。"
    assert recognizer._hotwords == "小艾 Aria 只回答"


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
