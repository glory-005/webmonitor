"""跨模块小工具：时间、深合并、文本截断。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    """带时区的当前时间（UTC）。全项目统一用 UTC 存库，展示时再本地化。"""
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    """datetime → ISO8601 字符串（UTC）。SQLite 里按字符串存，可直接字典序比较。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    """ISO8601 字符串 → 带时区 datetime，失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典：override 覆盖 base，嵌套字典逐层合并。

    用于把 targets.yaml 里的 defaults 合并进每个 target，
    这样 target 只写 `diff: {min_changed_chars: 20}` 也能继承 defaults 里其余的 diff 选项。
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def truncate(text: str, limit: int, suffix: str = "\n…（内容已截断）") -> str:
    """按字符数截断，避免把整页塞进通知或日志。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + suffix


def format_elapsed(ms: int | float) -> str:
    """毫秒 → 人类可读。"""
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f}s"
    return f"{ms / 60_000:.1f}min"
