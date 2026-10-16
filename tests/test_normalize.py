"""归一化测试：把渲染抖动抹平，但不吃掉真实内容。"""

from __future__ import annotations

from webmoniter.models import DiffOptions
from webmoniter.normalize import normalize_text


def test_collapses_whitespace_and_blank_lines() -> None:
    text = "  公告一  \n\n\n\t公告二\t \n"
    assert normalize_text(text, DiffOptions()) == "公告一\n公告二"


def test_removes_zero_width_and_normalizes_width() -> None:
    text = "公告\u200b一\nＡＢＣ１２３"
    assert normalize_text(text, DiffOptions()) == "公告一\nABC123"


def test_drops_consecutive_duplicate_lines() -> None:
    text = "重复\n重复\n另一个"
    assert normalize_text(text, DiffOptions()) == "重复\n另一个"


def test_ignore_regex_removes_timestamps() -> None:
    options = DiffOptions(ignore_regex=[r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"])
    text = "更新于 2025-01-02 03:04:05\n正文"
    assert normalize_text(text, options) == "更新于\n正文"


def test_ignore_regex_removes_csrf_token() -> None:
    options = DiffOptions(ignore_regex=[r'csrf_token="[^"]+"'])
    text = 'csrf_token="a1b2c3"\n正文'
    assert normalize_text(text, options) == "正文"
