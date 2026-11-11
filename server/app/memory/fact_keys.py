"""Small, deterministic entity slots shared by extraction and exact retrieval."""

import re


def birthday_key(text: str) -> str | None:
    match = re.match(
        r"^(?:用户的?|我的?|请问)?(?:女儿|儿子|妻子|丈夫)?([\w\u4e00-\u9fff]{1,12}?)(?:的)?(?:生日|出生日期|出生于)",
        text.strip(),
    )
    if match:
        name = match.group(1)
        if name in {"用户", "我", "你", "助手"}:
            return "profile.birthday"
        return f"person.{name.casefold()}.birthday"
    if re.match(r"^(?:用户|我|你|助手)(?:的)?(?:生日|出生日期|出生于)", text.strip()):
        return "profile.birthday"
    return None
