"""Compile explicit Markdown endpoint tables without asking a model to reproduce them."""

from __future__ import annotations

import re
from pathlib import PurePath

from .models import SkillApiManifest, SkillDocument, SkillLoginAuth, SkillOperation, SkillParameter

_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`?(/[^`|\s]+)`?\s*\|\s*([^|]+)\|",
    re.MULTILINE,
)
_HEADING = re.compile(
    r"^###\s+\d+(?:\.\d+)?\s+(GET|POST|PUT|PATCH|DELETE)\s+`?(/[^`\s]+)`?",
    re.MULTILINE,
)
_FIELD = re.compile(r"`([A-Za-z][A-Za-z0-9_]*)`")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_PATH_ARG = re.compile(r"\{([a-z][A-Za-z0-9_]*)\}")
_FILE_EXPORT = re.compile(r"导出|下载|excel|csv|pdf|文件流", re.IGNORECASE)
_INT_NAMES = {"id", "ids", "userId", "cardId", "pageNum", "pageSize", "score", "money", "number"}


def _operation_name(method: str, path: str) -> str:
    words = _CAMEL.sub(r"\1_\2", path.strip("/"))
    words = re.sub(r"\{([a-z][A-Za-z0-9_]*)\}", r"by_\1", words)
    words = re.sub(r"[^a-zA-Z0-9]+", "_", words).lower().strip("_")
    return f"{method.lower()}_{words}"[:50].rstrip("_")


def _section_map(source: str) -> dict[tuple[str, str], str]:
    matches = list(_HEADING.finditer(source))
    return {
        (match.group(1), match.group(2)): source[match.end() : matches[index + 1].start()]
        if index + 1 < len(matches)
        else source[match.end() :]
        for index, match in enumerate(matches)
    }


def _params(method: str, path: str, section: str) -> dict[str, SkillParameter]:
    params: dict[str, SkillParameter] = {
        name: SkillParameter(
            type="integer" if name in _INT_NAMES and name != "ids" else "string",
            required=True,
            location="path",
        )
        for name in _PATH_ARG.findall(path)
    }
    label = "Query" if method == "GET" else "Body"
    detail = next((line for line in section.splitlines() if line.strip().startswith(label)), "")
    if "：" in detail:
        fields = detail.split("：", 1)[1].split("。", 1)[0]
        for segment in re.split(r"[、+]", fields):
            found = _FIELD.search(segment)
            if found is None:
                continue
            name = found.group(1)
            if name in params:
                continue
            params[name] = SkillParameter(
                type="integer" if name in _INT_NAMES else "string",
                required="必填" in segment,
                location="query" if method == "GET" else "body",
            )
        if method == "GET" and "分页" in fields:
            for name in ("pageNum", "pageSize"):
                params.setdefault(
                    name, SkillParameter(type="integer", required=False, location="query")
                )
    elif method != "GET" and detail.startswith("Body 必带"):
        found = _FIELD.search(detail)
        if found:
            name = found.group(1)
            params[name] = SkillParameter(
                type="integer" if name in _INT_NAMES else "string",
                required=True,
                location="body",
            )
    return dict(list(params.items())[:12])


def compile_markdown_api(
    source: str, *, system_name: str, filename: str | None = None
) -> tuple[SkillDocument, list[str], list[str]] | None:
    """Use only endpoints explicitly listed in a method/path/description table."""
    table_rows = list(_ROW.finditer(source))
    heading_rows = list(_HEADING.finditer(source))
    if not table_rows and not heading_rows:
        return None
    entries = (
        [
            (row.group(1), row.group(2), row.group(3).strip(), row.group(0).strip())
            for row in table_rows
        ]
        if table_rows
        else [
            (
                row.group(1),
                row.group(2),
                source[row.end() :].splitlines()[0].strip(" —-\t") or "接口操作",
                row.group(0).strip(),
            )
            for row in heading_rows
        ]
    )
    sections = _section_map(source)
    operations: list[SkillOperation] = []
    auth: SkillLoginAuth | None = None
    evidence: list[str] = []
    warnings = ["已从明确的接口表提取草稿；请对照原文核对参数和业务含义。"]
    seen: set[str] = set()
    excluded = 0
    for method, path, description, quote in entries[:20]:
        section = sections.get((method, path), "")
        if (
            method == "POST"
            and re.search(r"login|登录", path + description, re.IGNORECASE)
            and re.search(r"\buserName\b|\busername\b", section)
            and re.search(r"\bpassword\b", section)
            and re.search(r"\btoken\b", section)
        ):
            auth = SkillLoginAuth(
                type="login_bearer",
                path=path,
                username_field="userName" if "userName" in section else "username",
                password_field="password",
                token_field="token",
            )
            evidence.append(quote)
            warnings.append("已识别用户名密码登录契约；账号和密码只在服务端连接中配置。")
            continue
        if method == "GET" and _FILE_EXPORT.search(description + section[:200]):
            excluded += 1
            continue
        name = _operation_name(method, path)
        if name in seen:
            continue
        try:
            operation = SkillOperation(
                name=name,
                description=description,
                method=method,
                path=path,
                risk="read" if method == "GET" else "confirm",
                parameters=_params(method, path, section),
            )
        except ValueError:
            excluded += 1
            continue
        operations.append(operation)
        evidence.append(quote)
        seen.add(name)
    if not operations:
        return None
    stem = PurePath(filename or "").stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    stem = re.sub(r"^(api|docs?|documentation)-", "", stem)
    if stem and stem != system_name:
        name = f"{system_name}-{stem}"[:64].rstrip("-")
    else:
        name = f"{system_name}-api"
    title = next(
        (line.lstrip("# ").strip() for line in source.splitlines() if line.startswith("# ")),
        name,
    )[:100]
    bearer_pnkx = system_name == "pnkx" and (
        auth is not None or bool(re.search(r"Authorization\s*:\s*Bearer", source, re.IGNORECASE))
    )
    connection = "pnkx-admin" if bearer_pnkx else system_name
    if bearer_pnkx:
        if auth is not None:
            warnings.append(
                "文档声明了用户名密码登录获取 Bearer；请在 pnkx-admin 连接中审批登录路径，"
                "并配置服务端账号、密码引用。"
            )
        else:
            warnings.append(
                "文档要求 Authorization: Bearer；现有 PNKX 连接使用另一种认证头。"
                "请单独配置 pnkx-admin API 连接和 Bearer 密钥。"
            )
    elif system_name != "pnkx":
        warnings.append("请先配置并启用对应 API 连接，核对认证和路径白名单。")
    if any(operation.risk == "confirm" for operation in operations):
        warnings.append("写操作仅登记契约，当前不能执行；需接入用户确认链后再单独审核。")
    if excluded:
        warnings.append(f"已跳过 {excluded} 个导出、下载或不符合当前契约的接口。")
    if "余量可能扣成负数" in source or "未做归属/权限校验" in source:
        warnings.append("文档指出写接口存在业务校验风险；请在服务端修复后再考虑启用。")
    instructions = (
        "当用户询问此系统的数据时，优先使用匹配的只读接口查询，并解释返回结果。"
        "需要登录态的接口使用管理员配置的连接认证。"
        "写入、确认、删除或评分操作当前不可执行，不得声称已经完成。"
        "若数据为空或接口失败，应说明实际状态，不得编造卡券。"
    )
    document = SkillDocument(
        name=name,
        description=f"{title}：查询、查看和管理文档列出的接口。",
        instructions=instructions,
        api=SkillApiManifest(
            schema_version=1, connection=connection, auth=auth, operations=operations
        ),
    )
    return document, warnings, evidence
