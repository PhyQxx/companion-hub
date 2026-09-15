"""SenseAudio 开放平台客户端（admin 声音管理代理）。

覆盖三类只读/低频管理接口：音色目录查询、TTS 试听合成、语音识别历史。
API key 在 Hub 侧解析并注入 Authorization；浏览器管理端只接触 Hub 代理，
不直接持有密钥。TTS 音频按文档以 hex 编码返回，这里统一解码为原始字节。
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

DEFAULT_BASE_URL = "https://api.senseaudio.cn"
DEFAULT_TTS_MODEL = "sensenova-tts-2.0"
VoiceType = Literal["system", "voice_clone", "voice_generation", "all"]

# Free（普通音色）套餐可合成的系统音色（docs 音色目录页，不含限免活动音色）。
# get_voice 响应不含套餐字段，这里按文档维护；列表会随后续套餐说明更新。
FREE_TIER_VOICE_IDS = frozenset(
    {
        "child_0001_a",
        "child_0001_b",
        "male_0004_a",
        "male_0018_a",
    }
)

# 上游"无此音色权限"的固定 status_msg：映射为 Free 套餐引导话术。
NO_VOICE_ACCESS_MESSAGE = "no access to the specified voice"


class SenseAudioError(Exception):
    """上游返回失败或响应结构不符合预期。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.trace_id = trace_id
        suffix = ""
        if status_code is not None:
            suffix += f"（status_code={status_code}"
            suffix += f", trace_id={trace_id}" if trace_id else ""
            suffix += "）"
        elif trace_id:
            suffix = f"（trace_id={trace_id}）"
        super().__init__(f"{message}{suffix}")


@dataclass(frozen=True, slots=True)
class SenseAudioVoice:
    category: str
    voice_id: str
    voice_name: str
    description: tuple[str, ...]
    created_time: str | None
    free_tier: bool = False


@dataclass(frozen=True, slots=True)
class SenseAudioSynthesis:
    audio: bytes
    audio_format: str
    sample_rate: int | None
    usage_characters: int | None
    audio_length: float | None
    audio_size: int | None


@dataclass(frozen=True, slots=True)
class SenseAudioCloneFile:
    file_id: str
    filename: str
    size_bytes: int
    created_at: int | None


@dataclass(frozen=True, slots=True)
class SenseAudioCloneResult:
    label: str
    name: str
    description: str
    created_at: int | None
    demo: str | None


class SenseAudioClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        tts_model: str = DEFAULT_TTS_MODEL,
        timeout_s: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._tts_model = tts_model
        self._timeout_s = timeout_s
        self._client = http_client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _transport(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SenseAudioError("上游响应不是 JSON 对象")
        base = payload.get("base_resp")
        if isinstance(base, dict) and base.get("status_code") not in (None, 0):
            raise SenseAudioError(
                str(base.get("status_msg") or "上游返回失败"),
                status_code=base.get("status_code"),
                trace_id=(
                    str(payload["trace_id"])
                    if isinstance(payload.get("trace_id"), str)
                    else None
                ),
            )
        return payload

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        try:
            response = await self._transport().request(
                method, url, headers=self._headers(), json=json_body
            )
        except httpx.HTTPError as error:
            raise SenseAudioError(f"请求 SenseAudio 失败：{error}") from error
        if response.status_code in {401, 403}:
            raise SenseAudioError("SenseAudio 鉴权失败，请检查 API Key", status_code=401)
        if response.status_code != 200:
            raise SenseAudioError(
                f"SenseAudio 返回 HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload: Any = response.json()
        except ValueError as error:
            raise SenseAudioError("SenseAudio 响应不是合法 JSON") from error
        return self._check(payload)

    async def list_voices(self, voice_type: VoiceType = "all") -> list[SenseAudioVoice]:
        payload = await self._request_json(
            "POST", "/v1/get_voice", json_body={"voice_type": voice_type}
        )
        category_map = (
            ("system", "system_voice"),
            ("voice_clone", "voice_cloning"),
            ("voice_generation", "voice_generation"),
        )
        voices: list[SenseAudioVoice] = []
        for category, field in category_map:
            items = payload.get(field)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not item.get("voice_id"):
                    continue
                description = item.get("description")
                voices.append(
                    SenseAudioVoice(
                        category=category,
                        voice_id=str(item["voice_id"]),
                        voice_name=str(item.get("voice_name") or item["voice_id"]),
                        description=tuple(
                            part
                            for part in (
                                description if isinstance(description, list) else [description]
                            )
                            if isinstance(part, str) and part.strip()
                        ),
                        created_time=(
                            str(item["created_time"]) if item.get("created_time") else None
                        ),
                        free_tier=str(item["voice_id"]) in FREE_TIER_VOICE_IDS,
                    )
                )
        return voices

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        model: str | None = None,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        audio_format: str = "mp3",
        sample_rate: int = 32000,
    ) -> SenseAudioSynthesis:
        payload = await self._request_json(
            "POST",
            "/v1/t2a_v2",
            json_body={
                "model": model or self._tts_model,
                "text": text,
                "stream": False,
                "voice_setting": {
                    "voice_id": voice_id,
                    "speed": speed,
                    "vol": vol,
                    "pitch": pitch,
                },
                "audio_setting": {
                    "format": audio_format,
                    "sample_rate": sample_rate,
                },
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("audio"), str):
            raise SenseAudioError("SenseAudio 未返回音频数据")
        try:
            audio = bytes.fromhex(data["audio"])
        except ValueError as error:
            raise SenseAudioError("SenseAudio 音频数据解码失败") from error
        extra = payload.get("extra_info")
        extra = extra if isinstance(extra, dict) else {}
        return SenseAudioSynthesis(
            audio=audio,
            audio_format=str(extra.get("audio_format") or audio_format),
            sample_rate=extra.get("audio_sample_rate") or sample_rate,
            usage_characters=extra.get("usage_characters"),
            audio_length=extra.get("audio_length"),
            audio_size=extra.get("audio_size"),
        )

    async def asr_records(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"page": max(page, 1), "page_size": max(min(page_size, 100), 1)}
        if session_id:
            query["session_id"] = session_id
        payload = await self._request_json("GET", "/v1/audio/records", query=query)
        records = payload.get("list")
        if records is None:
            raise SenseAudioError("SenseAudio 识别历史响应缺少 list 字段")
        return {
            "total": int(payload.get("total") or 0),
            "records": [item for item in records if isinstance(item, dict)],
        }

    async def upload_clone_file(
        self,
        data: bytes,
        *,
        filename: str,
    ) -> SenseAudioCloneFile:
        """上传克隆参考音频。该端点 Authorization 直接传 API Key（文档：不加 Bearer）。"""
        url = f"{self._base_url}/v1/files/upload"
        try:
            response = await self._transport().request(
                "POST",
                url,
                headers={"Authorization": self._api_key},
                files={"file": (filename, data)},
                data={"purpose": "voice_clone"},
            )
        except httpx.HTTPError as error:
            raise SenseAudioError(f"上传参考音频失败：{error}") from error
        if response.status_code != 200:
            raise SenseAudioError(
                f"上传参考音频返回 HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload: Any = response.json()
        except ValueError as error:
            raise SenseAudioError("上传响应不是合法 JSON") from error
        payload = self._check(payload)
        file_info = payload.get("file")
        if not isinstance(file_info, dict) or not file_info.get("file_id"):
            raise SenseAudioError("上传响应缺少 file_id")
        return SenseAudioCloneFile(
            file_id=str(file_info["file_id"]),
            filename=str(file_info.get("filename") or filename),
            size_bytes=int(file_info.get("bytes") or len(data)),
            created_at=int(file_info["created_at"]) if file_info.get("created_at") else None,
        )

    async def clone_voice(
        self,
        *,
        file_id: str,
        label: str,
        description: str,
        text: str,
        model: str | None = None,
    ) -> SenseAudioCloneResult:
        payload = await self._request_json(
            "POST",
            "/v1/voice/clone",
            json_body={
                "file_id": file_id,
                "label": label,
                "description": description,
                "text": text,
                "model": model or "sensenova-tts-multilingual-2.0",
            },
        )
        if not payload.get("label"):
            raise SenseAudioError("克隆响应缺少 label")
        return SenseAudioCloneResult(
            label=str(payload["label"]),
            name=str(payload.get("name") or label),
            description=str(payload.get("description") or ""),
            created_at=(
                int(payload["created_at"]) if payload.get("created_at") else None
            ),
            demo=str(payload["demo"]) if payload.get("demo") else None,
        )


async def check_connection(
    client: SenseAudioClient,
) -> tuple[bool, str]:
    """用一次最小的音色目录请求验证连通性与鉴权。"""
    try:
        await client.list_voices("system")
    except SenseAudioError as error:
        return False, str(error)
    return True, "连接成功，音色目录可读取"


def decode_hex_audio(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except (ValueError, binascii.Error) as error:
        raise SenseAudioError("音频数据解码失败") from error
