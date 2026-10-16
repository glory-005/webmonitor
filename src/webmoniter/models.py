"""配置与运行时数据的 Pydantic 模型。

配置写错在启动时就报错，而不是跑到一半才崩——这是这里用 Pydantic 而不是裸 dict 的唯一理由。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

FetchMode = Literal["http", "browser"]
ExtractType = Literal["css", "regex", "json"]
ExtractMode = Literal["text", "html", "attr"]
EventKind = Literal["changed", "error", "recovered"]

#: 允许写进 state 表的字段白名单（动态 SQL 拼接前的护栏）
STATE_FIELDS = frozenset(
    {
        "last_hash",
        "last_ok_at",
        "consecutive_failures",
        "last_notified_at",
        "silenced_until",
        "error_notified",
    }
)


class ExtractRule(BaseModel):
    """一条抽取规则。"""

    type: ExtractType
    selector: str | None = None
    mode: ExtractMode = "text"
    attr: str | None = None
    pattern: str | None = None
    group: int = 0
    path: str | None = None
    flags: str = ""  # 仅 regex：可含 i / m / s / x

    @model_validator(mode="after")
    def _require_fields(self) -> ExtractRule:
        if self.type == "css" and not self.selector:
            raise ValueError("css 规则必须提供 selector")
        if self.type == "regex" and not self.pattern:
            raise ValueError("regex 规则必须提供 pattern")
        if self.type == "json" and not self.path:
            raise ValueError("json 规则必须提供 path")
        if self.mode == "attr" and not self.attr:
            raise ValueError("mode=attr 时必须提供 attr，例如 attr: href")
        return self

    def describe(self) -> str:
        if self.type == "css":
            return f"css:{self.selector}[{self.mode}]"
        if self.type == "regex":
            return f"regex:{self.pattern}#{self.group}"
        return f"json:{self.path}"


class DiffOptions(BaseModel):
    """比对与去噪参数。"""

    strip_selectors: list[str] = Field(
        default_factory=lambda: ["script", "style", "noscript", "#comment"]
    )
    ignore_regex: list[str] = Field(default_factory=list)
    min_changed_chars: int = 1
    similarity_threshold: float = 1.0

    @model_validator(mode="after")
    def _validate_regex(self) -> DiffOptions:
        for pattern in self.ignore_regex:
            try:
                re.compile(pattern)
            except re.error as exc:  # 配置期就拦住，别等到运行时
                raise ValueError(f"ignore_regex 不是合法正则: {pattern!r}（{exc}）") from exc
        return self


class NotifyOptions(BaseModel):
    """单个目标的告警策略。"""

    channels: list[str] = Field(default_factory=list)
    on: list[EventKind] = Field(default_factory=lambda: ["changed"])
    error_after_consecutive: int = 3
    mention_all_keywords: list[str] = Field(default_factory=list)


class Target(BaseModel):
    """一个被监控的页面。"""

    id: str
    url: str
    name: str = ""
    enabled: bool = True
    interval: int = 300
    jitter: int = 30
    timeout: float = 20.0
    fetch: FetchMode = "http"
    wait_for: str | None = None  # 仅 browser：等待该选择器出现
    parser: str = "html.parser"
    headers: dict[str, str] = Field(default_factory=dict)
    cookies_env: str | None = None  # 从该环境变量读取整条 Cookie
    extract: list[ExtractRule] = Field(default_factory=list)
    diff: DiffOptions = Field(default_factory=DiffOptions)
    notify: NotifyOptions = Field(default_factory=NotifyOptions)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sane_values(self) -> Target:
        if self.interval < 5:
            raise ValueError(f"target {self.id}: interval 太小（{self.interval}s），最低 5s")
        if self.jitter < 0:
            raise ValueError(f"target {self.id}: jitter 不能为负")
        return self

    @property
    def effective_jitter(self) -> int:
        """实际使用的抖动上限：不超过间隔的一半。

        这样 defaults 里写 jitter: 30 时，某个目标把 interval 调成 30 也不会报错或让周期翻倍。
        """
        return min(self.jitter, max(0, self.interval // 2))

    @property
    def display_name(self) -> str:
        return self.name or self.id

    def describe(self) -> str:
        rules = "、".join(rule.describe() for rule in self.extract) or "整页文本"
        return f"{self.display_name} [{self.interval}s] {self.url} ← {rules}"


class ChannelConfig(BaseModel):
    """通知渠道（来自环境变量扫描）。"""

    name: str  # 例：feishu_ops
    kind: Literal["feishu", "dingtalk"]
    webhook: str
    secret: str | None = None
    keyword: str | None = None


class NotificationMessage(BaseModel):
    """渠道无关的通知内容。"""

    target_id: str
    kind: EventKind
    title: str
    lines: list[str] = Field(default_factory=list)
    url: str = ""
    level: Literal["info", "warning", "error"] = "info"
    mention_all: bool = False

    def to_markdown(self) -> str:
        return "\n".join(self.lines)


class NotifyResult(BaseModel):
    channel: str
    ok: bool
    detail: str = ""


class Snapshot(BaseModel):
    target_id: str
    fetched_at: str
    http_status: int | None = None
    content_hash: str | None = None
    text: str | None = None
    error: str | None = None
    elapsed_ms: int = 0


__all__: list[str] = [
    "STATE_FIELDS",
    "ChannelConfig",
    "DiffOptions",
    "EventKind",
    "ExtractMode",
    "ExtractRule",
    "ExtractType",
    "FetchMode",
    "NotificationMessage",
    "NotifyOptions",
    "NotifyResult",
    "Snapshot",
    "Target",
]
