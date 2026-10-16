"""归一化：把「渲染层面的抖动」抹平，只留「内容层面的差异」。

这一步决定了误报率，比通知部分重要得多。经验上：
* 空白/换行/零宽字符/全角半角差异必须抹掉；
* 时间戳、随机 token、访问计数这类天然变化交给 ignore_regex；
* 连续重复行（列表页常见的重复块）去掉，能显著降低噪声。
"""

from __future__ import annotations

import re
import unicodedata

from loguru import logger

from webmoniter.models import DiffOptions

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_INLINE_SPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")


def normalize_text(text: str, options: DiffOptions) -> str:
    """归一化文本。返回按行拼接的干净文本，可直接做 hash 与 diff。"""
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_ZERO_WIDTH)
    text = unicodedata.normalize("NFKC", text)

    for pattern in options.ignore_regex:
        try:
            text = re.sub(pattern, "", text, flags=re.MULTILINE)
        except re.error as exc:  # 配置期已校验，这里只是兜底
            logger.warning("ignore_regex 执行失败，已跳过: {!r}（{}）", pattern, exc)

    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = _INLINE_SPACE_RE.sub(" ", raw_line).strip()
        if not line:
            continue
        if lines and lines[-1] == line:  # 连续重复行折叠
            continue
        lines.append(line)
    return "\n".join(lines)


__all__ = ["normalize_text"]
