from __future__ import annotations

import hashlib
import io
import json
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .store import AvatarInstanceView, AvatarPackView, AvatarStore

MAX_STATIC_BYTES = 10 * 1024 * 1024
MAX_STATIC_PIXELS = 25_000_000
MAX_LIVE2D_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_LIVE2D_FILES = 256
MAX_LIVE2D_EXPANDED_BYTES = 150 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

_LIVE2D_SUFFIXES = (
    ".model3.json",
    ".motion3.json",
    ".exp3.json",
    ".physics3.json",
    ".pose3.json",
    ".userdata3.json",
    ".json",
    ".moc3",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
)


@dataclass(frozen=True, slots=True)
class AvatarImportResult:
    pack: AvatarPackView
    instance: AvatarInstanceView


class AvatarAssetImporter:
    """Validate and install user-owned avatar assets without executing package content."""

    def __init__(self, root: Path, avatar_store: AvatarStore) -> None:
        self._root = root.resolve()
        self._store = avatar_store
        self._root.mkdir(parents=True, exist_ok=True)

    async def import_static(
        self,
        data: bytes,
        *,
        name: str,
        rights_confirmed: bool,
    ) -> AvatarImportResult:
        if not rights_confirmed:
            raise ValueError("请先确认你拥有该立绘的使用权")
        normalized_name = self._validate_name(name)
        if not data or len(data) > MAX_STATIC_BYTES:
            raise ValueError("立绘文件必须小于 10 MB")

        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("仅支持 PNG、JPEG 或 WebP 立绘")
                width, height = source.size
                if width < 64 or height < 64:
                    raise ValueError("立绘尺寸至少为 64 x 64")
                if width * height > MAX_STATIC_PIXELS:
                    raise ValueError("立绘像素总量不能超过 2500 万")
                image = ImageOps.exif_transpose(source)
                image.load()
                image = image.convert("RGBA")
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError("无法识别或文件已损坏") from error

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        sanitized = output.getvalue()
        digest = hashlib.sha256(sanitized).hexdigest()
        relative = Path("static") / digest[:2] / f"{digest}.png"
        target = self._safe_target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(sanitized)

        asset_url = f"/api/v1/avatar-user-assets/{relative.as_posix()}"
        pack = await self._store.install_pack(
            pack_id=f"custom-static-{digest[:16]}",
            name=normalized_name,
            archetype="custom-static",
            engine="static",
            manifest={
                "schema_version": 1,
                "emotions": ["neutral"],
                "gestures": [],
                "outfits": [],
                "touch_regions": [],
                "supports_viseme": False,
                "fallbacks": {},
                "customizable_slots": [],
                "assets": {
                    "thumbnail": asset_url,
                    "emotions": {"neutral": asset_url},
                    "width": image.width,
                    "height": image.height,
                },
            },
            content_hash=digest,
            license_info={
                "author": "local-user",
                "usage": "user-confirmed",
                "rights_confirmed": True,
                "source_hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
            },
        )
        instance = await self._store.create_instance(pack.id, normalized_name)
        return AvatarImportResult(pack=pack, instance=instance)

    async def import_live2d(
        self,
        data: bytes,
        *,
        name: str,
        rights_confirmed: bool,
    ) -> AvatarImportResult:
        if not rights_confirmed:
            raise ValueError("请先确认你拥有该 Live2D 模型的使用权")
        normalized_name = self._validate_name(name)
        if not data or len(data) > MAX_LIVE2D_ARCHIVE_BYTES:
            raise ValueError("Live2D 模型包必须小于 50 MB")

        archive_hash = hashlib.sha256(data).hexdigest()
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as error:
            raise ValueError("Live2D 模型包不是有效的 ZIP 文件") from error

        with archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if not files or len(files) > MAX_LIVE2D_FILES:
                raise ValueError("Live2D 模型包文件数量必须在 1 到 256 之间")
            expanded = sum(entry.file_size for entry in files)
            if expanded > MAX_LIVE2D_EXPANDED_BYTES:
                raise ValueError("Live2D 模型包解压后不能超过 150 MB")

            safe_names: set[str] = set()
            entries_by_name: dict[str, zipfile.ZipInfo] = {}
            for entry in files:
                path = self._validate_archive_path(entry.filename)
                mode = entry.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise ValueError("Live2D 模型包不能包含符号链接")
                if entry.file_size and entry.compress_size == 0:
                    raise ValueError("Live2D 模型包压缩信息异常")
                if (
                    entry.compress_size
                    and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ValueError("Live2D 模型包压缩率异常")
                if path.as_posix() in safe_names:
                    raise ValueError(f"Live2D 模型包包含重复路径: {entry.filename}")
                safe_names.add(path.as_posix())
                entries_by_name[path.as_posix()] = entry

            model_files = sorted(
                (name for name in safe_names if name.lower().endswith(".model3.json")),
                key=self._model_candidate_priority,
                reverse=True,
            )
            if not model_files:
                raise ValueError("Live2D 模型包必须包含至少一个 .model3.json")

            model_path: PurePosixPath | None = None
            model: dict[str, Any] | None = None
            candidate_errors: list[str] = []
            for candidate in model_files:
                candidate_path = PurePosixPath(candidate)
                try:
                    candidate_model = json.loads(archive.read(candidate))
                    if not isinstance(candidate_model, dict) or not isinstance(
                        candidate_model.get("FileReferences"), dict
                    ):
                        raise ValueError("缺少 FileReferences")
                    self._validate_model_references(
                        candidate_path,
                        candidate_model["FileReferences"],
                        safe_names,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                    candidate_errors.append(f"{candidate}: {error}")
                    continue
                model_path = candidate_path
                model = candidate_model
                break
            if model_path is None or model is None:
                reason = candidate_errors[0] if candidate_errors else "没有可用模型"
                raise ValueError(f"Live2D 模型包没有可导入的 runtime: {reason}")

            selected_files: list[zipfile.ZipInfo] = []
            for entry_name, entry in entries_by_name.items():
                path = PurePosixPath(entry_name)
                if not path.is_relative_to(model_path.parent):
                    continue
                if not entry_name.lower().endswith(_LIVE2D_SUFFIXES):
                    raise ValueError(f"选中的 runtime 包含不允许的文件: {entry_name}")
                selected_files.append(entry)

            relative_root = Path("live2d") / archive_hash[:2] / archive_hash
            target_root = self._safe_target(relative_root)
            if not target_root.exists():
                target_root.mkdir(parents=True, exist_ok=True)
                try:
                    for entry in selected_files:
                        relative_file = Path(self._validate_archive_path(entry.filename).as_posix())
                        target = (target_root / relative_file).resolve()
                        if not target.is_relative_to(target_root):
                            raise ValueError("Live2D 模型包路径越界")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                except Exception:
                    shutil.rmtree(target_root, ignore_errors=True)
                    raise

        model_url = (
            f"/api/v1/avatar-user-assets/{relative_root.as_posix()}/{model_path.as_posix()}"
        )
        pack = await self._store.install_pack(
            pack_id=f"custom-live2d-{archive_hash[:16]}",
            name=normalized_name,
            archetype="custom-live2d",
            engine="live2d",
            manifest={
                "schema_version": 1,
                "model": model_url,
                "emotions": [],
                "gestures": [],
                "outfits": [],
                "touch_regions": [],
                "supports_viseme": True,
                "fallbacks": {},
                "customizable_slots": [],
                "assets": {"model": model_url},
                "source_package": {
                    "selected_model": model_path.as_posix(),
                    "models_detected": model_files,
                    "ignored_files": len(files) - len(selected_files),
                },
                "runtime": {
                    "adapter": "cubism-web",
                    "core_required": True,
                    "network_access": False,
                },
            },
            content_hash=archive_hash,
            license_info={
                "author": "local-user",
                "usage": "user-confirmed",
                "rights_confirmed": True,
                "source_hash": f"sha256:{archive_hash}",
            },
        )
        instance = await self._store.create_instance(pack.id, normalized_name)
        return AvatarImportResult(pack=pack, instance=instance)

    def _safe_target(self, relative: Path) -> Path:
        target = (self._root / relative).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("资源路径越界")
        return target

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = name.strip()
        if not normalized or len(normalized) > 160:
            raise ValueError("形象名称长度必须在 1 到 160 个字符之间")
        return normalized

    @staticmethod
    def _validate_archive_path(name: str) -> PurePosixPath:
        normalized = name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"模型包包含不安全路径: {name}")
        if ":" in path.parts[0] or any(part.startswith(".") for part in path.parts):
            raise ValueError(f"模型包包含不安全路径: {name}")
        return path

    @staticmethod
    def _model_candidate_priority(name: str) -> tuple[int, int, int, str]:
        """Prefer a PRO runtime, then runtime directories, then shallower paths."""
        path = PurePosixPath(name)
        lowered_parts = [part.lower() for part in path.parts]
        pro_variant = int(any("pro" in part for part in lowered_parts))
        runtime_dir = int("runtime" in lowered_parts)
        return pro_variant, runtime_dir, -len(path.parts), name

    @classmethod
    def _validate_model_references(
        cls,
        model_path: PurePosixPath,
        references: dict[str, Any],
        safe_names: set[str],
    ) -> None:
        values: list[str] = []
        for key in ("Moc", "Physics", "Pose", "UserData"):
            value = references.get(key)
            if isinstance(value, str):
                values.append(value)
        textures = references.get("Textures", [])
        if isinstance(textures, list):
            values.extend(value for value in textures if isinstance(value, str))
        expressions = references.get("Expressions", [])
        if isinstance(expressions, list):
            values.extend(
                item["File"]
                for item in expressions
                if isinstance(item, dict) and isinstance(item.get("File"), str)
            )
        motions = references.get("Motions", {})
        if isinstance(motions, dict):
            for group in motions.values():
                if isinstance(group, list):
                    values.extend(
                        item["File"]
                        for item in group
                        if isinstance(item, dict) and isinstance(item.get("File"), str)
                    )
        if not values:
            raise ValueError("model3.json 未声明任何模型资源")
        for value in values:
            reference = cls._validate_archive_path(value)
            resolved = model_path.parent.joinpath(reference)
            if ".." in resolved.parts or resolved.as_posix() not in safe_names:
                raise ValueError(f"model3.json 引用了缺失或越界的资源: {value}")
