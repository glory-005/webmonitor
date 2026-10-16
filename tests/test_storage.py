"""存储层测试：状态 UPSERT、快照正文字段、变更/通知审计。"""

from __future__ import annotations

import pytest

from webmoniter.storage import Storage
from webmoniter.utils import iso, utcnow


async def test_save_state_upserts_without_duplicate(storage: Storage) -> None:
    await storage.save_state("t1", consecutive_failures=1)
    await storage.save_state("t1", consecutive_failures=2, last_hash="abc")

    state = await storage.get_state("t1")
    assert state is not None
    assert state["consecutive_failures"] == 2
    assert state["last_hash"] == "abc"


async def test_save_state_rejects_unknown_field(storage: Storage) -> None:
    # 字段名进 SQL 之前必须过白名单
    with pytest.raises(ValueError, match="state 表不存在的字段"):
        await storage.save_state("t1", drop_table="x")


async def test_state_row_is_created_on_first_write(storage: Storage) -> None:
    assert await storage.get_state("t1") is None
    await storage.save_state("t1", last_notified_at=iso(utcnow()))
    assert await storage.get_state("t1") is not None


async def test_latest_snapshot_with_text_skips_hash_only_rows(storage: Storage) -> None:
    await storage.add_snapshot(
        "t1", http_status=200, content_hash="h1", text="第一版", error=None, elapsed_ms=5
    )
    # 未变化时写入的行没有正文
    await storage.add_snapshot(
        "t1", http_status=200, content_hash="h1", text=None, error=None, elapsed_ms=3
    )

    latest = await storage.latest_snapshot("t1")
    assert latest is not None and latest["text"] is None

    with_text = await storage.latest_snapshot("t1", with_text=True)
    assert with_text is not None and with_text["text"] == "第一版"


async def test_add_snapshot_returns_rowid(storage: Storage) -> None:
    assert await storage.add_snapshot(
        "t1", http_status=500, content_hash=None, text=None, error="boom", elapsed_ms=1
    ) > 0


async def test_notification_audit(storage: Storage) -> None:
    await storage.record_notification(
        "t1", channel="feishu_ops", kind="changed", ok=True, detail="ok", dedup_key="k1"
    )
    await storage.record_notification(
        "t1", channel="dingtalk_ops", kind="error", ok=False, detail="failed"
    )

    assert await storage.notification_exists("k1")
    assert not await storage.notification_exists("k2")

    rows = await storage.recent_notifications(limit=10)
    assert len(rows) == 2
    assert rows[0]["ok"] == 0  # 倒序，最近一条失败


async def test_recent_changes_joins_target_name(storage: Storage) -> None:
    await storage.upsert_target(
        "t1", name="某公告列表", url="https://example.com", config_hash="c1", enabled=True
    )
    await storage.add_change(
        "t1",
        from_snapshot_id=None,
        to_snapshot_id=None,
        added=1,
        removed=0,
        similarity=0.9,
        summary="新增 1 行",
    )

    rows = await storage.recent_changes()
    assert rows[0]["target_name"] == "某公告列表"
    assert rows[0]["url"] == "https://example.com"


async def test_config_change_can_reset_baseline(storage: Storage) -> None:
    await storage.upsert_target("t1", name="", url="https://e.com", config_hash="old", enabled=True)
    row = await storage.get_target("t1")
    assert row is not None and row["config_hash"] == "old"

    await storage.save_state("t1", last_hash=None)
    state = await storage.get_state("t1")
    assert state is not None and state["last_hash"] is None


async def test_health_age(storage: Storage) -> None:
    assert await storage.health_age_seconds() is None  # 从未成功过
    await storage.save_state("t1", last_ok_at=iso(utcnow()))
    age = await storage.health_age_seconds()
    assert age is not None and age < 5


async def test_stats_counts_rows(storage: Storage) -> None:
    await storage.upsert_target("t1", name="", url="https://e.com", config_hash="c", enabled=True)
    await storage.add_snapshot(
        "t1", http_status=200, content_hash="h", text="x", error=None, elapsed_ms=1
    )
    counts = await storage.stats()
    assert counts["targets"] == 1
    assert counts["snapshots"] == 1
    assert counts["changes"] == 0
