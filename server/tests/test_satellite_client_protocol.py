"""SAT 卫星客户端与 Hub 的协议一致性：签名算法与帧契约交叉验证。

客户端在 integrations/satellite_client/（无 pytest 依赖），按路径加载模块，
确保两边 HMAC 规范化签名永远一致——任何一侧改了签名/序列化规则，
这组测试都会失败并阻止部署。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from app.api.device_commands import sign_device_frame, verify_device_signature

_CLIENT_PATH = (
    Path(__file__).resolve().parents[2]
    / "integrations"
    / "satellite_client"
    / "satellite.py"
)


def _load_client_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("satellite_client_under_test", _CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("satellite_client_under_test", module)
    spec.loader.exec_module(module)
    return module


def test_client_signature_matches_hub() -> None:
    client = _load_client_module()
    token = "device-access-token-0123456789abcdef"
    payload = {
        "proto_version": 1,
        "type": "satellite.audio.chunk",
        "utterance_id": str(uuid4()),
        "index": 3,
        "data_b64": base64.b64encode(b"\x00\x01\x02").decode("ascii"),
        "sent_at": "2026-09-16T00:00:00+00:00",
    }
    client_signed = client.sign_frame(token, dict(payload))
    assert verify_device_signature(token, client_signed)
    # 与 Hub 直接签名完全一致
    assert client_signed["signature"] == sign_device_frame(token, dict(payload))
    # 客户端签名不被错误令牌接受
    assert not verify_device_signature("other-token", client_signed)


def test_client_chunking_matches_hub_limits() -> None:
    """客户端上行分片必须满足 Hub 帧契约：b64 ≤90k、话语 ≤4MiB。"""
    client = _load_client_module()
    assert int(90_000 * 3 / 4) >= client.CHUNK_BYTES  # b64 膨胀后仍在 schema 限制内
    assert client.MAX_UTTERANCE_BYTES <= 4 * 1024 * 1024
    # 每片 b64 长度实测
    piece = bytes(client.CHUNK_BYTES)
    assert len(base64.b64encode(piece).decode("ascii")) <= 90_000


def test_client_frame_shapes_match_hub_schema() -> None:
    """客户端发出的关键帧字段能通过 Hub 的 pydantic 帧模型校验。"""
    from app.satellite.models import (
        SatelliteAudioChunkFrame,
        SatelliteHelloFrame,
        SatelliteWakeFrame,
    )

    client = _load_client_module()
    utterance_id = str(uuid4())
    hello = {
        "proto_version": 1,
        "type": "satellite.hello",
        "room_id": "bedroom",
        "firmware": "satellite-client/1.0",
        "max_privacy_level": "L1",
        "continuous_timeout_seconds": 8.0,
    }
    assert SatelliteHelloFrame.model_validate(hello)

    wake = {
        "proto_version": 1,
        "type": "satellite.wake",
        "room_id": "bedroom",
        "detected_at": "2026-09-16T00:00:00+00:00",
        "confidence": 1.0,
    }
    assert SatelliteWakeFrame.model_validate(wake)

    chunk = {
        "proto_version": 1,
        "type": "satellite.audio.chunk",
        "utterance_id": utterance_id,
        "index": 0,
        "data_b64": base64.b64encode(bytes(client.CHUNK_BYTES)).decode("ascii"),
    }
    parsed = SatelliteAudioChunkFrame.model_validate(chunk)
    assert len(base64.b64decode(parsed.data_b64)) == client.CHUNK_BYTES


def test_client_sign_uses_canonical_json() -> None:
    """键序/空白差异不影响签名：与 Hub 相同的规范化规则。"""
    client = _load_client_module()
    token = "t" * 40
    payload = {"type": "device.heartbeat", "sent_at": "2026-09-16T00:00:00+00:00"}
    signed = client.sign_frame(token, dict(payload))
    # 手工按 Hub 规则重算
    unsigned = {k: v for k, v in signed.items() if k != "signature"}
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hmac as _hmac

    assert signed["signature"] == _hmac.new(
        token.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
