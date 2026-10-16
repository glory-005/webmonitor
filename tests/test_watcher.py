"""状态机测试：基线 → 未变 → 变更 → 噪声 → 失败 → 恢复。

这些是项目的核心回归测试：误报/漏报都从这里冒出来。
"""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import FakeFetcher, FakeNotifier
from webmoniter.fetch.base import FetchResult
from webmoniter.scheduler import MonitorScheduler
from webmoniter.storage import Storage
from webmoniter.throttle import Throttle
from webmoniter.utils import iso, utcnow
from webmoniter.watcher import PENDING_MAX_AGE_SECONDS, Watcher

HTML_A = """<html><body><div class="list"><ul>
<li>公告A</li><li>公告B</li>
</ul></div></body></html>"""

HTML_B = """<html><body><div class="list"><ul>
<li>公告A</li><li>公告B</li><li>公告C-招标公告</li>
</ul></div></body></html>"""

HTML_EMPTY_TARGET = """<html><body><div class="other">内容换了位置</div></body></html>"""

HTML_C = """<html><body><div class="list"><ul>
<li>公告A</li><li>公告B</li><li>公告C-招标公告</li><li>公告D</li>
</ul></div></body></html>"""

CSS_RULE = {"type": "css", "selector": "div.list ul", "mode": "text"}


def build_watcher(storage: Storage, settings, make_target, *, notifier: FakeNotifier, **target_kwargs):
    target = make_target(
        extract=[CSS_RULE],
        notify={
            "channels": [notifier.name],
            "on": ["changed", "error", "recovered"],
            "error_after_consecutive": 2,
            "mention_all_keywords": ["招标"],
        },
        **target_kwargs,
    )
    fetcher = FakeFetcher(HTML_A)
    watcher = Watcher(
        target,
        settings=settings,
        storage=storage,
        fetcher=fetcher,
        notifiers={notifier.name: notifier},
        throttle=Throttle(1000),
    )
    return watcher, fetcher


async def test_full_lifecycle(storage: Storage, settings, make_target) -> None:
    notifier = FakeNotifier()
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)

    # 1) 首次运行建立基线，不发通知
    result = await watcher.check()
    assert result.status == "baseline"
    assert notifier.messages == []

    # 2) 内容没变
    result = await watcher.check()
    assert result.status == "unchanged"
    assert result.notified == []

    # 3) 真实变更 + 命中关键词 → @所有人
    fetcher.push(HTML_B)
    result = await watcher.check()
    assert result.status == "changed"
    assert result.added == 1
    assert result.notified == ["feishu_ops"]
    assert len(notifier.messages) == 1
    assert notifier.messages[0].mention_all is True
    assert "公告C-招标公告" in notifier.messages[0].to_markdown()

    # 4) 连续失败：第 1 次不告警，第 2 次告警
    fetcher.push(FetchResult(url="https://example.com/page", error="ConnectionError: boom"))
    result = await watcher.check()
    assert result.status == "error"
    assert len(notifier.messages) == 1

    result = await watcher.check()
    assert result.status == "error"
    assert len(notifier.messages) == 2
    assert notifier.messages[1].kind == "error"
    assert "连续失败" in notifier.messages[1].to_markdown()

    # 5) 恢复正常 → 补一条恢复通知
    fetcher.push(HTML_B)
    result = await watcher.check()
    assert result.status == "unchanged"
    assert result.recovered is True
    assert len(notifier.messages) == 3
    assert notifier.messages[2].kind == "recovered"


async def test_small_change_is_treated_as_noise_and_baseline_moves(
    storage: Storage, settings, make_target
) -> None:
    notifier = FakeNotifier()
    watcher, fetcher = build_watcher(
        storage, settings, make_target, notifier=notifier, diff={"min_changed_chars": 500}
    )

    assert (await watcher.check()).status == "baseline"
    fetcher.push(HTML_B)
    result = await watcher.check()

    assert result.status == "noise"
    assert result.notified == []
    assert notifier.messages == []

    # 噪声也要静默更新基线，否则每次检查都会重新判一遍
    assert (await watcher.check()).status == "unchanged"


async def test_failed_selector_is_reported_as_error_not_deletion(
    storage: Storage, settings, make_target
) -> None:
    notifier = FakeNotifier()
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)

    assert (await watcher.check()).status == "baseline"
    fetcher.push(HTML_EMPTY_TARGET)  # 选择器匹配不到任何节点
    result = await watcher.check()

    assert result.status == "error"
    assert "抽取结果为空" in result.detail


async def test_unknown_channel_does_not_break_check(
    storage: Storage, settings, make_target
) -> None:
    notifier = FakeNotifier(name="feishu_ops")
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)
    watcher.notifiers = {}  # 模拟 targets.yaml 引用了未配置的渠道

    assert (await watcher.check()).status == "baseline"
    fetcher.push(HTML_B)
    result = await watcher.check()

    assert result.status == "changed"
    assert result.notified == []
    assert notifier.messages == []


async def test_unchanged_checks_do_not_store_full_text(
    storage: Storage, settings, make_target
) -> None:
    """未变化时只存指纹，不存正文——否则数据库会被整页文本撑爆。"""
    notifier = FakeNotifier()
    watcher, _ = build_watcher(storage, settings, make_target, notifier=notifier)

    await watcher.check()  # 基线（存正文）
    await watcher.check()  # 未变化（不存正文）

    latest = await storage.latest_snapshot("t1")
    assert latest is not None
    assert latest["text"] is None
    assert latest["content_hash"] is not None


async def test_failed_delivery_does_not_silence_error_alarm(
    storage: Storage, settings, make_target
) -> None:
    """发送失败时不能把 error_notified 落库，否则错误告警会被永久静默。"""
    notifier = FakeNotifier(ok=False)
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)

    assert (await watcher.check()).status == "baseline"

    fetcher.push(FetchResult(url="https://example.com/page", error="boom"))
    for _ in range(3):  # 连续失败 3 次，阈值是 2
        result = await watcher.check()
        assert result.status == "error"
        assert result.notified == []

    state = await storage.get_state("t1")
    assert state is not None
    assert state["consecutive_failures"] == 3
    assert not state["error_notified"], "发送失败却落了标记 → 该目标会被永久静默"

    # 每次都尝试过发送（阈值达成后每轮都试），说明没有被 flag 卡住
    error_messages = [m for m in notifier.messages if m.kind == "error"]
    assert len(error_messages) == 2

    audit = await storage.recent_notifications(limit=10)
    assert audit and all(row["ok"] == 0 for row in audit)


async def test_undelivered_change_is_retried_on_next_tick(
    storage: Storage, settings, make_target
) -> None:
    """渠道临时故障时，变更通知要在下一轮补投，而不是永久丢掉。"""
    notifier = FakeNotifier(ok=False)
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)

    assert (await watcher.check()).status == "baseline"

    fetcher.push(HTML_B)
    result = await watcher.check()
    assert result.status == "changed"
    assert result.notified == []
    assert len(notifier.messages) == 1  # 发出但失败
    assert watcher._pending is not None  # noqa: SLF001 - 待投队列是这里的行为契约

    notifier.ok = True
    result = await watcher.check()  # 内容没再变，但应补投上一轮的通知
    assert len(notifier.messages) == 2
    assert notifier.messages[1].kind == "changed"
    assert watcher._pending is None  # noqa: SLF001
    assert "公告C-招标公告" in notifier.messages[1].to_markdown()


async def test_scheduler_run_one_respects_notify_flag(
    storage: Storage, settings, make_target
) -> None:
    """`webmoniter check` 默认不发消息：notify=False 时一个渠道都不该发。"""
    notifier = FakeNotifier()
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)
    scheduler = MonitorScheduler(
        targets=[watcher.target], watchers={watcher.target.id: watcher}, settings=settings
    )

    baseline = await scheduler.run_one(watcher.target.id)
    assert baseline is not None and baseline.status == "baseline"

    fetcher.push(HTML_B)
    dry = await scheduler.run_one(watcher.target.id, notify=False)
    assert dry is not None and dry.status == "changed"
    assert dry.notified == []
    assert notifier.messages == []

    # 再次变更，这次带 notify=True，应当真的发出去
    fetcher.push(HTML_C)
    wet = await scheduler.run_one(watcher.target.id, notify=True)
    assert wet is not None and wet.status == "changed"
    assert wet.notified == ["feishu_ops"]
    assert len(notifier.messages) == 1


async def test_silenced_change_is_not_queued_for_retry(
    storage: Storage, settings, make_target
) -> None:
    """静默期内的变更不能被排队补投，否则静默一结束就推一条过期告警。"""
    notifier = FakeNotifier(ok=False)
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)

    assert (await watcher.check()).status == "baseline"
    await storage.save_state("t1", silenced_until=iso(utcnow() + timedelta(hours=1)))

    fetcher.push(HTML_B)
    result = await watcher.check()

    assert result.status == "changed"
    assert result.notified == []
    assert notifier.messages == []
    assert watcher._pending is None  # noqa: SLF001


async def test_unknown_channel_change_is_not_queued_for_retry(
    storage: Storage, settings, make_target
) -> None:
    """渠道名写错（压根没得发）时不能排队，否则待投项每轮白转、永远清不掉。"""
    notifier = FakeNotifier(ok=False)
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)
    watcher.notifiers = {}  # 模拟 targets.yaml 引用了未配置的渠道

    assert (await watcher.check()).status == "baseline"
    fetcher.push(HTML_B)
    result = await watcher.check()

    assert result.status == "changed"
    assert result.notified == []
    assert watcher._pending is None  # noqa: SLF001


async def test_stale_pending_is_dropped(settings, storage: Storage, make_target) -> None:
    """待补投通知超过时限就丢弃，避免渠道几小时后恢复时推一条陈旧告警。"""
    notifier = FakeNotifier(ok=False)
    watcher, fetcher = build_watcher(storage, settings, make_target, notifier=notifier)

    assert (await watcher.check()).status == "baseline"
    fetcher.push(HTML_B)
    assert (await watcher.check()).status == "changed"
    assert watcher._pending is not None  # noqa: SLF001

    # 把待投项「变老」，再跑一轮：应当丢弃且不再尝试发送
    watcher._pending_at -= PENDING_MAX_AGE_SECONDS + 1  # noqa: SLF001
    result = await watcher.check()

    assert result.status == "unchanged"
    assert watcher._pending is None  # noqa: SLF001
    assert len(notifier.messages) == 1  # 只有最初那次失败尝试
