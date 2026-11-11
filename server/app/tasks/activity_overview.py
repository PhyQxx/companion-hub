"""Conservative narrative overview from the saved report, without new source reads."""

import re
from collections import Counter

_THEMES = {
    "开发与技术排查": ("代码", "开发", "调试", "测试", "部署", "接口", "修复", "github"),
    "设计与规划": ("设计", "方案", "规划", "需求", "架构", "原型"),
    "学习与资料查阅": ("学习", "阅读", "教程", "文档", "研究", "资料"),
    "沟通与协作": ("会议", "讨论", "沟通", "评审", "邮件", "协作"),
    "生活事务": ("购物", "预约", "健康", "运动", "出行", "家务"),
}


def compose_overview(text: str, kind: str) -> str:
    """Describe supported themes; counts are records, never time or productivity."""
    period = "这一天" if kind == "daily" else "这一周"
    section = ""
    activity: set[str] = set()
    completed = 0
    pending = 0
    calendar = 0
    for line in text.splitlines():
        if line.startswith("【"):
            section = line
        if not line.startswith("- "):
            continue
        content = re.sub(r"\s*\[(?:timeline|task|calendar):[^\]]+\]", "", line[2:])
        if section in {"【聊天】", "【浏览】", "【屏幕】"}:
            activity.add(content.lower().strip())
        elif section.startswith("【任务"):
            completed += int(content.endswith(" · done"))
            pending += int(content.endswith((" · active", " · firing")))
        elif section.startswith("【日程"):
            calendar += 1
    themes: Counter[str] = Counter()
    for entry in activity:
        for theme, words in _THEMES.items():
            if any(word in entry for word in words):
                themes[theme] += 1
    parts = []
    if themes:
        focus = "、".join(name for name, _ in themes.most_common(3))
        parts.append(f"从已保存的活动内容看，{period}涉及的主要方向是{focus}。")
        parts.append("这些方向来自聊天、浏览和屏幕记录的主题归纳，不代表实际投入时长。")
    elif activity:
        parts.append(f"{period}有多项聊天或电脑活动记录，但现有信息不足以可靠归纳主要主题。")
    if completed or pending:
        parts.append(
            f"任务推进方面，所收录任务中有 {completed} 项在生成时已完成，{pending} 项仍待推进。"
        )
        if pending:
            parts.append("后续可优先核对仍待推进的事项，结合实际截止时间确定顺序。")
    if calendar:
        parts.append(f"此外有 {calendar} 项日程安排与本期重叠；是否实际参加需另行确认。")
    if not parts:
        parts.append(f"{period}的有效记录较少，暂不足以形成活动和进展判断。")
    if "仅展示前 100 条" in text:
        parts.append("部分记录超出展示上限，以上概览仅覆盖已保存的样本。")
    return "".join(parts)
