"""从配置中心快照构建语音提供方（docs/04 §3.3）。

语音配置与模型路由一样进后台配置中心（保存即生效）；
每条话语开始前重新解析一次，管理端改配置不需要重启服务。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.config.models import VoiceAsrConfig, VoiceCostConfig, VoiceTtsProviderConfig
from app.harness.budget import BudgetDenied
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.llm.provider import EnvSecretProvider, SecretNotFound

from .contracts import SpeechRecognizer, SpeechSynthesizer
from .delivery_ports import VoiceProviderBinding
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
        *,
        pricing: tuple[tuple[object, VoiceProviderBinding], ...] = (),
    ) -> None:
        self._recognizer = recognizer
        self._tts_chain = tts_chain
        self._pricing = pricing

    def binding_for(self, provider: object) -> VoiceProviderBinding | None:
        return next(
            (binding for candidate, binding in self._pricing if candidate is provider), None
        )

    def validate_provider(self, provider: object) -> None:
        if provider is self._recognizer:
            return
        if self._tts_chain is None or not any(
            candidate is provider for candidate in self._tts_chain.providers
        ):
            raise BudgetDenied("voice_provider_configuration_changed")

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
        self._pricing: WeakKeyDictionary[object, VoiceProviderBinding] = WeakKeyDictionary()

    def binding_for(self, provider: object) -> VoiceProviderBinding | None:
        return self._pricing.get(provider)

    def validate_provider(self, provider: object) -> None:
        binding = self.binding_for(provider)
        config = self._config_store.current.config
        entries: list[VoiceAsrConfig | VoiceTtsProviderConfig] = [
            entry for entry in config.voice.tts if entry.enabled
        ]
        if config.voice.asr is not None:
            entries.append(config.voice.asr)
        if binding is None or not any(
            _provider_fingerprints(config, entry)[1] == binding.authority_fingerprint
            for entry in entries
        ):
            raise BudgetDenied("voice_provider_configuration_changed")

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

            def register(provider: object, entry: VoiceCostConfig, kind: str) -> None:
                quote = (
                    UnitCostQuote(
                        pricing=UnitPricing(
                            unit="request",
                            currency=entry.cost_currency,
                            rate_per_unit=entry.request_cost_ceiling,
                        ),
                        maximum_quantity=1,
                    )
                    if entry.request_cost_ceiling is not None and entry.cost_currency is not None
                    else None
                )
                assert isinstance(entry, (VoiceAsrConfig, VoiceTtsProviderConfig))
                fingerprint, authority = _provider_fingerprints(snapshot.config, entry)
                self._pricing[provider] = VoiceProviderBinding(
                    f"voice.{kind}.{fingerprint}", quote, snapshot.version, authority
                )

            self._cache = build_voice_providers(snapshot.config, register=register)
            self._cache_key = cache_key
            return self._cache


def _provider_fingerprints(
    config: HubConfig, entry: VoiceAsrConfig | VoiceTtsProviderConfig
) -> tuple[str, str]:
    """Keep credential authority in memory; public cost identifiers exclude it."""
    public = entry.model_dump(
        mode="json",
        include={
            "provider",
            "model",
            "voice",
            "language",
            "device",
            "compute_type",
            "runs_local",
            "cost_currency",
            "request_cost_ceiling",
        },
    )
    authority = entry.model_dump(mode="json")
    effective_url = entry.base_url
    if isinstance(entry, VoiceTtsProviderConfig) and entry.provider == "senseaudio":
        shared = config.voice.senseaudio
        if entry.base_url is None:
            effective_url = shared.base_url
            authority["base_url"] = str(effective_url) if effective_url is not None else None
        if entry.secret_value is None:
            # An unresolved entry reference also falls back to shared credentials.
            authority["shared_secret_ref"] = shared.secret_ref
            authority["shared_secret_value"] = shared.secret_value

    # URLs may carry credentials in userinfo, queries or paths. Only their
    # public origin groups prices; the full connection authority stays in memory.
    origin = urlsplit(str(effective_url)) if effective_url is not None else None
    public["origin"] = (origin.scheme, origin.hostname, origin.port) if origin is not None else None

    def digest(payload: object) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    return digest(public)[:24], digest(authority)


def _resolve_secret(secret_value: str | None, secret_ref: str | None) -> str | None:
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
    *,
    register: Callable[[object, VoiceCostConfig, str], None] | None = None,
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
                initial_prompt=(asr.initial_prompt or FASTER_WHISPER_DEFAULT_INITIAL_PROMPT),
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
        if recognizer is not None and register is not None:
            register(recognizer, asr, "asr")

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
            api_key = _resolve_secret(provider_config.secret_value, provider_config.secret_ref)
            if api_key is None or provider_config.base_url is None:
                logger.warning("voice tts provider skipped: mimo secret/base_url not resolved")
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
            providers.append(EdgeTtsSynthesizer(provider_config.voice or EDGE_TTS_DEFAULT_VOICE))
        if register is not None:
            register(providers[-1], provider_config, "tts")

    tts_chain = TtsProviderChain(providers) if providers else None
    return recognizer, tts_chain
