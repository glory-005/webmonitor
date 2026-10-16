"""通知器基类：统一 webhook 发送与「HTTP 200 但业务失败」的识别。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from loguru import logger

from webmoniter.models import ChannelConfig, NotificationMessage, NotifyResult

#: webhook 请求超时（秒）
SEND_TIMEOUT = 15.0


@dataclass(slots=True)
class HttpOutcome:
    """一次 webhook 调用的原始结果。"""

    http_ok: bool
    status: int
    data: dict[str, Any]
    detail: str

    @property
    def business_code(self) -> Any:
        """飞书用 code/StatusCode，钉钉用 errcode。"""
        for key in ("code", "StatusCode", "errcode"):
            if key in self.data:
                return self.data[key]
        return None


class Notifier(ABC):
    """所有渠道的基类。"""

    kind: ClassVar[str] = "base"

    def __init__(self, config: ChannelConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.name = config.name
        self.webhook = config.webhook
        self.secret = config.secret or None
        self.keyword = config.keyword or None
        self._client = client

    def decorate_title(self, title: str) -> str:
        """机器人安全设置选了「关键词」时，把关键词拼进标题，否则消息会被静默拒收。"""
        if self.keyword and self.keyword not in title:
            return f"[{self.keyword}] {title}"
        return title

    async def _post(self, url: str, payload: dict[str, Any]) -> HttpOutcome:
        try:
            response = await self._client.post(url, json=payload, timeout=SEND_TIMEOUT)
        except Exception as exc:
            return HttpOutcome(
                http_ok=False, status=0, data={}, detail=f"{type(exc).__name__}: {exc}"
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        detail = json.dumps(data, ensure_ascii=False)[:500] if data else response.text[:500]
        if not data:
            logger.debug("{} 响应非 JSON: {}", self.name, response.text[:200])
        return HttpOutcome(
            http_ok=response.is_success, status=response.status_code, data=data, detail=detail
        )

    @abstractmethod
    async def send(self, message: NotificationMessage) -> NotifyResult:
        """发送一条通知。实现方不得抛异常，失败要放进 NotifyResult。"""


__all__ = ["HttpOutcome", "Notifier", "SEND_TIMEOUT"]
