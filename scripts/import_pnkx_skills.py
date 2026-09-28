"""Import generated PNKX skill packages into the running Hub as disabled drafts.

Usage: PYTHONPATH=server .venv/bin/python scripts/import_pnkx_skills.py [--base URL]
Reads ARIA_ADMIN_TOKEN from .env.local. Also merges every contract read path into
the pnkx-admin connection whitelist (connection stays in its current enabled state).
"""

from __future__ import annotations

import io
import json
import sys
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from gen_pnkx_skills import SKILLS, render_package  # noqa: E402

BASE = "http://127.0.0.1:8000"
API = "/api/v1/admin/skills"


def token() -> str:
    for line in (ROOT / ".env.local").read_text().splitlines():
        if line.startswith("ARIA_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("ARIA_ADMIN_TOKEN not found in .env.local")


def request(method: str, path: str, body: bytes | None = None, content_type: str | None = None):
    req = Request(BASE + path, method=method, data=body)
    req.add_header("Authorization", f"Bearer {token()}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urlopen(req, timeout=30) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except HTTPError as error:
        detail = error.read().decode()[:400]
        return error.code, {"error": detail}


def zip_package(name: str, markdown: str, api_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}/SKILL.md", markdown)
        archive.writestr(f"{name}/aria-api.yaml", api_text)
    return buffer.getvalue()


def multipart(name: str, filename: str, data: bytes) -> tuple[str, bytes]:
    boundary = f"----pnkx{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def main() -> None:
    existing = request("GET", API)
    if existing[0] != 200:
        raise SystemExit(f"admin API unreachable: {existing}")
    by_name = {item["name"]: item for item in existing[1]}

    paths_by_connection: dict[str, set[str]] = {}
    for name, connection, description, instructions, operations in SKILLS:
        for operation in operations:
            if operation["risk"] == "read":
                paths_by_connection.setdefault(connection, set()).add(operation["path"])
        markdown, api_text = render_package(name, connection, description, instructions, operations)
        if name in by_name:
            installed = by_name[name]
            status, detail = request("GET", f"{API}/{installed['id']}")
            manifest = detail.get("api") if status == 200 and detail else None
            if manifest and manifest.get("connection") != connection:
                manifest = {**manifest, "connection": connection}
                status, view = request(
                    "PUT",
                    f"{API}/{installed['id']}/api",
                    json.dumps(manifest).encode(),
                    "application/json",
                )
                note = (
                    "re-point SKIPPED (enabled, edit needs disable)"
                    if status == 409
                    else f"re-pointed -> v{view.get('version')}"
                )
            else:
                note = "connection already correct"
            print(f"skip {name}: already installed ({note})")
            continue
        content_type, body = multipart("file", f"{name}.zip", zip_package(name, markdown, api_text))
        status, view = request("POST", f"{API}/import", body, content_type)
        if status not in (200, 201, 409):
            raise SystemExit(f"import {name} failed: {status} {view}")
        print(f"{name}: imported disabled v{view.get('version') if status == 201 else view}")

    connection = request("GET", f"{API}/connections")
    if connection[0] != 200:
        raise SystemExit(f"connections listing failed: {connection}")
    known = {item["id"]: item for item in connection[1]}
    admin = known.get("pnkx-admin")
    if admin is None:
        raise SystemExit("pnkx-admin connection missing; configure it in Admin first")

    for target, paths in sorted(paths_by_connection.items()):
        current = known.get(target)
        base = current or {**admin, "id": target, "allowed_paths": [], "enabled": True}
        merged = list(dict.fromkeys([*base["allowed_paths"], *sorted(paths)]))
        if len(merged) > 50:
            raise SystemExit(f"{target}: whitelist would hold {len(merged)} paths (max 50)")
        if current is not None and merged == current["allowed_paths"]:
            print(f"{target}: whitelist unchanged ({len(merged)} paths)")
            continue
        payload = {
            "id": target,
            "base_url": base["base_url"],
            "auth_type": base["auth_type"],
            "secret_ref": base.get("secret_ref"),
            "username_ref": base.get("username_ref"),
            "header_name": base.get("header_name"),
            "allowed_paths": merged,
            "allowed_auth_paths": base["allowed_auth_paths"],
            "enabled": base["enabled"],
        }
        status, view = request(
            "PUT",
            f"{API}/connections/{target}",
            json.dumps(payload).encode(),
            "application/json",
        )
        action = "merged" if current else "created"
        print(f"{target}: {action} whitelist -> {len(merged)} paths: HTTP {status}")
        if status != 200:
            raise SystemExit(view)


if __name__ == "__main__":
    main()
