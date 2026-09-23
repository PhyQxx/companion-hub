"""从配置中心快照构建语音提供方（docs/04 §3.3）。

语音配置与模型路由一样进后台配置中心（保存即生效）；
每条话语开始前重新解析一次，管理端改配置不需要重启服务。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Protocol

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.llm.provider import EnvSecretProvider, SecretNotFound

from .contracts import SpeechRecognizer, SpeechSynthesizer
from .failover import TtsProviderChain
from .faster_whisper import DEFAULT_HOTWORDS as FASTER_WHISPER_DEFAULT_HOTWORDS
from .faster_whisper import (
    DEFAULT_INITIAL_PROMPT as FASTER_WHISPER_DEFAULT_INITIAL_PROMPT,
)
from .faster_whisper import FasterWhisperRecognizer
from .mimo import MiMoAsrRecognizer, MiMoTtsSynthesizer
from .senseaudio import (
    SENSEAUDIO_TTS_DEFAULT_MODEL,
    SENSEAUDIO_TTS_DEFAULT_VOICE,
    SenseAudioTtsSynthesizer,
)
from .sherpa_streaming import SherpaStreamingRecognizer
from .tts import EdgeTtsSynthesizer

logger = logging.getLogger(__name__)

EDGE_TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
MIMO_TTS_DEFAULT_VOICE = "冰糖"


class VoiceProviderSource(Protocol):
    """语音回合的提供方来源：每条话语解析一次，跟随配置中心热更新。"""

    async def resolve(
        self,
    ) -> tuple[SpeechRecognizer | None, TtsProviderChain | None]: ...


class StaticVoiceSource:
    """固定提供方：测试与文件配置场景。"""

    def __init__(
        self,
        recognizer: SpeechRecognizer | None,
        tts_chain: TtsProviderChain | None,
    ) -> None:
        self._recognizer = recognizer
        self._tts_chain = tts_chain

    async def resolve(
        self,
    ) -> tuple[SpeechRecognizer | None, TtsProviderChain | None]:
        return self._recognizer, self._tts_chain


class ConfigVoiceSource:
    """配置中心来源：每条话语刷新配置；voice 未变化时复用提供方实例。"""

    def __init__(self, config_store: ConfigStore | DatabaseConfigStore) -> None:
        self._config_store = config_store
        self._cache_key: str | None = None
        self._cache: tuple[SpeechRecognizer | None, TtsProviderChain | None] = (None, None)
        self._cache_lock = asyncio.Lock()

    async def resolve(
        self,
    ) -> tuple[SpeechRecognizer | None, TtsProviderChain | None]:
        if isinstance(self._config_store, DatabaseConfigStore):
            snapshot = await self._config_store.refresh()
        else:
            snapshot = self._config_store.current
        voice_payload = snapshot.config.voice.model_dump_json()
        cache_key = hashlib.sha256(voice_payload.encode()).hexdigest()
        if cache_key == self._cache_key:
            return self._cache
        async with self._cache_lock:
            if cache_key == self._cache_key:
                return self._cache
            self._cache = build_voice_providers(snapshot.config)
            self._cache_key = cache_key
            return self._cache


def _resolve_secret(
    secret_value: str | None, secret_ref: str | None
) -> str | None:
    if secret_value is not None:
        return secret_value
    if secret_ref is not None:
        try:
            return EnvSecretProvider().resolve(secret_ref)
        except SecretNotFound:
            logger.warning("voice secret reference unavailable: %s", secret_ref)
            return None
    return None


def build_voice_providers(
    config: HubConfig,
) -> tuple[SpeechRecognizer | None, TtsProviderChain | None]:
    """按 voice 配置节构建识别器与合成链；密钥缺失的条目跳过并告警。"""
    recognizer: SpeechRecognizer | None = None
    asr = config.voice.asr
    if asr is not None:
        if asr.provider == "faster_whisper":
            recognizer = FasterWhisperRecognizer(
                model=asr.model,
                device=asr.device,
                compute_type=asr.compute_type,
                language=asr.language,
                initial_prompt=(
                    asr.initial_prompt or FASTER_WHISPER_DEFAULT_INITIAL_PROMPT
                ),
                hotwords=asr.hotwords or FASTER_WHISPER_DEFAULT_HOTWORDS,
            )
        elif asr.provider == "sherpa_streaming":
            recognizer = SherpaStreamingRecognizer(model_dir=asr.model)
        else:
            api_key = _resolve_secret(asr.secret_value, asr.secret_ref)
            if api_key is None or asr.base_url is None:
                logger.warning("voice asr skipped: secret/base_url not resolved")
            else:
                recognizer = MiMoAsrRecognizer(
                    api_key,
                    base_url=str(asr.base_url),
                    model=asr.model,
                    language=asr.language,
                )

    providers: list[SpeechSynthesizer] = []
    for provider_config in config.voice.tts:
        if not provider_config.enabled:
            continue
        if provider_config.provider == "senseaudio":
            # 条目自带配置优先，留空回退到共享连接（voice.senseaudio / 声音管理）。
            shared = config.voice.senseaudio
            api_key = _resolve_secret(
                provider_config.secret_value, provider_config.secret_ref
            ) or _resolve_secret(shared.secret_value, shared.secret_ref)
            base_url = provider_config.base_url or shared.base_url
            if api_key is None or base_url is None:
                logger.warning(
                    "voice tts provider skipped: senseaudio secret/base_url not resolved"
                )
                continue
            providers.append(
                SenseAudioTtsSynthesizer(
                    api_key,
                    base_url=str(base_url),
                    model=provider_config.model or SENSEAUDIO_TTS_DEFAULT_MODEL,
                    voice=provider_config.voice or SENSEAUDIO_TTS_DEFAULT_VOICE,
                )
            )
        elif provider_config.provider == "mimo":
            api_key = _resolve_secret(
                provider_config.secret_value, provider_config.secret_ref
            )
            if api_key is None or provider_config.base_url is None:
                logger.warning(
                    "voice tts provider skipped: mimo secret/base_url not resolved"
                )
                continue
            providers.append(
                MiMoTtsSynthesizer(
                    api_key,
                    base_url=str(provider_config.base_url),
                    model=provider_config.model,
                    voice=provider_config.voice or MIMO_TTS_DEFAULT_VOICE,
                )
            )
        else:
            providers.append(
                EdgeTtsSynthesizer(provider_config.voice or EDGE_TTS_DEFAULT_VOICE)
            )

    tts_chain = TtsProviderChain(providers) if providers else None
    return recognizer, tts_chain
