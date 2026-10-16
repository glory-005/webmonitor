"""发送限流。

飞书/钉钉自定义机器人都有分钟级频控（钉钉约 20 条/分钟），
超限后消息会被直接丢弃或返回错误码，所以「先排队再发」是必需品而不是优化项。
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger


class TokenBucket:
    """令牌桶。acquire() 会等到有令牌才返回，因此不会丢消息，只会慢一点。"""

    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute 必须 > 0")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, rate_per_minute // 6))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.total_waited = 0.0
        self.total_acquired = 0

    async def acquire(self) -> float:
        """取一个令牌，返回本次等待秒数。"""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                if elapsed > 0:
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
                    self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self.total_acquired += 1
                    self.total_waited += waited
                    return waited
                wait_for = (1.0 - self._tokens) / self.rate_per_second
                logger.debug("通知限流：等待 {:.1f}s 后发送", wait_for)
                # 持锁 sleep 是刻意的：同一渠道的发送在这里天然串行，顺序稳定
                await asyncio.sleep(wait_for)
                waited += wait_for


class Throttle:
    """按渠道维度的限流器集合。"""

    def __init__(self, rate_per_minute: int = 18) -> None:
        self.rate_per_minute = rate_per_minute
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, channel: str) -> TokenBucket:
        bucket = self._buckets.get(channel)
        if bucket is None:
            bucket = TokenBucket(self.rate_per_minute)
            self._buckets[channel] = bucket
        return bucket

    async def acquire(self, channel: str) -> float:
        return await self.bucket(channel).acquire()

    def stats(self) -> dict[str, dict[str, float]]:
        return {
            name: {"acquired": bucket.total_acquired, "waited": round(bucket.total_waited, 2)}
            for name, bucket in self._buckets.items()
        }


__all__ = ["Throttle", "TokenBucket"]
