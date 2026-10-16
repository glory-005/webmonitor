"""变更判定：hash 快比对 + 行级 diff + 相似度阈值。"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field

from webmoniter.models import DiffOptions

#: 相似度计算用的最大长度，避免超长页面把 CPU 拖住
SIMILARITY_LIMIT = 200_000
#: 每条消息里最多保留的 diff 行数
MAX_DIFF_LINES = 200


def hash_text(text: str) -> str:
    """内容指纹。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class DiffResult:
    """比对结果。changed=False 时 reason 说明为什么判定为噪声。"""

    changed: bool
    reason: str = "changed"  # changed / hash-equal / below-threshold / too-similar
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    similarity: float = 1.0
    changed_chars: int = 0

    @property
    def added(self) -> int:
        return len(self.added_lines)

    @property
    def removed(self) -> int:
        return len(self.removed_lines)

    def summary(self) -> str:
        if not self.changed:
            return f"未变更（{self.reason}）"
        return f"新增 {self.added} 行 / 删除 {self.removed} 行，相似度 {self.similarity:.4f}"


def diff_text(old: str, new: str, options: DiffOptions) -> DiffResult:
    """比较两次归一化文本。

    判定顺序：完全一致 → 变化量太小（噪声）→ 相似度过高（排版抖动）→ 真实变更。
    """
    if old == new:
        return DiffResult(changed=False, reason="hash-equal")

    old_lines = old.split("\n") if old else []
    new_lines = new.split("\n") if new else []

    added: list[str] = []
    removed: list[str] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old_lines[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new_lines[j1:j2])

    changed_chars = sum(len(line) for line in added) + sum(len(line) for line in removed)
    similarity = _similarity(old, new)

    result = DiffResult(
        changed=True,
        added_lines=added[:MAX_DIFF_LINES],
        removed_lines=removed[:MAX_DIFF_LINES],
        similarity=similarity,
        changed_chars=changed_chars,
    )

    if changed_chars < options.min_changed_chars:
        result.changed = False
        result.reason = "below-threshold"
    elif similarity >= options.similarity_threshold:
        result.changed = False
        result.reason = "too-similar"
    return result


def _similarity(old: str, new: str) -> float:
    """整体相似度。先 quick_ratio 预筛，明显不同就不用算精确值。"""
    old_sample = old[:SIMILARITY_LIMIT]
    new_sample = new[:SIMILARITY_LIMIT]
    matcher = difflib.SequenceMatcher(a=old_sample, b=new_sample, autojunk=False)
    if matcher.quick_ratio() < 0.9:
        return matcher.quick_ratio()
    return matcher.ratio()


def unified_diff(old: str, new: str, *, context: int = 1, limit: int = MAX_DIFF_LINES) -> str:
    """给人看的 unified diff（写日志 / CLI 输出用）。"""
    lines = list(
        difflib.unified_diff(
            old.split("\n"),
            new.split("\n"),
            fromfile="变更前",
            tofile="变更后",
            lineterm="",
            n=context,
        )
    )
    if len(lines) > limit:
        lines = lines[:limit] + [f"…（diff 共 {len(lines)} 行，已截断）"]
    return "\n".join(lines)


__all__ = ["DiffResult", "diff_text", "hash_text", "unified_diff"]
