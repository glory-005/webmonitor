"""单目标状态机：抓取 → 抽取 → 归一化 → 比对 → 落库 → 通知。

状态语义：
    ┌ 首次运行 ─────────────► baseline（建立基线，不通知）
    │
    └ 已有基线 ── hash 相同 ─► unchanged
                ├ 变化但判为噪声 ─► noise（静默更新基线，避免下次重复评估）
                ├ 真实变化 ───────► changed（落库 + 通知）
                └ 抓取/抽取失败 ──► error（连续 N 次后告警，恢复时补一条 recovered）
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from webmoniter.diff import DiffResult, diff_text, hash_text, unified_diff
from webmoniter.extract import build_soup, extract
from webmoniter.fetch.base import Fetcher
from webmoniter.models import NotificationMessage, NotifyResult, Target
from webmoniter.normalize import normalize_text
from webmoniter.notify.base import Notifier
from webmoniter.render import change_message, error_message, recovered_message
from webmoniter.settings import Settings
from webmoniter.storage import Storage
from webmoniter.throttle import Throttle
from webmoniter.utils import iso, parse_iso, truncate, utcnow

CheckStatus = Literal["baseline", "unchanged", "noise", "changed", "error"]

#: CheckResult.excerpt 保留的字符数
EXCERPT_LIMIT = 400

#: 待补投通知的最长保留时间（秒）。超过就丢弃：渠道几小时后才恢复时，
#: 推一条过期告警会让人误以为是刚刚发生的变更。
PENDING_MAX_AGE_SECONDS = 3600


@dataclass(slots=True)
class CheckResult:
    """一次检查的结果（CLI 与日志用）。"""

    target_id: str
    status: CheckStatus
    detail: str = ""
    added: int = 0
    removed: int = 0
    similarity: float = 1.0
    elapsed_ms: int = 0
    notified: list[str] = field(default_factory=list)
    recovered: bool = False
    #: 抽取文本的开头片段，便于 `check --show-text` 排查选择器
    excerpt: str = ""
    #: 逐行 diff（仅 changed 时有值），便于 `check --show-diff`
    unified: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "error"

    def brief(self) -> str:
        extra = f"（+{self.added}/-{self.removed}）" if self.status == "changed" else ""
        notified = f" → {','.join(self.notified)}" if self.notified else ""
        return f"[{self.status}]{extra} {self.detail}{notified}"


class Watcher:
    """一个 Watcher 实例只负责一个 Target。"""

    def __init__(
        self,
        target: Target,
        *,
        settings: Settings,
        storage: Storage,
        fetcher: Fetcher,
        notifiers: dict[str, Notifier],
        throttle: Throttle,
    ) -> None:
        self.target = target
        self.settings = settings
        self.storage = storage
        self.fetcher = fetcher
        self.notifiers = notifiers
        self.throttle = throttle
        #: 上一次没能送达的变更通知，下一轮补投（进程重启则丢，但 changes 表里仍有记录）
        self._pending: NotificationMessage | None = None
        self._pending_at: float = 0.0
        self._pending_key: str | None = None

    # ------------------------------------------------------------------ 主流程
    async def check(self, *, notify: bool = True) -> CheckResult:
        """检查一次。除编程错误外不抛异常——失败会走 error 状态。"""
        started = time.perf_counter()
        target = self.target
        state = await self.storage.get_state(target.id) or {}
        await self._flush_pending(state, notify=notify)

        result = await self.fetcher.fetch(target)
        if result.error or not result.text:
            return await self._handle_error(
                state,
                result.error or "响应内容为空",
                result.status,
                result.elapsed_ms,
                notify=notify,
                started=started,
            )

        try:
            text = self._extract_text(result.text)
        except Exception as exc:  # 抽取层的意外错误也要走错误通道，而不是崩掉调度
            logger.exception("target {} 抽取时出现意外错误", target.id)
            return await self._handle_error(
                state,
                f"抽取异常 {type(exc).__name__}: {exc}",
                result.status,
                result.elapsed_ms,
                notify=notify,
                started=started,
            )

        if not text.strip():
            # 选择器失效 / 站点改版 / 内容被清空——这类必须走错误通道，
            # 否则会被误报成「页面内容全部删除」。
            return await self._handle_error(
                state,
                "抽取结果为空：选择器可能已失效，或页面结构发生变化",
                result.status,
                result.elapsed_ms,
                notify=notify,
                started=started,
            )

        digest = hash_text(text)
        previous_hash = state.get("last_hash")

        if not previous_hash:
            await self._save_baseline(text, digest, result.status, result.elapsed_ms)
            return CheckResult(
                target_id=target.id,
                status="baseline",
                detail=f"建立基线（{len(text)} 字符）",
                elapsed_ms=self._elapsed(started),
                excerpt=truncate(text, EXCERPT_LIMIT, ""),
            )

        if digest == previous_hash:
            await self.storage.add_snapshot(
                target.id,
                http_status=result.status,
                content_hash=digest,
                text=None,  # 未变化不存正文，只留指纹与时间，控制库体积
                error=None,
                elapsed_ms=result.elapsed_ms,
            )
            recovery = await self._mark_ok(state, digest)
            notified: list[str] = []
            if recovery is not None and notify:
                notified = await self._notify("recovered", recovery, state)
            return CheckResult(
                target_id=target.id,
                status="unchanged",
                detail="内容未变化",
                elapsed_ms=self._elapsed(started),
                notified=notified,
                recovered=recovery is not None,
                excerpt=truncate(text, EXCERPT_LIMIT, ""),
            )

        # ---- 内容指纹变了，做逐行比对 ----
        previous = await self.storage.latest_snapshot(target.id, with_text=True)
        previous_text = (previous or {}).get("text") or ""
        previous_snapshot_id = (previous or {}).get("id")

        if previous_text:
            diff = diff_text(previous_text, text, target.diff)
        else:
            # 历史正文被清理过，无法给出逐行差异，但仍然如实报告变化
            diff = DiffResult(changed=True, reason="no-history", changed_chars=len(text))

        snapshot_id = await self.storage.add_snapshot(
            target.id,
            http_status=result.status,
            content_hash=digest,
            text=text,
            error=None,
            elapsed_ms=result.elapsed_ms,
        )
        artifact = await self._write_artifact(text)
        recovery = await self._mark_ok(state, digest)

        if not diff.changed:
            logger.debug(
                "target {} 内容变化被判为噪声（{}），静默更新基线", target.id, diff.reason
            )
            notified: list[str] = []
            if recovery is not None and notify:
                notified = await self._notify("recovered", recovery, state)
            return CheckResult(
                target_id=target.id,
                status="noise",
                detail=f"疑似噪声（{diff.reason}，{diff.changed_chars} 字符）",
                similarity=diff.similarity,
                elapsed_ms=self._elapsed(started),
                notified=notified,
                recovered=recovery is not None,
                excerpt=truncate(text, EXCERPT_LIMIT, ""),
            )

        await self.storage.add_change(
            target.id,
            from_snapshot_id=previous_snapshot_id,
            to_snapshot_id=snapshot_id,
            added=diff.added,
            removed=diff.removed,
            similarity=diff.similarity,
            summary=diff.summary(),
        )
        logger.info("target {} 检测到变更：{}", target.id, diff.summary())

        notified: list[str] = []
        if notify:
            message = change_message(
                target,
                diff,
                text,
                elapsed_ms=result.elapsed_ms,
                max_lines=self.settings.max_notify_lines,
            )
            if artifact is not None:
                message.lines.append(f"**内容留存**：{artifact}")
            notified = await self._notify("changed", message, state, dedup_key=f"{digest}")
            if self._should_queue_pending(notified, state):
                # 只有「真的尝试发送但全部失败」才排队补投
                self._pending = message
                self._pending_at = time.monotonic()
                self._pending_key = digest
                logger.warning("target {} 的变更通知未送达，将于下一轮补投", target.id)
        if recovery is not None and notify:
            notified += await self._notify("recovered", recovery, state)

        return CheckResult(
            target_id=target.id,
            status="changed",
            detail=diff.summary(),
            added=diff.added,
            removed=diff.removed,
            similarity=diff.similarity,
            elapsed_ms=self._elapsed(started),
            notified=notified,
            recovered=recovery is not None,
            excerpt=truncate(text, EXCERPT_LIMIT, ""),
            unified=unified_diff(previous_text, text) if previous_text else "",
        )

    # ------------------------------------------------------------------ 子步骤
    def _deliverable_channels(self) -> list[str]:
        """去重后、确实已在环境里配置好的渠道。"""
        return [
            channel
            for channel in dict.fromkeys(self.target.notify.channels)
            if channel in self.notifiers
        ]

    @staticmethod
    def _in_silence_window(state: dict[str, Any]) -> bool:
        until = parse_iso(state.get("silenced_until"))
        return bool(until and utcnow() < until)

    def _should_queue_pending(self, notified: list[str], state: dict[str, Any]) -> bool:
        """只有「确实尝试发送但一条都没成功」才该排队补投。

        静默期、未订阅 changed、渠道名写错这三种情况都必须排除：
        * 静默期排队 → 静默一结束就补发一条过期告警，等于静默失效；
        * 渠道名写错排队 → 待投项永远清不掉，每个周期都白跑一次。
        """
        if notified or "changed" not in self.target.notify.on:
            return False
        if self._in_silence_window(state):
            return False
        return bool(self._deliverable_channels())

    def _clear_pending(self) -> None:
        self._pending = None
        self._pending_at = 0.0
        self._pending_key = None

    async def _flush_pending(self, state: dict[str, Any], *, notify: bool) -> None:
        """补投上一轮没送达的变更通知。

        状态 last_hash 是在发送之前落库的，所以「已落状态但没发出去」的变更
        不会在下一轮被重新检出——靠这个内存里的待投队列兜住。
        代价：进程重启会丢掉待投项，只保留最近一条，且超过 PENDING_MAX_AGE_SECONDS 就丢弃
        （changes 表与 artifacts 里仍有记录）。
        """
        if self._pending is None or not notify:
            return
        if time.monotonic() - self._pending_at > PENDING_MAX_AGE_SECONDS:
            logger.error(
                "target {} 的待补投通知已超过 {}s 仍未送达，放弃补投（changes 表与 artifacts 仍有记录）",
                self.target.id,
                PENDING_MAX_AGE_SECONDS,
            )
            self._clear_pending()
            return

        message = self._pending
        sent = await self._notify(message.kind, message, state, dedup_key=self._pending_key)
        if sent:
            logger.info(
                "target {} 补投了上一轮未送达的通知 → {}", self.target.id, ",".join(sent)
            )
            self._clear_pending()

    def _extract_text(self, html: str) -> str:
        soup = build_soup(html, self.target.parser, self.target.diff.strip_selectors)
        raw = extract(soup, html, self.target.extract)
        return normalize_text(raw, self.target.diff)

    async def _save_baseline(
        self, text: str, digest: str, http_status: int | None, elapsed_ms: int
    ) -> None:
        await self.storage.add_snapshot(
            self.target.id,
            http_status=http_status,
            content_hash=digest,
            text=text,
            error=None,
            elapsed_ms=elapsed_ms,
        )
        await self._write_artifact(text)
        await self.storage.save_state(
            self.target.id,
            last_hash=digest,
            last_ok_at=iso(utcnow()),
            consecutive_failures=0,
            error_notified=0,
        )
        logger.info("target {} 建立基线（{} 字符）", self.target.id, len(text))

    async def _mark_ok(self, state: dict[str, Any], digest: str) -> NotificationMessage | None:
        """记录一次成功；若此前处于失败态，返回恢复通知（由调用方决定何时发送）。

        之所以把「发送」交给调用方，是为了让消息顺序读起来自然：
        变更通知在前、恢复通知在后。
        """
        failures = int(state.get("consecutive_failures") or 0)
        last_ok = parse_iso(state.get("last_ok_at"))
        await self.storage.save_state(
            self.target.id,
            last_hash=digest,
            last_ok_at=iso(utcnow()),
            consecutive_failures=0,
            error_notified=0,
        )
        if failures <= 0:
            return None

        downtime = (utcnow() - last_ok).total_seconds() if last_ok else None
        logger.info("target {} 已恢复正常（此前连续失败 {} 次）", self.target.id, failures)
        return recovered_message(self.target, failures, downtime_seconds=downtime)

    async def _handle_error(
        self,
        state: dict[str, Any],
        error: str,
        http_status: int | None,
        elapsed_ms: int,
        *,
        notify: bool,
        started: float,
    ) -> CheckResult:
        failures = int(state.get("consecutive_failures") or 0) + 1
        await self.storage.add_snapshot(
            self.target.id,
            http_status=http_status,
            content_hash=None,
            text=None,
            error=truncate(error, 1000),
            elapsed_ms=elapsed_ms,
        )
        await self.storage.save_state(self.target.id, consecutive_failures=failures)
        logger.warning("target {} 检查失败（第 {} 次）: {}", self.target.id, failures, error)

        threshold = self.target.notify.error_after_consecutive
        already_notified = bool(state.get("error_notified"))
        notified: list[str] = []
        if notify and failures >= threshold and not already_notified:
            notified = await self._notify(
                "error",
                error_message(self.target, error, failures, last_ok_at=state.get("last_ok_at")),
                state,
            )
            if notified:
                # 只有真的送达才落标记：否则（渠道全失败）下一轮继续尝试，
                # 不会因为一次发送失败就把这个目标永久静默掉。
                await self.storage.save_state(self.target.id, error_notified=1)
        return CheckResult(
            target_id=self.target.id,
            status="error",
            detail=truncate(error, 300),
            elapsed_ms=self._elapsed(started),
            notified=notified,
        )

    async def _notify(
        self,
        kind: str,
        message: NotificationMessage,
        state: dict[str, Any],
        dedup_key: str | None = None,
    ) -> list[str]:
        """按目标策略发送通知，返回发送成功的渠道名（已去重）。"""
        options = self.target.notify
        if kind not in options.on:
            logger.debug("target {} 未订阅 {} 事件，跳过通知", self.target.id, kind)
            return []
        if not options.channels:
            return []

        if self._in_silence_window(state):
            logger.info(
                "target {} 处于静默期（至 {}），跳过 {} 通知",
                self.target.id,
                state.get("silenced_until"),
                kind,
            )
            return []

        # 这里刻意**不**按内容 digest 去重：
        # (1) 状态 last_hash 是在发送之前落库的，崩溃后本来就不会重复推送；
        # (2) 按 digest 去重会误杀 A→B→A→B 这类来回变动的第二次告警。
        # dedup_key 只写进 notifications 表，供事后审计。
        audit_key = f"{self.target.id}:{kind}:{dedup_key}" if dedup_key else None
        attempts = max(1, self.settings.notify_retry_attempts)
        sent: list[str] = []
        for channel in dict.fromkeys(options.channels):  # 渠道名去重且保持顺序
            notifier = self.notifiers.get(channel)
            if notifier is None:
                logger.warning(
                    "target {} 引用了未配置的渠道 {}，请检查 .env", self.target.id, channel
                )
                continue
            await self.throttle.acquire(channel)
            result = await self._send_with_retry(notifier, message)
            await self.storage.record_notification(
                self.target.id,
                channel=channel,
                kind=kind,
                ok=result.ok,
                detail=result.detail,
                dedup_key=audit_key,
            )
            if result.ok:
                sent.append(channel)
                logger.info("target {} 的 {} 通知已发送到 {}", self.target.id, kind, channel)
            else:
                recorded = "该变更已写入 changes 表；" if kind == "changed" else ""
                logger.error(
                    "渠道 {} 发送失败（尝试 {} 次、重试 {} 次）: {}{}"
                    "请用 `webmoniter test-notify` 检查渠道配置",
                    channel,
                    attempts,
                    attempts - 1,
                    result.detail,
                    recorded,
                )

        if sent:
            await self.storage.save_state(self.target.id, last_notified_at=iso(utcnow()))
        return sent

    async def _send_with_retry(
        self, notifier: Notifier, message: NotificationMessage
    ) -> NotifyResult:
        """带指数退避的发送重试，减少网络抖动造成的漏报。"""
        attempts = max(1, self.settings.notify_retry_attempts)
        result = NotifyResult(channel=notifier.name, ok=False, detail="未发送")
        for attempt in range(1, attempts + 1):
            result = await notifier.send(message)
            if result.ok or attempt == attempts:
                return result
            delay = float(min(2**attempt, 8))
            logger.warning(
                "渠道 {} 第 {}/{} 次发送失败（{}），{:.0f}s 后重试",
                notifier.name,
                attempt,
                attempts,
                result.detail,
                delay,
            )
            await asyncio.sleep(delay)
        return result

    # -------------------------------------------------------------------- 杂项
    async def _write_artifact(self, text: str) -> Path | None:
        """把抽取文本留一份（按字节截断），方便事后核对是不是误报。"""
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", self.target.id) or "target"
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        blob = text.encode("utf-8", errors="ignore")[: self.settings.max_artifact_bytes]

        def write() -> Path:
            directory = self.settings.resolve(self.settings.artifacts_dir) / safe_id
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{stamp}.txt"
            path.write_bytes(blob)
            return path

        try:
            # 文件 IO 丢到线程池，避免卡住共享事件循环里其他目标的抓取
            return await asyncio.to_thread(write)
        except OSError as exc:
            logger.warning("target {} 留存内容失败: {}", self.target.id, exc)
            return None

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


__all__ = ["CheckResult", "CheckStatus", "Watcher"]
