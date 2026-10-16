"""从环境变量 / .env 发现通知渠道并构造通知器。

约定（避免再多一个配置文件）：
    <KIND>_WEBHOOK_<NAME>   —— 必需，渠道名 = <kind>_<name 小写>
    <KIND>_SECRET_<NAME>    —— 可选，加签密钥
    <KIND>_KEYWORD_<NAME>   —— 可选，机器人安全设置为「关键词」时填

例：
    FEISHU_WEBHOOK_OPS=...   → 渠道 feishu_ops
    DINGTALK_WEBHOOK_OPS=... → 渠道 dingtalk_ops
targets.yaml 里的 channels 直接引用这些名字。

环境来源由 webmoniter.settings 统一负责：它在导入时就把 .env 注入 os.environ，
并由 load_environ() 提供「.env + os.environ」的合并视图。
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from loguru import logger

from webmoniter.models import ChannelConfig
from webmoniter.notify.base import Notifier
from webmoniter.notify.dingtalk import DingTalkNotifier
from webmoniter.notify.feishu import FeishuNotifier
from webmoniter.settings import env_file_paths, load_environ

WEBHOOK_PREFIX = "WEBHOOK_"
SECRET_PREFIX = "SECRET_"
KEYWORD_PREFIX = "KEYWORD_"

#: kind → 实现类
NOTIFIER_CLASSES: dict[str, type[Notifier]] = {
    "feishu": FeishuNotifier,
    "dingtalk": DingTalkNotifier,
}


def discover_channels(environ: Mapping[str, str] | None = None) -> dict[str, ChannelConfig]:
    """扫描环境（含 .env），返回 渠道名 → ChannelConfig。"""
    environ = load_environ(environ)
    channels: dict[str, ChannelConfig] = {}

    for kind in NOTIFIER_CLASSES:
        prefix = f"{kind.upper()}_{WEBHOOK_PREFIX}"
        for key, webhook in environ.items():
            if not key.startswith(prefix) or not webhook.strip():
                continue
            suffix = key[len(prefix) :].strip()
            if not suffix:
                continue
            name = f"{kind}_{suffix.lower()}"
            channels[name] = ChannelConfig(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                webhook=webhook.strip(),
                secret=(environ.get(f"{kind.upper()}_{SECRET_PREFIX}{suffix}") or "").strip() or None,
                keyword=(environ.get(f"{kind.upper()}_{KEYWORD_PREFIX}{suffix}") or "").strip() or None,
            )
    return channels


def build_notifiers(
    client: httpx.AsyncClient, environ: Mapping[str, str] | None = None
) -> dict[str, Notifier]:
    """构造所有已配置的通知器。"""
    notifiers: dict[str, Notifier] = {}
    for name, config in discover_channels(environ).items():
        notifier_class = NOTIFIER_CLASSES[config.kind]
        notifiers[name] = notifier_class(config, client)
        logger.debug("已注册通知渠道 {}（{}，加签={}）", name, config.kind, bool(config.secret))
    if notifiers:
        logger.info("通知渠道: {}", "、".join(sorted(notifiers)))
    else:
        logger.warning(
            "没有发现任何通知渠道。请参考 .env.example 配置 "
            "FEISHU_WEBHOOK_<NAME> / DINGTALK_WEBHOOK_<NAME>"
        )
    return notifiers


__all__ = [
    "NOTIFIER_CLASSES",
    "build_notifiers",
    "discover_channels",
    "env_file_paths",
    "load_environ",
]
