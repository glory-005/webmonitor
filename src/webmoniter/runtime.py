"""组件装配与生命周期管理。

CLI 各命令都通过 Runtime 拿到已就绪的 storage / fetchers / notifiers / watchers，
避免装配逻辑散落在每条命令里。
"""

from __future__ import annotations

from types import TracebackType

import httpx
from loguru import logger

from webmoniter.config import config_hash
from webmoniter.fetch.base import Fetcher
from webmoniter.fetch.http import HttpFetcher
from webmoniter.health import HealthMonitor
from webmoniter.models import Target
from webmoniter.notify.base import Notifier
from webmoniter.notify.registry import build_notifiers
from webmoniter.settings import Settings
from webmoniter.storage import Storage
from webmoniter.throttle import Throttle
from webmoniter.watcher import Watcher


class Runtime:
    """持有全部运行时组件；用 async with 或显式 aclose() 释放资源。"""

    def __init__(
        self,
        *,
        settings: Settings,
        targets: list[Target],
        storage: Storage,
        http_client: httpx.AsyncClient,
        fetchers: dict[str, Fetcher],
        notifiers: dict[str, Notifier],
        throttle: Throttle,
        watchers: dict[str, Watcher],
        health: HealthMonitor,
    ) -> None:
        self.settings = settings
        self.targets = targets
        self.storage = storage
        self.http_client = http_client
        self.fetchers = fetchers
        self.notifiers = notifiers
        self.throttle = throttle
        self.watchers = watchers
        self.health = health

    @classmethod
    async def create(cls, settings: Settings, targets: list[Target]) -> Runtime:
        storage = Storage(settings.resolve(settings.db_path))
        await storage.init()

        # 目标登记；配置指纹变化（换了选择器 / 过滤规则）时清掉基线，
        # 否则新规则算出的文本会和旧规则的 hash 相比，产生一次伪告警。
        for target in targets:
            fingerprint = config_hash(target)
            previous = await storage.get_target(target.id)
            if previous and previous.get("config_hash") and previous["config_hash"] != fingerprint:
                logger.warning(
                    "target {} 的抓取/抽取配置已变化，下一轮将重建基线（本次不告警）", target.id
                )
                # 同时重置故障计数与错误告警标记：配置刚改过，应当重新武装错误告警
                await storage.save_state(target.id, last_hash=None, error_notified=0)
            await storage.upsert_target(
                target.id,
                name=target.name,
                url=target.url,
                config_hash=fingerprint,
                enabled=target.enabled,
            )

        http_client = httpx.AsyncClient(follow_redirects=True, timeout=15.0)
        notifiers = build_notifiers(http_client)
        throttle = Throttle(settings.rate_per_minute)

        fetchers: dict[str, Fetcher] = {"http": HttpFetcher(settings)}
        if any(target.fetch == "browser" for target in targets if target.enabled):
            from webmoniter.fetch.browser import BrowserFetcher  # 延迟导入，未装 Playwright 也能跑

            fetchers["browser"] = BrowserFetcher(settings)

        watchers: dict[str, Watcher] = {}
        for target in targets:
            if not target.enabled:
                logger.info("target {} 已禁用，跳过", target.id)
                continue
            fetcher = fetchers.get(target.fetch)
            if fetcher is None:
                logger.error("target {} 需要 {} 抓取器但不可用，已跳过", target.id, target.fetch)
                continue
            watchers[target.id] = Watcher(
                target,
                settings=settings,
                storage=storage,
                fetcher=fetcher,
                notifiers=notifiers,
                throttle=throttle,
            )

        health = HealthMonitor(
            storage=storage,
            notifiers=notifiers,
            throttle=throttle,
            silence_hours=settings.health_silence_hours,
            target_count=len(watchers),
            channels=settings.health_channel_list or None,
        )
        return cls(
            settings=settings,
            targets=targets,
            storage=storage,
            http_client=http_client,
            fetchers=fetchers,
            notifiers=notifiers,
            throttle=throttle,
            watchers=watchers,
            health=health,
        )

    @property
    def channels(self) -> list[str]:
        return sorted(self.notifiers)

    async def aclose(self) -> None:
        for fetcher in self.fetchers.values():
            try:
                await fetcher.aclose()
            except Exception as exc:  # 关闭失败不应阻断其余清理
                logger.warning("关闭抓取器 {} 失败: {}", fetcher.name, exc)
        await self.http_client.aclose()
        await self.storage.close()

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


__all__ = ["Runtime"]
