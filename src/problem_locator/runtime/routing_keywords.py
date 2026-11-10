"""Validate explicit literal keywords used only by deterministic routing."""
from __future__ import annotations

import unicodedata


_GENERIC_KEYWORDS = frozenset({
    "超时", "失败", "错误", "故障", "异常", "timeout", "error", "failure", "slow", "rpc",
})


def parse_direct_match_keywords(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("direct_match_keywords 必须是最多包含 16 项的数组。")
    keywords: list[str] = []
    for keyword in value:
        if (
            not isinstance(keyword, str)
            or not 3 <= len(keyword) <= 128
            or keyword != keyword.strip()
            or any(unicodedata.category(char) == "Cc" for char in keyword)
        ):
            raise ValueError("直接路由关键词须为 3～128 个字符，不能包含首尾空白或控制字符。")
        if keyword in keywords:
            raise ValueError("直接路由关键词不能重复。")
        if keyword.casefold() in _GENERIC_KEYWORDS:
            raise ValueError("直接路由关键词不能只使用超时、错误等泛词。")
        keywords.append(keyword)
    return tuple(keywords)
