"""MEET-01 会议录音客户端：本机录音 + 本地转写，只把文本上传给 Hub。

设计约束（与 Hub 侧契约一致）：
- 原始音频永不离开本机：sounddevice 采集 → WAV 分段 → faster-whisper 本地转写
  → 分段删除音频，仅上传 TranscriptSegment（说话人 + 文本 + 时间戳）；
- 说话人必须在会议参与人（或本人）名单内，由录音者当场切换标注；
- 转写上传前必须经用户显式授权（终端确认），可随时撤销；
- 撤销后立即停止上传；已上传文本由 Hub 侧授权门禁管理。

用法示例：
    pip install -r requirements.txt
    python recorder.py --hub http://127.0.0.1:8000 prepare --title "周会" \
        --participants 阿莉娅,裴先生
    python recorder.py --hub http://127.0.0.1:8000 record --meeting <id> \
        --speakers 阿莉娅,裴先生
    python recorder.py --hub http://127.0.0.1:8000 finish --meeting <id>
"""

from __future__ import annotations

import argparse
import asyncio
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

RECORD_SAMPLE_RATE = 16_000
RECORD_CHANNELS = 1
CHUNK_SECONDS = 30
UPLOAD_BATCH = 40  # Hub 单次上限 50，留余量


@dataclass(slots=True)
class HubSession:
    base_url: str
    access_token: str

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


class HubClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self, password: str) -> HubSession:
        response = await self._client.post(
            f"{self._base_url}/api/v1/auth/login", json={"password": password}
        )
        response.raise_for_status()
        return HubSession(self._base_url, response.json()["access_token"])

    async def _request(
        self, session: HubSession, method: str, path: str, *, json: Any = None
    ) -> httpx.Response:
        response = await self._client.request(
            method, f"{self._base_url}{path}", json=json, headers=session.headers()
        )
        response.raise_for_status()
        return response

    async def prepare_meeting(
        self,
        session: HubSession,
        *,
        title: str | None,
        participants: list[str],
        privacy_level: str = "L1",
        calendar_event_id: UUID | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"participants": participants, "privacy_level": privacy_level}
        if title:
            body["title"] = title
        if calendar_event_id:
            body["calendar_event_id"] = str(calendar_event_id)
        response = await self._request(session, "POST", "/api/v1/meetings", json=body)
        return response.json()

    async def list_meetings(self, session: HubSession) -> list[dict[str, Any]]:
        response = await self._request(session, "GET", "/api/v1/meetings")
        return response.json()

    async def authorize(self, session: HubSession, meeting_id: UUID) -> dict[str, Any]:
        response = await self._request(
            session,
            "POST",
            f"/api/v1/meetings/{meeting_id}/transcription/authorize",
            json={"authorized": True},
        )
        return response.json()

    async def revoke(self, session: HubSession, meeting_id: UUID) -> dict[str, Any]:
        response = await self._request(
            session, "POST", f"/api/v1/meetings/{meeting_id}/transcription/revoke"
        )
        return response.json()

    async def append_transcript(
        self, session: HubSession, meeting_id: UUID, segments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        response = await self._request(
            session,
            "POST",
            f"/api/v1/meetings/{meeting_id}/transcript",
            json={"segments": segments},
        )
        return response.json()

    async def finish(self, session: HubSession, meeting_id: UUID) -> dict[str, Any]:
        response = await self._request(
            session, "POST", f"/api/v1/meetings/{meeting_id}/finish"
        )
        return response.json()


class LocalTranscriber:
    """faster-whisper 本地转写；模型按需加载（首次下载到本地缓存）。"""

    def __init__(
        self,
        *,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        initial_prompt: str | None = None,
    ) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._initial_prompt = initial_prompt

    def transcribe(self, wav_path: Path) -> str:
        segments, _info = self._model.transcribe(
            str(wav_path), language="zh", initial_prompt=self._initial_prompt, vad_filter=True
        )
        return "".join(segment.text for segment in segments).strip()


def write_wav(path: Path, frames: bytes, *, sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)  # int16
        handle.setframerate(sample_rate)
        handle.writeframes(frames)


async def record_and_upload(
    hub: HubClient,
    session: HubSession,
    meeting_id: UUID,
    *,
    speakers: list[str],
    transcriber: LocalTranscriber,
    chunk_seconds: int = CHUNK_SECONDS,
    scratch_dir: Path | None = None,
    input_timeout: float | None = None,
) -> int:
    """主录音循环：按块录音 → 本地转写 → 选择说话人 → 上传文本。

    返回已上传的 segment 数。Ctrl+C 或空行回车结束（先 finish 由调用方决定）。
    """
    import sounddevice as sd

    scratch = scratch_dir or Path(".meeting-scratch")
    scratch.mkdir(exist_ok=True)
    current_speaker = speakers[0]
    started_at = datetime.now(UTC)
    uploaded = 0
    chunk_index = 0

    print(f"开始录音（每段 {chunk_seconds}s）。当前说话人：{current_speaker}")
    print("段间输入序号切换说话人，直接回车保持；输入 q 结束录音。")
    try:
        while True:
            print(f"● 录音中… 段 {chunk_index + 1}（{current_speaker}）")
            frames = await asyncio.to_thread(
                sd.rec,
                chunk_seconds * RECORD_SAMPLE_RATE,
                samplerate=RECORD_SAMPLE_RATE,
                channels=RECORD_CHANNELS,
                dtype="int16",
            )
            await asyncio.to_thread(sd.wait)
            wav_path = scratch / f"chunk-{chunk_index:04d}.wav"
            write_wav(
                wav_path,
                frames.tobytes(),
                sample_rate=RECORD_SAMPLE_RATE,
                channels=RECORD_CHANNELS,
            )
            text = await asyncio.to_thread(transcriber.transcribe, wav_path)
            wav_path.unlink(missing_ok=True)  # 音频即焚：转写完成立即删除
            if text:
                segments = [
                    {
                        "speaker": current_speaker,
                        "text": text[:4000],
                        "started_at": (
                            started_at + timedelta(seconds=chunk_index * chunk_seconds)
                        ).isoformat(),
                    }
                ]
                await hub.append_transcript(session, meeting_id, segments)
                uploaded += len(segments)
                print(f"  ↳ 已上传 {uploaded} 段")
            else:
                print("  ↳ 本段无语音，跳过")
            chunk_index += 1

            choice = await asyncio.to_thread(_prompt_speaker, speakers, current_speaker)
            if choice == "q":
                break
            if choice is not None:
                current_speaker = speakers[choice]
    except KeyboardInterrupt:
        print("\n收到中断，停止录音。")
    finally:
        for leftover in scratch.glob("chunk-*.wav"):
            leftover.unlink(missing_ok=True)
    return uploaded


def _prompt_speaker(speakers: list[str], current: str) -> int | str | None:
    """返回序号（切换）、'q'（结束）或 None（保持）。"""
    try:
        raw = input(f"说话人 [{'/'.join(speakers)}] 当前 {current}（回车保持, q 结束）: ").strip()
    except EOFError:
        return "q"
    if raw.lower() == "q":
        return "q"
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(speakers):
        return int(raw) - 1
    return None


async def _login_flow(hub: HubClient, password: str | None) -> HubSession:
    import getpass

    secret = password or getpass.getpass("Aria 聊天密码: ")
    return await hub.login(secret)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aria MEET-01 会议录音客户端")
    parser.add_argument("--hub", default="http://127.0.0.1:8000", help="Hub 基地址")
    parser.add_argument("--password", default=None, help="聊天密码（缺省交互输入）")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="创建会议（可从日历生成简报）")
    prepare.add_argument("--title", default=None)
    prepare.add_argument("--participants", default="", help="逗号分隔的参与人名单")
    prepare.add_argument("--privacy", choices=["L1", "L2"], default="L1")
    prepare.add_argument("--calendar-event", default=None, help="日历事件 ID")

    record = sub.add_parser("record", help="录音 + 本地转写 + 上传文本")
    record.add_argument("--meeting", required=True)
    record.add_argument("--speakers", required=True, help="逗号分隔的说话人（须为参与人）")
    record.add_argument("--model", default="base", help="faster-whisper 模型")
    record.add_argument("--chunk-seconds", type=int, default=CHUNK_SECONDS)
    record.add_argument("--hotwords", default="小艾,Aria", help="转写偏置词（逗号分隔）")

    finish = sub.add_parser("finish", help="结束会议并触发摘要")
    finish.add_argument("--meeting", required=True)

    revoke = sub.add_parser("revoke", help="撤销转写授权")
    revoke.add_argument("--meeting", required=True)

    sub.add_parser("list", help="列出会议")
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    hub = HubClient(args.hub)
    try:
        session = await _login_flow(hub, args.password)

        if args.command == "prepare":
            participants = [p.strip() for p in args.participants.split(",") if p.strip()]
            meeting = await hub.prepare_meeting(
                session,
                title=args.title,
                participants=participants,
                privacy_level=args.privacy,
                calendar_event_id=UUID(args.calendar_event) if args.calendar_event else None,
            )
            print(f"会议已创建: {meeting['id']} 状态 {meeting['status']}")
            return 0

        if args.command == "list":
            for meeting in await hub.list_meetings(session):
                print(f"{meeting['id']}  {meeting.get('title') or '(无标题)'}  {meeting['status']}")
            return 0

        meeting_id = UUID(args.meeting)

        if args.command == "revoke":
            await hub.revoke(session, meeting_id)
            print("已撤销转写授权；录音端应立即停止上传。")
            return 0

        if args.command == "finish":
            meeting = await hub.finish(session, meeting_id)
            print(f"会议已结束: {meeting['status']}")
            return 0

        if args.command == "record":
            speakers = [s.strip() for s in args.speakers.split(",") if s.strip()]
            if not speakers:
                print("至少提供一个说话人")
                return 2
            answer = input("授权上传本次转写文本到 Hub吗 (yes/no): ").strip().lower()
            if answer != "yes":
                print("未授权，退出。原始音频与转写均保留在本机。")
                return 2
            await hub.authorize(session, meeting_id)
            transcriber = LocalTranscriber(
                model_size=args.model, initial_prompt=f"术语提示：{args.hotwords}"
            )
            uploaded = await record_and_upload(
                hub,
                session,
                meeting_id,
                speakers=speakers,
                transcriber=transcriber,
                chunk_seconds=args.chunk_seconds,
            )
            print(f"录音结束，共上传 {uploaded} 段文本。运行 finish 结束会议并生成摘要。")
            return 0
        return 1
    finally:
        await hub.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
