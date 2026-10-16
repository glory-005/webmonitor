"""钉钉自定义机器人。

加签算法（与飞书故意不同）：
    string_to_sign = f"{timestamp}\\n{secret}"
    sign = urlencode(base64(hmac_sha256(key=secret, msg=string_to_sign)))
    timestamp 单位：毫秒
timestamp 与 sign 作为 URL 查询参数，不是请求体。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any, ClassVar

from webmoniter.models import NotificationMessage, NotifyResult
from webmoniter.notify.base import Notifier


def sign(timestamp_ms: int, secret: str) -> str:
    """计算钉钉加签值（已 urlencode）。timestamp 为毫秒级时间戳。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))


def build_markdown(message: NotificationMessage) -> str:
    """拼 markdown 正文。"""
    parts: list[str] = []
    if message.mention_all:
        parts.append("@所有人")
    parts.extend(message.lines)
    if message.url:
        parts.append(f"[查看原页面]({message.url})")
    return "\n\n".join(parts)


class DingTalkNotifier(Notifier):
    kind: ClassVar[str] = "dingtalk"

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        timestamp = int(time.time() * 1000)
        separator = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{separator}timestamp={timestamp}&sign={sign(timestamp, self.secret)}"

    async def send(self, message: NotificationMessage) -> NotifyResult:
        title = self.decorate_title(message.title)
        payload: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": build_markdown(message)},
            "at": {"isAtAll": message.mention_all},
        }
        outcome = await self._post(self._signed_url(), payload)
        ok = outcome.http_ok and outcome.business_code == 0
        if not ok:
            return NotifyResult(
                channel=self.name, ok=False, detail=f"HTTP {outcome.status} {outcome.detail}"
            )
        return NotifyResult(channel=self.name, ok=True, detail=outcome.detail)


__all__ = ["DingTalkNotifier", "build_markdown", "sign"]
