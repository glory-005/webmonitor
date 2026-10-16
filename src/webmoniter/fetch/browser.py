"""可选的 Playwright 抓取器，用于 JS 动态渲染页面。

默认不安装（依赖重：Chromium 约 300MB）。启用方式：
    uv sync --extra browser
    uv run playwright install chromium
然后在 targets.yaml 里写 `fetch: browser`。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from webmoniter.fetch.base import FetchResult
from webmoniter.models import Target
from webmoniter.settings import Settings

INSTALL_HINT = "未安装 Playwright。请执行： uv sync --extra browser && uv run playwright install chromium"


class BrowserFetcher:
    """常驻一个 Chromium 实例，每个 target 开独立 context 后关闭。"""

    name = "browser"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Any:
        async with self._lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - 取决于是否装了 extra
                raise RuntimeError(INSTALL_HINT) from exc
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            logger.info("Chromium 已启动（browser 抓取器）")
            return self._browser

    async def fetch(self, target: Target) -> FetchResult:
        started = time.perf_counter()
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                user_agent=self._settings.default_user_agent,
                locale="zh-CN",
                extra_http_headers=dict(target.headers) or None,
            )
            try:
                page = await context.new_page()
                response = await page.goto(
                    target.url, wait_until="domcontentloaded", timeout=int(target.timeout * 1000)
                )
                if target.wait_for:
                    await page.wait_for_selector(target.wait_for, timeout=int(target.timeout * 1000))
                else:
                    await page.wait_for_load_state("networkidle", timeout=int(target.timeout * 1000))
                html = await page.content()
                elapsed = int((time.perf_counter() - started) * 1000)
                status = response.status if response else None
                if status is not None and status >= 400:
                    return FetchResult(
                        url=target.url, status=status, elapsed_ms=elapsed, error=f"HTTP {status}"
                    )
                return FetchResult(url=target.url, text=html, status=status, elapsed_ms=elapsed)
            finally:
                await context.close()
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning("target {} 浏览器抓取异常: {}: {}", target.id, type(exc).__name__, exc)
            return FetchResult(
                url=target.url, elapsed_ms=elapsed, error=f"{type(exc).__name__}: {exc}"
            )

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
