"""飞书自定义机器人。

加签算法（与钉钉故意不同，最容易写错的地方）：
    string_to_sign = f"{timestamp}\\n{secret}"
    sign = base64(hmac_sha256(key=string_to_sign, msg=b""))
    timestamp 单位：秒
请求体里带 {"timestamp": "<秒>", "sign": "<sign>", ...}。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, ClassVar

from webmoniter.models import NotificationMessage, NotifyResult
from webmoniter.notify.base import Notifier

#: 按严重程度选卡片颜色
LEVEL_TEMPLATE = {"info": "blue", "warning": "orange", "error": "red"}


def sign(timestamp: int | str, secret: str) -> str:
    """计算飞书加签值。timestamp 为秒级时间戳。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_card(message: NotificationMessage) -> dict[str, Any]:
    """拼交互式卡片。"""
    body_lines = list(message.lines)
    if message.mention_all:
        body_lines.insert(0, '<at user_id="all">所有人</at>')
    if message.url:
        body_lines.append(f"[查看原页面]({message.url})")

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(body_lines)}}
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": LEVEL_TEMPLATE.get(message.level, "blue"),
            "title": {"tag": "plain_text", "content": message.title},
        },
        "elements": elements,
    }


class FeishuNotifier(Notifier):
    kind: ClassVar[str] = "feishu"

    async def send(self, message: NotificationMessage) -> NotifyResult:
        card = build_card(message.model_copy(update={"title": self.decorate_title(message.title)}))
        payload: dict[str, Any] = {"msg_type": "interactive", "card": card}
        if self.secret:
            timestamp = int(time.time())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = sign(timestamp, self.secret)

        outcome = await self._post(self.webhook, payload)
        code = outcome.business_code
        ok = outcome.http_ok and code in (0, None)
        if not ok:
            return NotifyResult(
                channel=self.name, ok=False, detail=f"HTTP {outcome.status} {outcome.detail}"
            )
        return NotifyResult(channel=self.name, ok=True, detail=outcome.detail)


__all__ = ["FeishuNotifier", "build_card", "sign"]
