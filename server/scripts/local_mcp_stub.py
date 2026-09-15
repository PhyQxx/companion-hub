"""本地 MCP 调试 Server（MCP-D 验收用，docs/06 §3 允许的回环 HTTP 场景）。

提供一对工具：
- notes_get（只读）：读取指定 ID 的笔记；
- notes_create（写）：创建笔记并落盘 notes.jsonl，供跨进程回读验证。

鉴权：Bearer 令牌（--token，默认 dev-token）。仅监听 127.0.0.1。

用法：
    uv run python server/scripts/local_mcp_stub.py --port 9765 --token dev-token
然后在 Hub 配置 mcp.servers 增加：
    server_id: local
    endpoint: http://127.0.0.1:9765/mcp
    allow_insecure_local_http: true
    allow_write_tools: true
    allowed_tools: [notes_get, notes_create]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

STATE_PATH = os.path.join(os.path.dirname(__file__), "local_mcp_stub_notes.jsonl")


def _load_notes() -> dict[str, dict[str, str]]:
    notes: dict[str, dict[str, str]] = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    note = json.loads(line)
                    notes[note["id"]] = note
    return notes


def _append_note(note: dict[str, str]) -> None:
    with open(STATE_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(note, ensure_ascii=False) + "\n")


def build_server() -> MCPServer:
    server = MCPServer(name="Local Notes Stub", version="1.0.0")

    @server.tool(
        name="notes_get",
        title="Get Note",
        description="按 ID 读取本地笔记。",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    def notes_get(note_id: str) -> str:
        note = _load_notes().get(note_id)
        return json.dumps(note, ensure_ascii=False) if note else f"note not found: {note_id}"

    @server.tool(
        name="notes_create",
        title="Create Note",
        description="创建一条本地笔记（写操作）。",
        annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
    )
    def notes_create(title: str, body: str = "") -> str:
        existing = _load_notes()
        note_id = f"n{len(existing) + 1:04d}"
        note = {"id": note_id, "title": title, "body": body}
        _append_note(note)
        return json.dumps({"created": True, "note": note}, ensure_ascii=False)

    return server


class BearerAuthMiddleware:
    """ASGI 中间件：校验 Authorization: Bearer <token>，不匹配返回 401。"""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        if headers.get("authorization") != f"Bearer {self.token}":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"unauthorized"}',
                }
            )
            return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="local MCP stub server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9765)
    parser.add_argument("--token", default="dev-token")
    args = parser.parse_args()

    import uvicorn

    server = build_server()
    app = BearerAuthMiddleware(server.streamable_http_app(), args.token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return sys.exit(0)


if __name__ == "__main__":
    main()
