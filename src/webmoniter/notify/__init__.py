"""通知渠道。新增渠道只需实现 Notifier 并在 registry 里注册。"""

from webmoniter.notify.base import HttpOutcome, Notifier
from webmoniter.notify.dingtalk import DingTalkNotifier
from webmoniter.notify.feishu import FeishuNotifier
from webmoniter.notify.registry import build_notifiers, discover_channels

__all__ = [
    "DingTalkNotifier",
    "FeishuNotifier",
    "HttpOutcome",
    "Notifier",
    "build_notifiers",
    "discover_channels",
]
