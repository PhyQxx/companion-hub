"""SAT-01~03 全屋语音卫星客户端：树莓派/旧手机（Termux）可运行的房间终端。

职责与红线（与 Hub 侧 `app/satellite/models.py` 状态机一一对应）：
- 设备身份：一次性配对获得的设备凭据（access_token），本客户端不持有任何
  用户账号信息；凭据泄露时在 Admin 设备页单独撤销本台设备；
- 唤醒仲裁：听到唤醒词只上报 `satellite.wake`，进入 listening 必须等 Hub
  下发 `satellite.state.set`（多台同喊时只有一台胜出，其余被压制）；
- 上行音频：PCM16/16k/mono，分片连续递增 + 结束时字节数与 SHA-256 校验，
  单话语上限 4 MiB；
- 下行播放：`voice.sentence`（mime/采样率）+ `satellite.audio.chunk` 分片，
  PCM 直接播放，MP3 依赖系统 `ffplay`/`mpv`（可选）；
- 免唤醒窗口（SAT-03）：回复后 Hub 置 listening 并给出 follow_up_until，
  窗口内直接开始录音，不需要再次唤醒；超时回 idle；
- barge-in：播放中检测到人声即发 `barge_in=true` 的 audio.start，打断旧回合；
- 跨房间接管：收到 `satellite.session.available` 后按键 `t` 发送
  `satellite.takeover`，把连续对话上下文迁到本房间。

依赖（requirements.txt）：websockets、numpy、sounddevice；
唤醒词可选 openwakeword（未安装时用回车键手动触发唤醒）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import queue
import sys
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import websockets

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("satellite")

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000
CHUNK_BYTES = 16 * 1024  # 上行分片（帧 schema 上限 90k b64 字符，留足余量）
MAX_UTTERANCE_BYTES = 4 * 1024 * 1024
HEARTBEAT_SECONDS = 25
VAD_SILENCE_MS = 900  # 静音多久判语毕
VAD_ENERGY_THRESHOLD = 0.012  # 归一化 RMS 阈值（按现场噪音调整）
WAKE_HANGOVER_MS = 300


def sign_frame(access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """与 Hub `sign_device_frame` 相同的 HMAC-SHA256 规范化签名。"""
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hmac.new(access_token.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    payload["signature"] = digest
    return payload


class EnergyVad:
    """能量 + hangover 的轻量 VAD；返回 (speech_frames, silence_ms)。"""

    def __init__(self, threshold: float = VAD_ENERGY_THRESHOLD) -> None:
        self.threshold = threshold
        self.speech_seen = False

    def is_speech(self, pcm_frame: bytes) -> bool:
        samples = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        return rms >= self.threshold


@dataclass
class SatelliteState:
    room_id: str
    state: str = "idle"
    follow_up_until: str | None = None
    playing: bool = False
    pending_takeover_from: str | None = None


@dataclass
class PlaybackSession:
    """一句下行音频的缓冲：mime 决定播放方式。"""

    mime: str
    sample_rate: int
    text: str
    chunks: list[bytes] = field(default_factory=list)


class SatelliteClient:
    def __init__(
        self,
        *,
        hub_url: str,
        access_token: str,
        room_id: str,
        max_privacy_level: str = "L1",
        continuous_timeout_seconds: float = 8.0,
        vad_threshold: float = VAD_ENERGY_THRESHOLD,
        wake_engine: Any | None = None,
        output_cmd: list[str] | None = None,
    ) -> None:
        self._hub_url = hub_url.rstrip("/")
        self._token = access_token
        self._room = room_id
        self._max_privacy = max_privacy_level
        self._continuous = continuous_timeout_seconds
        self._vad = EnergyVad(vad_threshold)
        self._wake = wake_engine
        # 非 PCM 时的外部播放命令（如 ffplay -nodisp -autoexit -）
        self._output_cmd = output_cmd
        self._ws: Any | None = None
        self.status = SatelliteState(room_id=room_id)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # 连接与主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        import sounddevice as sd  # 延迟导入：无音频设备的环境也能跑协议联调

        self._sd = sd
        url = self._hub_url.replace("http", "ws", 1) + "/ws/devices"
        async for websocket in websockets.connect(url, max_size=2**22):
            self._ws = websocket
            try:
                await self._send(
                    {
                        "proto_version": 1,
                        "type": "device.authenticate",
                        "access_token": self._token,
                        "capabilities": ["voice.satellite"],
                    },
                    signed=False,
                )
                accepted = json.loads(await websocket.recv())
                if accepted.get("type") != "device.accepted":
                    logger.error("鉴权失败: %s", accepted)
                    return
                logger.info(
                    "已连接设备 %s（心跳 %ss）",
                    accepted.get("device_id"),
                    accepted.get("heartbeat_interval_seconds"),
                )
                await self._send(
                    {
                        "proto_version": 1,
                        "type": "satellite.hello",
                        "room_id": self._room,
                        "firmware": "satellite-client/1.0",
                        "max_privacy_level": self._max_privacy,
                        "continuous_timeout_seconds": self._continuous,
                    }
                )
                heartbeat = asyncio.create_task(self._heartbeat_loop())
                audio_in = asyncio.create_task(self._mic_loop())
                try:
                    await self._receive_loop()
                finally:
                    heartbeat.cancel()
                    audio_in.cancel()
            except websockets.ConnectionClosed as error:
                logger.warning("连接断开（%s），5 秒后重连", error.code)
                await asyncio.sleep(5)
            finally:
                self.status.state = "idle"
                self.status.playing = False

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._send({"type": "device.heartbeat", "capabilities": ["voice.satellite"]})

    async def _send(self, frame: dict[str, Any], *, signed: bool = True) -> None:
        assert self._ws is not None
        frame.setdefault("sent_at", datetime.now(UTC).isoformat())
        if signed:
            frame = sign_frame(self._token, frame)
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 下行：状态、文本与音频分片
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        playback: PlaybackSession | None = None
        async for raw in self._ws:
            frame = json.loads(raw)
            frame_type = frame.get("type", "")
            if frame_type == "satellite.state.set":
                await self._on_state_set(frame)
            elif frame_type == "satellite.audio.chunk":
                if playback is not None:
                    playback.chunks.append(
                        base64.b64decode(frame["data_b64"], validate=True)
                    )
            elif frame_type == "voice.sentence":
                playback = PlaybackSession(
                    mime=frame.get("mime", "audio/pcm"),
                    sample_rate=int(frame.get("sample_rate", SAMPLE_RATE)),
                    text=frame.get("text", ""),
                )
                logger.info("Aria: %s", playback.text)
            elif frame_type == "voice.sentence.end":
                if playback is not None and playback.chunks:
                    await self._play(playback)
                playback = None
                # 回合终态由 Hub 在话语处理完成后自行应用（REPLY_DONE /
                # FOLLOW_UP_READY 并下发 state.set）；设备早上报会触发非法转移。
            elif frame_type == "satellite.error":
                logger.warning(
                    "Hub 错误帧: %s %s", frame.get("reason_code"), frame.get("detail", "")
                )
                self.status.state = "idle"
                self.status.playing = False
            elif frame_type == "satellite.wake.suppressed":
                logger.info("唤醒被压制（%s），本次由其他房间响应", frame.get("reason_code"))
            elif frame_type == "satellite.session.available":
                self.status.pending_takeover_from = frame.get("from_device_id")
                logger.info(
                    "其他房间有连续会话（至 %s）；按 t 接管到本房间",
                    frame.get("follow_up_until"),
                )
            elif frame_type == "satellite.cancelled":
                logger.info("回合已取消（%s）", frame.get("reason_code"))
                self.status.state = "idle"
                self.status.playing = False
            elif frame_type == "satellite.session.expired":
                logger.info("免唤醒窗口结束，回到待唤醒")
                self.status.state = "idle"
                self.status.follow_up_until = None

    async def _on_state_set(self, frame: dict[str, Any]) -> None:
        self.status.state = frame.get("state", "idle")
        follow_up = frame.get("follow_up")
        self.status.follow_up_until = frame.get("follow_up_until") if follow_up else None
        logger.info(
            "状态 → %s%s",
            self.status.state,
            f"（免唤醒窗口至 {self.status.follow_up_until}）"
            if self.status.follow_up_until
            else "",
        )

    async def _report_state(self, state: str, *, reason_code: str | None = None) -> None:
        frame: dict[str, Any] = {"proto_version": 1, "type": "satellite.state", "state": state}
        if reason_code:
            frame["reason_code"] = reason_code
        await self._send(frame)

    # ------------------------------------------------------------------
    # 播放（含 barge-in 静音）
    # ------------------------------------------------------------------

    async def _play(self, session: PlaybackSession) -> None:
        self.status.playing = True
        data = b"".join(session.chunks)
        try:
            if session.mime.startswith("audio/pcm"):
                await asyncio.to_thread(
                    self._sd.play,
                    np.frombuffer(data, dtype=np.int16),
                    session.sample_rate,
                    blocking=True,
                )
            elif self._output_cmd:
                import subprocess

                await asyncio.to_thread(
                    subprocess.run, self._output_cmd, input=data, check=False
                )
            else:
                logger.warning(
                    "TTS 输出为 %s 且未配置外部播放器（--player），跳过播放",
                    session.mime,
                )
        finally:
            self.status.playing = False

    # ------------------------------------------------------------------
    # 上行：唤醒 + 录音 + 分片上传
    # ------------------------------------------------------------------

    async def _mic_loop(self) -> None:
        """常开麦克风：唤醒监测 + listening 时录话语 + speaking 时 barge-in。"""
        audio_queue: queue.Queue[bytes] = queue.Queue()
        stream = self._sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_BYTES // 2,
            callback=lambda indata, frames, time_info, status: (
                audio_queue.put(bytes(indata))
            ),
        )
        with stream:
            while not self._stop.is_set():
                try:
                    frame_data = await asyncio.to_thread(audio_queue.get, timeout=0.5)
                except queue.Empty:
                    continue
                speech = self._vad.is_speech(frame_data)

                if self.status.state == "speaking" and speech:
                    logger.info("检测到 barge-in，打断播放")
                    await self._stop_playback()
                    await self._record_and_upload(barge_in=True, audio_queue=audio_queue)
                    continue
                elif self.status.state == "listening":
                    await self._record_and_upload(barge_in=False, audio_queue=audio_queue)
                elif self.status.state == "idle" and self._wake is not None:
                    if self._wake.process(frame_data):
                        await self._send_wake()
                await asyncio.sleep(0)

    async def _stop_playback(self) -> None:
        self._sd.stop()
        self.status.playing = False

    async def _send_wake(self) -> None:
        self._vad.speech_seen = True
        await self._send(
            {
                "proto_version": 1,
                "type": "satellite.wake",
                "room_id": self._room,
                "detected_at": datetime.now(UTC).isoformat(),
                "confidence": 1.0,
            }
        )
        logger.info("已上报唤醒，等待 Hub 仲裁…")

    async def _record_and_upload(self, *, barge_in: bool, audio_queue: Any) -> None:
        """从音频队列读取直到语毕（静音 hangover），随后分片上传。"""
        utterance_id = str(uuid4())
        buffer = bytearray()
        silence_ms = 0
        started = False
        idle_ms = 0

        while True:
            try:
                frame_data = await asyncio.to_thread(audio_queue.get, timeout=0.5)
            except queue.Empty:
                silence_ms += 500
                if started and silence_ms >= VAD_SILENCE_MS:
                    break
                if not started:
                    idle_ms += 500
                    if idle_ms >= 10_000:
                        return  # listening 但长时间无声，放弃本轮
                continue
            speech = self._vad.is_speech(frame_data)
            if speech:
                started = True
                silence_ms = 0
                buffer.extend(frame_data)
                if len(buffer) > MAX_UTTERANCE_BYTES:
                    logger.warning("话语超过 4MiB 上限，截断")
                    break
            elif started:
                silence_ms += FRAME_MS
                buffer.extend(frame_data)
                if silence_ms >= VAD_SILENCE_MS:
                    break

        if not started or len(buffer) < SAMPLE_RATE // 4 * 2:  # <0.25s 视为误触发
            return

        await self._send(
            {
                "proto_version": 1,
                "type": "satellite.audio.start",
                "utterance_id": utterance_id,
                "format": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "privacy_level": self._max_privacy,
                "barge_in": barge_in,
            }
        )
        chunks = 0
        for offset in range(0, len(buffer), CHUNK_BYTES):
            piece = bytes(buffer[offset : offset + CHUNK_BYTES])
            await self._send(
                {
                    "proto_version": 1,
                    "type": "satellite.audio.chunk",
                    "utterance_id": utterance_id,
                    "index": chunks,
                    "data_b64": base64.b64encode(piece).decode("ascii"),
                }
            )
            chunks += 1
        payload = bytes(buffer)
        await self._send(
            {
                "proto_version": 1,
                "type": "satellite.audio.end",
                "utterance_id": utterance_id,
                "chunks": chunks,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
        if self._debug_dir:
            (self._debug_dir / f"utterance-{utterance_id}.wav").write_bytes(
                _wrap_wav(payload)
            )
        logger.info("话语 %s 已上传（%.1fs）", utterance_id, len(payload) / (SAMPLE_RATE * 2))

    _debug_dir: Path | None = None


def _wrap_wav(pcm: bytes) -> bytes:
    import io

    handle = io.BytesIO()
    with wave.open(handle, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)
    return handle.getvalue()


class PushToTalkWake:
    """无唤醒词模型的兜底：终端回车即唤醒（由 stdin 任务直接触发）。"""

    def process(self, frame: bytes) -> bool:
        del frame
        return False


def build_wake_engine(name: str) -> tuple[Any, bool]:
    """返回 (engine, needs_manual)。manual=True 时监听回车触发唤醒。"""
    if name == "none":
        return PushToTalkWake(), True
    if name == "openwakeword":
        from openwakeword.model import Model as WakeModel  # type: ignore[import-not-found]

        return WakeModel(), False
    raise ValueError(f"未知唤醒引擎: {name}")


async def stdin_wake_trigger(client: SatelliteClient) -> None:
    """回车触发唤醒（--wake none 模式）或接管（t 键）。"""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        line = line.strip().lower()
        if line == "t" and client.status.pending_takeover_from:
            await client._send(
                {
                    "proto_version": 1,
                    "type": "satellite.takeover",
                    "from_device_id": client.status.pending_takeover_from,
                }
            )
            client.status.pending_takeover_from = None
        elif line == "":
            await client._send_wake()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aria 全屋语音卫星客户端")
    parser.add_argument("--hub", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="设备访问令牌（Admin 配对获得）")
    parser.add_argument("--room", required=True, help="房间标识（如 bedroom）")
    parser.add_argument("--privacy", choices=["L0", "L1", "L2"], default="L1")
    parser.add_argument("--continuous", type=float, default=8.0, help="免唤醒窗口秒数")
    parser.add_argument("--wake", choices=["none", "openwakeword"], default="none")
    parser.add_argument(
        "--player", default=None, help="MP3 播放命令（如 'ffplay -nodisp -autoexit -'）"
    )
    parser.add_argument("--vad-threshold", type=float, default=VAD_ENERGY_THRESHOLD)
    parser.add_argument("--debug-dir", default=None, help="保存上行话语 WAV（调试用）")
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wake_engine, needs_manual = build_wake_engine(args.wake)
    client = SatelliteClient(
        hub_url=args.hub,
        access_token=args.token,
        room_id=args.room,
        max_privacy_level=args.privacy,
        continuous_timeout_seconds=args.continuous,
        vad_threshold=args.vad_threshold,
        wake_engine=wake_engine,
        output_cmd=args.player.split() if args.player else None,
    )
    client._debug_dir = Path(args.debug_dir) if args.debug_dir else None
    if client._debug_dir:
        client._debug_dir.mkdir(parents=True, exist_ok=True)
    tasks = [asyncio.create_task(client.run())]
    if needs_manual:
        tasks.append(asyncio.create_task(stdin_wake_trigger(client)))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
