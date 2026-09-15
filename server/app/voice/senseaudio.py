"""SenseAudio 聊天 TTS 合成器：t2a_v2 非流式按句合成（docs/04 语音管线）。

云端实现（runs_local=False）：L2 私密内容禁止出站，由故障转移链降级。
输出 mp3（24kHz 单声道），整段返回后按固定块切片产出，与 edge-tts 的
mp3 通道一致；按字符计费，句子级切片由管线负责（first_tts_chunk_chars）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.integrations.senseaudio import SenseAudioClient
from app.schemas import PrivacyLevel

from .contracts import LocalOnlySynthesizerError

logger = logging.getLogger(__name__)

SENSEAUDIO_TTS_DEFAULT_MODEL = "sensenova-tts-2.0"
SENSEAUDIO_TTS_DEFAULT_VOICE = "male_0018_a"
CHUNK_BYTES = 4_096


class SenseAudioTtsSynthesizer:
    """SenseAudio 云端 TTS；L2 内容禁止交给本合成器。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str = SENSEAUDIO_TTS_DEFAULT_MODEL,
        voice: str = SENSEAUDIO_TTS_DEFAULT_VOICE,
        speed: float = 1.0,
        timeout_s: float = 30.0,
    ) -> None:
        self.runs_local = False
        self.mime = "audio/mpeg"
        self.sample_rate = 24_000
        self._client = SenseAudioClient(
            api_key,
            base_url=base_url,
            tts_model=model,
            timeout_s=timeout_s,
        )
        self._voice = voice
        self._speed = speed

    async def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        # 隐私闸门：L2 内容只允许本地 TTS；SenseAudio 是云端服务
        if privacy_level is PrivacyLevel.L2:
            raise LocalOnlySynthesizerError(privacy_level)
        logger.info(
            "tts synthesize provider=senseaudio voice=%s chars=%d",
            self._voice,
            len(text),
        )
        result = await self._client.synthesize(
            text,
            self._voice,
            speed=self._speed,
            audio_format="mp3",
            sample_rate=self.sample_rate,
        )
        audio = result.audio
        for offset in range(0, len(audio), CHUNK_BYTES):
            yield audio[offset : offset + CHUNK_BYTES]
