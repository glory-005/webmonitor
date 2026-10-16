"""比对测试：区间阈值、噪声判定、行级差异。"""

from __future__ import annotations

from webmoniter.diff import diff_text, hash_text, unified_diff
from webmoniter.models import DiffOptions

BASE = "\n".join(f"第{i}行内容" for i in range(1, 51))


def test_identical_text_is_hash_equal() -> None:
    result = diff_text(BASE, BASE, DiffOptions())
    assert not result.changed
    assert result.reason == "hash-equal"
    assert hash_text(BASE) == hash_text(BASE)


def test_below_threshold_is_noise() -> None:
    options = DiffOptions(min_changed_chars=100)
    result = diff_text(BASE, BASE + "\n新增一行", options)
    assert not result.changed
    assert result.reason == "below-threshold"
    assert result.added_lines == ["新增一行"]


def test_too_similar_is_noise() -> None:
    modified = BASE.replace("第25行内容", "第25行内容（改）")
    options = DiffOptions(min_changed_chars=1, similarity_threshold=0.9)
    result = diff_text(BASE, modified, options)
    assert not result.changed
    assert result.reason == "too-similar"
    assert result.similarity > 0.9


def test_real_change_is_detected() -> None:
    modified = BASE.replace("第1行内容", "第1行内容（已更新）") + "\n新增公告：某项目招标"
    options = DiffOptions(min_changed_chars=1, similarity_threshold=0.999)
    result = diff_text(BASE, modified, options)
    assert result.changed
    assert result.reason == "changed"
    assert any("招标" in line for line in result.added_lines)
    assert any("第1行内容" == line for line in result.removed_lines)
    assert result.changed_chars > 0


def test_unified_diff_marks_additions() -> None:
    text = unified_diff("a\nb", "a\nc")
    assert "-b" in text
    assert "+c" in text
