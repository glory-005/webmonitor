"""共享 fixture：临时 SQLite、Settings、假抓取器 / 假通知器。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from webmoniter.fetch.base import FetchResult
from webmoniter.models import NotificationMessage, NotifyResult, Target
from webmoniter.settings import Settings
from webmoniter.storage import Storage


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    store = Storage(tmp_path / "test.db")
    await store.init()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        artifacts_dir=tmp_path / "artifacts",
        log_dir=tmp_path / "logs",
        health_silence_hours=6.0,
        # 测试里不等退避 sleep，发送失败立即返回
        notify_retry_attempts=1,
    )


@pytest.fixture
def make_target():
    """构造 Target，只写关心的字段。"""

    def _make(**overrides: Any) -> Target:
        data: dict[str, Any] = {
            "id": "t1",
            "url": "https://example.com/page",
            "interval": 60,
            "jitter": 0,
        }
        data.update(overrides)
        return Target(**data)

    return _make


class FakeFetcher:
    """总是返回「当前」结果；用 push() 模拟页面变化。"""

    name = "fake"

    def __init__(self, result: FetchResult | str | None = None) -> None:
        self.current: FetchResult | None = None
        if result is not None:
            self.push(result)
        self.calls = 0
        self.closed = False

    @staticmethod
    def _coerce(result: FetchResult | str) -> FetchResult:
        if isinstance(result, str):
            return FetchResult(url="https://example.com/page", text=result, status=200)
        return result

    def push(self, result: FetchResult | str) -> None:
        self.current = self._coerce(result)

    async def fetch(self, target: Target) -> FetchResult:
        self.calls += 1
        if self.current is None:
            raise AssertionError("FakeFetcher 还没有结果，请先 push()")
        return self.current

    async def aclose(self) -> None:
        self.closed = True


class FakeNotifier:
    """记录发送内容，永不真的联网。"""

    kind = "feishu"

    def __init__(self, name: str = "feishu_ops", ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> NotifyResult:
        self.messages.append(message)
        return NotifyResult(channel=self.name, ok=self.ok, detail="fake")

    async def aclose(self) -> None:
        return None
