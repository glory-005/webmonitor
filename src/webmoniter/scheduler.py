"""APScheduler 调度：每个目标一个 interval job，各自带 jitter 错峰。

要点：
* `max_instances=1` —— 上一次还没跑完就不再叠加，避免慢站点把并发堆爆；
* `coalesce=True` —— 进程被挂起后补跑时只跑一次，不补一堆历史任务；
* `misfire_grace_time` —— 允许的错过执行宽限时间；
* 首次执行加随机延迟，避免所有目标同一秒一起冲出去。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from webmoniter.health import HealthMonitor
from webmoniter.models import Target
from webmoniter.settings import Settings
from webmoniter.watcher import CheckResult, Watcher

HEALTH_JOB_ID = "__health__"
#: 首次执行的最大随机延迟（秒）
MAX_START_DELAY = 30


class MonitorScheduler:
    def __init__(
        self,
        *,
        targets: list[Target],
        watchers: dict[str, Watcher],
        settings: Settings,
        health: HealthMonitor | None = None,
    ) -> None:
        self.targets = [target for target in targets if target.enabled]
        self.watchers = watchers
        self.settings = settings
        self.health = health
        self._semaphore = asyncio.Semaphore(max(1, settings.concurrency))
        self._scheduler: AsyncIOScheduler | None = None

    # ------------------------------------------------------------------ 单次执行
    async def run_one(self, target_id: str, *, notify: bool = True) -> CheckResult | None:
        """检查一个目标。notify=False 时只比对、不发送（CLI 默认如此）。"""
        watcher = self.watchers.get(target_id)
        if watcher is None:
            logger.warning("没有 target {} 的 watcher（可能已禁用）", target_id)
            return None
        async with self._semaphore:
            try:
                result = await watcher.check(notify=notify)
            except Exception:
                # 单个目标的意外错误不能拖垮整个调度器
                logger.exception("target {} 检查时出现未捕获异常", target_id)
                return None
        logger.info("target {} {}", target_id, result.brief())
        return result

    async def run_all_once(self, *, notify: bool = True) -> list[CheckResult]:
        """所有目标各检查一次（调试/定时巡检用）。"""

        async def guarded(watcher: Watcher) -> CheckResult | None:
            async with self._semaphore:
                try:
                    return await watcher.check(notify=notify)
                except Exception:
                    logger.exception("target {} 检查时出现未捕获异常", watcher.target.id)
                    return None

        results = await asyncio.gather(*(guarded(w) for w in self.watchers.values()))
        return [result for result in results if result is not None]

    # -------------------------------------------------------------------- 调度
    def build(self) -> AsyncIOScheduler:
        tz = ZoneInfo(self.settings.tz)
        scheduler = AsyncIOScheduler(
            timezone=tz,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
        )
        now = datetime.now(tz)
        for target in self.targets:
            if target.id not in self.watchers:
                continue
            scheduler.add_job(
                self.run_one,
                IntervalTrigger(seconds=target.interval, jitter=target.effective_jitter),
                args=[target.id],
                id=target.id,
                name=target.display_name,
                next_run_time=now
                + timedelta(seconds=random.uniform(0, min(target.interval, MAX_START_DELAY))),
            )
        if self.health is not None and self.health.enabled:
            scheduler.add_job(
                self.health.tick,
                IntervalTrigger(seconds=self.settings.health_interval),
                id=HEALTH_JOB_ID,
                name="自检",
            )
        self._scheduler = scheduler
        return scheduler

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        scheduler = self.build()
        scheduler.start()
        for job in scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            logger.info("已排程 {}（{}），下次执行 {}", job.name, job.id, next_run)
        logger.info("调度器已启动，共 {} 个监控目标", len(self.targets))

        stop_event = stop_event or asyncio.Event()
        try:
            await stop_event.wait()
        finally:
            logger.info("正在停止调度器…")
            scheduler.shutdown(wait=False)


__all__ = ["HEALTH_JOB_ID", "MonitorScheduler"]
