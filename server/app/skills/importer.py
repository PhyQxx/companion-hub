"""Safe ZIP import for Agent Skills packages."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError

from .models import SkillApiManifest, SkillDocument

MAX_ZIP_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 80


def parse_skill_markdown(markdown: str, api_text: str | None = None) -> SkillDocument:
    if not markdown.startswith("---\n"):
        raise ValueError("SKILL.md requires YAML frontmatter")
    parts = markdown.split("---\n", 2)
    if len(parts) != 3:
        raise ValueError("SKILL.md frontmatter is incomplete")
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as error:
        raise ValueError("invalid SKILL.md frontmatter") from error
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must be an object")
    api: SkillApiManifest | None = None
    try:
        if api_text is not None:
            raw_api: Any = yaml.safe_load(api_text)
            api = SkillApiManifest.model_validate(raw_api)
        return SkillDocument.model_validate(
            {
                "name": metadata.get("name"),
                "description": metadata.get("description"),
                "instructions": parts[2].strip(),
                "api": api,
            }
        )
    except (yaml.YAMLError, ValidationError) as error:
        raise ValueError("invalid skill metadata or API contract") from error


def import_skill_zip(data: bytes) -> tuple[SkillDocument, str, str]:
    """Return (document, archive digest, original SKILL.md text)."""
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("skill archive too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError("invalid skill archive") from error
    with archive:
        files: dict[str, bytes] = {}
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError("too many skill files")
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                info.filename.startswith("/")
                or "\\" in info.filename
                or any(part in {"", ".", ".."} for part in path.parts)
                or info.flag_bits & 1
                or ((info.external_attr >> 16) & 0o170000) == 0o120000
            ):
                raise ValueError("unsafe skill archive path")
            if info.is_dir():
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError("skill file too large")
            total += info.file_size
            if total > MAX_EXPANDED_BYTES:
                raise ValueError("expanded skill archive too large")
            if info.filename in files:
                raise ValueError("duplicate skill archive path")
            try:
                files[info.filename] = archive.read(info)
            except (RuntimeError, zipfile.BadZipFile) as error:
                raise ValueError("invalid skill archive contents") from error
        candidates = [name for name in files if name == "SKILL.md" or name.endswith("/SKILL.md")]
        if len(candidates) != 1:
            raise ValueError("archive must contain one SKILL.md")
        skill_path = candidates[0]
        prefix = skill_path.removesuffix("SKILL.md")
        if prefix and "/" in prefix.rstrip("/"):
            raise ValueError("skill root must be top-level")
        api_path = prefix + "aria-api.yaml"
        markdown = files[skill_path].decode("utf-8")
        api_text = files[api_path].decode("utf-8") if api_path in files else None
        document = parse_skill_markdown(markdown, api_text)
        if prefix and prefix.rstrip("/") != document.name:
            raise ValueError("skill directory must match skill name")
        return document, hashlib.sha256(data).hexdigest(), markdown
