"""抓取器抽象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from webmoniter.models import Target


@dataclass(slots=True)
class FetchResult:
    """一次抓取的结果。失败不抛异常，而是把原因放进 error，让上层统一走状态机。"""

    url: str
    text: str = ""
    status: int | None = None
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)

    def __str__(self) -> str:
        if self.ok:
            return f"HTTP {self.status} {len(self.text)} 字符"
        return f"失败: {self.error}"


class Fetcher(Protocol):
    """抓取器协议。需要新的抓取方式（API、登录态会话等）时实现它即可。"""

    name: str

    async def fetch(self, target: Target) -> FetchResult: ...

    async def aclose(self) -> None: ...
