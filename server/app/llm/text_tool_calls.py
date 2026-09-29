"""文本形态工具调用的还原与剥离。

部分 OpenAI 兼容网关只把工具目录注入 prompt，模型以
``<tool_name>{"args": ...}</tool_name>`` 文本形式回调用，网关却不把它
解析回 ``tool_calls`` 响应字段——调用既不会执行，内部协议标签还会
作为正文直接漏给用户。这里统一兜底：标签名必须与本轮实际挂载的
工具名完全一致才认定，避免误吞普通尖括号文本。

本模块必须保持零 app 内依赖（provider 与 chat.reply 双向引用它），
否则会形成 app.llm ↔ app.chat 的导入环。
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from typing import Any

# 工具名遵循 TokenName 语法（小写字母、数字、下划线、连字符、点）；
# 字符集不含 ">"，因此标签名匹配不会越过标签本身。
TEXT_TOOL_CALL_PATTERN = re.compile(
    r"<(?P<name>[a-z][a-z0-9_.-]*)>(?P<body>.*?)</(?P=name)>",
    re.DOTALL,
)

# CompletionResult.tool_calls 与 LLMMessage.tool_calls 的字段上限。
MAX_TEXT_TOOL_CALLS = 8


def extract_text_tool_calls(
    raw: str, tool_names: Collection[str]
) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """剥离正文里的文本形态工具调用。

    返回 ``(剥离后的正文, [(工具名, 参数), ...])``。标签名命中已挂载
    工具的块一律从正文移除——即使参数不是合法 JSON，内部协议也不该
    出现在用户可见文本里；只有参数能解析为 JSON 对象的块才会被还原
    成可执行的调用。
    """
    if not tool_names or not raw:
        return raw, []
    wanted = frozenset(tool_names)
    calls: list[tuple[str, dict[str, Any]]] = []

    def _take(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in wanted:
            return match.group(0)
        try:
            arguments = json.loads(match.group("body").strip())
        except json.JSONDecodeError:
            return ""
        if not isinstance(arguments, dict):
            return ""
        calls.append((name, arguments))
        return ""

    return TEXT_TOOL_CALL_PATTERN.sub(_take, raw), calls
