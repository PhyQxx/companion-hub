"""小米 MiMo 语音适配器（docs/33）：ASR 与 TTS 均为云端 OpenAI 兼容接口。

- ASR（mimo-v2.5-asr）：仅收 wav/mp3 的 base64，服务端把 PCM16 包上 WAV
  头再上传；asr_options.language 支持 auto/zh/en。
- TTS（mimo-v2.5-tts）：assistant 消息为待合成文本，audio.format=pcm16
  流式返回 base64 PCM16（24kHz 单声道），可直接衔接口型包络计算。

两者 runs_local=False：L2 音频/文本禁止出站，由调用侧降级处理。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import wave
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any

import httpx

from app.schemas import PrivacyLevel

from .contracts import LocalOnlySynthesizerError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
ASR_MODEL = "mimo-v2.5-asr"
TTS_MODEL = "mimo-v2.5-tts"
DEFAULT_TTS_VOICE = "冰糖"


def wrap_wav(pcm: bytes, *, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """裸 PCM16 包上 WAV 容器头（MiMo ASR 只收 wav/mp3）。"""
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


class MiMoAsrRecognizer:
    """整段话语 → 文本；云端实现，L2 音频禁止交给本识别器。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = ASR_MODEL,
        language: str = "auto",
        timeout_s: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.runs_local = False
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._language = language
        self._timeout_s = timeout_s
        self._client = http_client

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        wav = wrap_wav(pcm, sample_rate=sample_rate)
        encoded = base64.b64encode(wav).decode("ascii")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{encoded}"
                            },
                        }
                    ],
                }
            ],
            "asr_options": {"language": language or self._language},
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        finally:
            if self._client is None:
                await client.aclose()
        content = _content_text(body)
        logger.info(
            "asr transcribe provider=mimo model=%s pcm_ms=%d text_chars=%d",
            self._model,
            len(pcm) * 1000 // (sample_rate * 2),
            len(content),
        )
        return content


class MiMoTtsSynthesizer:
    """句级流式合成；输出裸 PCM16（24kHz 单声道）。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = TTS_MODEL,
        voice: str = DEFAULT_TTS_VOICE,
        timeout_s: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.runs_local = False
        self.mime = "audio/pcm;rate=24000"
        self.sample_rate = 24_000
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._voice = voice
        self._timeout_s = timeout_s
        self._client = http_client

    async def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        # 隐私闸门：合成文本不允许随 L2 回复出站到云端
        if privacy_level is PrivacyLevel.L2:
            raise LocalOnlySynthesizerError(privacy_level)
        payload = {
            "model": self._model,
            "messages": [{"role": "assistant", "content": text}],
            "audio": {"format": "pcm16", "voice": self._voice},
            "stream": True,
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            ) as response:
                response.raise_for_status()
                produced = False
                async for chunk in _sse_audio_chunks(response):
                    produced = True
                    yield chunk
                if not produced:
                    raise RuntimeError("mimo tts empty stream")
        finally:
            if self._client is None:
                await client.aclose()


def _content_text(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    return content if isinstance(content, str) else ""


async def _sse_audio_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    """解析 SSE 流，产出 delta.audio.data 解码后的 PCM 分片。"""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        audio = choices[0].get("delta", {}).get("audio") or {}
        encoded = audio.get("data")
        if not encoded:
            continue
        try:
            yield base64.b64decode(encoded)
        except (binascii.Error, ValueError):
            logger.warning("mimo tts chunk base64 decode failed, skipped")
            continue
