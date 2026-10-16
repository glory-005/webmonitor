"""把状态机的判定结果渲染成各渠道通用的通知内容。

渠道差异（卡片 / markdown / @人）由 notify 层处理，这里只管「说什么」。
"""

from __future__ import annotations

from webmoniter.diff import DiffResult
from webmoniter.models import NotificationMessage, Target
from webmoniter.utils import format_elapsed, truncate

#: 单行最多显示多少字符
LINE_LIMIT = 200


def _mention_all(target: Target, text: str) -> bool:
    """新增内容命中关键词时 @所有人。"""
    return any(keyword and keyword in text for keyword in target.notify.mention_all_keywords)


def change_message(
    target: Target,
    diff: DiffResult,
    new_text: str,
    *,
    elapsed_ms: int = 0,
    max_lines: int = 15,
) -> NotificationMessage:
    lines = [
        f"**变化量**：新增 {diff.added} 行 / 删除 {diff.removed} 行，相似度 {diff.similarity:.4f}",
    ]
    if elapsed_ms:
        lines.append(f"**耗时**：{format_elapsed(elapsed_ms)}")

    if diff.added_lines:
        lines.append("**新增内容：**")
        for line in diff.added_lines[:max_lines]:
            lines.append(f"+ {truncate(line, LINE_LIMIT)}")
        if diff.added > max_lines:
            lines.append(f"…（另有 {diff.added - max_lines} 行新增，详见本地日志与数据库）")

    if diff.removed_lines:
        lines.append("**移除内容：**")
        for line in diff.removed_lines[:max_lines]:
            lines.append(f"- {truncate(line, LINE_LIMIT)}")
        if diff.removed > max_lines:
            lines.append(f"…（另有 {diff.removed - max_lines} 行移除）")

    return NotificationMessage(
        target_id=target.id,
        kind="changed",
        title=f"{target.display_name} 内容有变化",
        lines=lines,
        url=target.url,
        level="warning",
        mention_all=_mention_all(target, new_text),
    )


def error_message(
    target: Target,
    error: str,
    failures: int,
    *,
    last_ok_at: str | None = None,
) -> NotificationMessage:
    lines = [
        f"**连续失败**：{failures} 次",
        f"**最近错误**：{truncate(error, 300)}",
    ]
    if last_ok_at:
        lines.append(f"**最近一次成功**：{last_ok_at}")
    lines.append("若为选择器失效或站点改版，请用 `webmoniter check "
                 f"{target.id} --show-diff` 排查。")
    return NotificationMessage(
        target_id=target.id,
        kind="error",
        title=f"{target.display_name} 抓取失败",
        lines=lines,
        url=target.url,
        level="error",
        mention_all=False,
    )


def recovered_message(
    target: Target,
    failures: int,
    *,
    downtime_seconds: float | None = None,
) -> NotificationMessage:
    lines = [f"**已恢复正常**：此前连续失败 {failures} 次"]
    if downtime_seconds:
        lines.append(f"**中断时长**：约 {format_elapsed(downtime_seconds * 1000)}")
    return NotificationMessage(
        target_id=target.id,
        kind="recovered",
        title=f"{target.display_name} 已恢复",
        lines=lines,
        url=target.url,
        level="info",
        mention_all=False,
    )


def health_message(
    *, silence_hours: float, age_seconds: float | None, target_count: int
) -> NotificationMessage:
    if age_seconds is None:
        detail = "启动以来没有任何一次成功的检查"
    else:
        detail = f"最近一次成功检查已是 {format_elapsed(age_seconds * 1000)} 之前"
    return NotificationMessage(
        target_id="__health__",
        kind="error",
        title="WebMoniter 自检异常",
        lines=[
            f"**告警**：超过 {silence_hours:g} 小时没有成功检查",
            f"**详情**：{detail}",
            f"**监控目标数**：{target_count}",
            "请检查进程是否存活、网络是否可用、目标站点是否整体不可达。",
        ],
        level="error",
        mention_all=True,
    )


__all__ = ["change_message", "error_message", "health_message", "recovered_message"]
