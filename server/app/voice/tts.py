# ruff: noqa: RUF002, RUF003
"""edge-tts 流式合成：免费、无 key，作为 MiMo TTS 的故障备份。

输出 mp3 分片（24kHz 单声道）；浏览器端按 sentence 事件的 mime 播放，
与 PCM 直出的主通道互不影响。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.schemas import PrivacyLevel

from .contracts import LocalOnlySynthesizerError

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class EdgeTtsSynthesizer:
    """微软 Edge 免费 TTS；云端实现，L2 禁止出站。"""

    def __init__(self, voice: str = DEFAULT_VOICE) -> None:
        self.runs_local = False
        self.mime = "audio/mpeg"
        self.sample_rate = 24_000
        self._voice = voice

    async def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        # 隐私闸门：L2 内容只允许本地 TTS；edge-tts 是云端服务
        if privacy_level is PrivacyLevel.L2:
            raise LocalOnlySynthesizerError(privacy_level)
        import edge_tts

        logger.info(
            "tts synthesize provider=edge-tts voice=%s chars=%d", self._voice, len(text)
        )
        communicate = edge_tts.Communicate(text, self._voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data = chunk.get("data")
                if data:
                    yield bytes(data)
