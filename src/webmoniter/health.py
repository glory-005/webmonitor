"""自监控：进程活着但所有目标都抓不到东西时，必须有人告诉你。

这是监控类项目最容易被忽略、也最致命的一环——「静默失效」。
判定逻辑：距离最近一次成功检查超过 health_silence_hours 就告警；
进程刚启动还没有成功记录时，用启动时刻计时，避免一启动就误报。
"""

from __future__ import annotations

from loguru import logger

from webmoniter.notify.base import Notifier
from webmoniter.render import health_message
from webmoniter.storage import Storage
from webmoniter.throttle import Throttle
from webmoniter.utils import format_elapsed, utcnow


class HealthMonitor:
    def __init__(
        self,
        *,
        storage: Storage,
        notifiers: dict[str, Notifier],
        throttle: Throttle,
        silence_hours: float,
        target_count: int,
        channels: list[str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.storage = storage
        self.notifiers = notifiers
        self.throttle = throttle
        self.silence_hours = silence_hours
        self.target_count = target_count
        self.channels = channels or list(notifiers)
        self.enabled = enabled and silence_hours > 0 and bool(self.channels)
        self._started_at = utcnow()
        self._last_alert_age: float | None = None  # 上次告警时「无成功检查」的时长，用于周期性重报

    async def tick(self) -> str | None:
        """被调度器定期调用。返回 'alert' / 'ok' / None。"""
        if not self.enabled:
            return None

        age = await self.storage.health_age_seconds()
        if age is None:
            age = (utcnow() - self._started_at).total_seconds()

        threshold = self.silence_hours * 3600
        if age < threshold:
            if self._last_alert_age is not None:
                logger.info("自检恢复正常：最近一次成功检查在 {} 之前", format_elapsed(age * 1000))
            self._last_alert_age = None
            return "ok"

        # 每隔一个静默周期重报一次，避免「告警一次后再无人知晓」
        if self._last_alert_age is not None and age - self._last_alert_age < threshold:
            return None

        logger.error(
            "自检异常：已有 {} 没有成功检查（阈值 {}h）", format_elapsed(age * 1000), self.silence_hours
        )
        self._last_alert_age = age
        await self._alert(age)
        return "alert"

    async def _alert(self, age_seconds: float) -> None:
        message = health_message(
            silence_hours=self.silence_hours, age_seconds=age_seconds, target_count=self.target_count
        )
        for channel in self.channels:
            notifier = self.notifiers.get(channel)
            if notifier is None:
                logger.warning("自检告警渠道 {} 未配置，已跳过", channel)
                continue
            await self.throttle.acquire(channel)
            result = await notifier.send(message)
            await self.storage.record_notification(
                "__health__",
                channel=channel,
                kind="error",
                ok=result.ok,
                detail=result.detail,
                dedup_key=None,
            )
            if not result.ok:
                logger.error("自检告警发送到 {} 失败: {}", channel, result.detail)


__all__ = ["HealthMonitor"]
