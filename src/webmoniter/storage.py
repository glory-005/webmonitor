"""SQLite 持久化（aiosqlite）。

设计取舍：
* 只在「首次基线 / 内容变化 / 抓取失败」时保存正文，未变化时只写 hash 与时间戳，
  这样既有完整的变更历史，又不会因为每 5 分钟存一次整页而把库撑爆。
* state 表记录每个目标的当前状态（最后 hash、连续失败次数等），进程重启不丢，
  因此重启后不会把「已知内容」误报成变更。
* 所有 SQL 都走 _fetchone / _fetchall / _write 三个小助手，它们只用
  `cursor = await db.execute(...)` 这一种统一可 await 的写法，
  不依赖 aiosqlite 0.21 才有的「execute 同时是异步上下文管理器」特性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from webmoniter.models import STATE_FIELDS
from webmoniter.utils import iso, parse_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    config_hash TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id    TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER,
    content_hash TEXT,
    text         TEXT,
    error        TEXT,
    elapsed_ms   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_target ON snapshots(target_id, id DESC);

CREATE TABLE IF NOT EXISTS changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id        TEXT NOT NULL,
    from_snapshot_id INTEGER,
    to_snapshot_id   INTEGER,
    detected_at      TEXT NOT NULL,
    added            INTEGER NOT NULL DEFAULT 0,
    removed          INTEGER NOT NULL DEFAULT 0,
    similarity       REAL NOT NULL DEFAULT 1.0,
    summary          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_changes_target ON changes(target_id, id DESC);

CREATE TABLE IF NOT EXISTS state (
    target_id            TEXT PRIMARY KEY,
    last_hash            TEXT,
    last_ok_at           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_notified_at     TEXT,
    silenced_until       TEXT,
    error_notified       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id  TEXT,
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',
    sent_at    TEXT NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 0,
    detail     TEXT NOT NULL DEFAULT '',
    dedup_key  TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_dedup ON notifications(dedup_key);
"""


class Storage:
    """异步 SQLite 仓储层。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    # ---------------------------------------------------------------- 生命周期
    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage 未初始化，请先 await storage.init()")
        return self._db

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._write("PRAGMA journal_mode=WAL")
        await self._write("PRAGMA synchronous=NORMAL")
        cursor = await self._db.executescript(SCHEMA)
        await cursor.close()
        await self._db.commit()
        logger.debug("Storage 就绪: {}", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Storage:
        await self.init()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -------------------------------------------------------------- SQL 小助手
    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        cursor = await self.db.execute(sql, params)
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return dict(row) if row is not None else None

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await self.db.execute(sql, params)
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        return [dict(row) for row in rows]

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        cursor = await self.db.execute(sql, params)
        await cursor.close()

    async def _insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """执行插入并返回 rowid；失败时返回 0。"""
        cursor = await self.db.execute(sql, params)
        try:
            rowid = cursor.lastrowid
        finally:
            await cursor.close()
        await self.db.commit()
        return int(rowid or 0)

    # ------------------------------------------------------------------ targets
    async def upsert_target(
        self, target_id: str, *, name: str, url: str, config_hash: str, enabled: bool
    ) -> None:
        await self._write(
            """
            INSERT INTO targets(id, name, url, config_hash, enabled, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url,
                config_hash=excluded.config_hash, enabled=excluded.enabled
            """,
            (target_id, name, url, config_hash, int(enabled), iso(utcnow())),
        )
        await self.db.commit()

    # -------------------------------------------------------------------- state
    async def get_state(self, target_id: str) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM state WHERE target_id=?", (target_id,))

    async def save_state(self, target_id: str, **fields: Any) -> None:
        unknown = set(fields) - STATE_FIELDS
        if unknown:
            raise ValueError(f"state 表不存在的字段: {sorted(unknown)}")
        # 单条 UPSERT，避免 read-then-insert 在并发（常驻进程 + 手动 check）下撞 UNIQUE
        await self._write(
            "INSERT INTO state(target_id) VALUES(?) ON CONFLICT(target_id) DO NOTHING",
            (target_id,),
        )
        if fields:
            assignments = ", ".join(f"{name}=?" for name in fields)
            await self._write(
                f"UPDATE state SET {assignments} WHERE target_id=?",  # noqa: S608 - 字段名已白名单校验
                (*fields.values(), target_id),
            )
        await self.db.commit()

    async def get_target(self, target_id: str) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM targets WHERE id=?", (target_id,))

    # ---------------------------------------------------------------- snapshots
    async def add_snapshot(
        self,
        target_id: str,
        *,
        http_status: int | None,
        content_hash: str | None,
        text: str | None,
        error: str | None,
        elapsed_ms: int,
    ) -> int:
        return await self._insert(
            """
            INSERT INTO snapshots(target_id, fetched_at, http_status, content_hash, text, error, elapsed_ms)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (target_id, iso(utcnow()), http_status, content_hash, text, error, elapsed_ms),
        )

    async def latest_snapshot(
        self, target_id: str, *, with_text: bool = False
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM snapshots WHERE target_id=?"
        if with_text:
            sql += " AND text IS NOT NULL"
        sql += " ORDER BY id DESC LIMIT 1"
        return await self._fetchone(sql, (target_id,))

    # ------------------------------------------------------------------ changes
    async def add_change(
        self,
        target_id: str,
        *,
        from_snapshot_id: int | None,
        to_snapshot_id: int | None,
        added: int,
        removed: int,
        similarity: float,
        summary: str,
    ) -> int:
        return await self._insert(
            """
            INSERT INTO changes(target_id, from_snapshot_id, to_snapshot_id, detected_at,
                                added, removed, similarity, summary)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                from_snapshot_id,
                to_snapshot_id,
                iso(utcnow()),
                added,
                removed,
                similarity,
                summary,
            ),
        )

    async def recent_changes(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._fetchall(
            """
            SELECT c.*, COALESCE(NULLIF(t.name, ''), c.target_id) AS target_name, t.url
            FROM changes c LEFT JOIN targets t ON t.id = c.target_id
            ORDER BY c.id DESC LIMIT ?
            """,
            (limit,),
        )

    # ------------------------------------------------------------ notifications
    async def record_notification(
        self,
        target_id: str,
        *,
        channel: str,
        kind: str,
        ok: bool,
        detail: str = "",
        dedup_key: str | None = None,
    ) -> None:
        await self._write(
            """
            INSERT INTO notifications(target_id, channel, kind, sent_at, ok, detail, dedup_key)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (target_id, channel, kind, iso(utcnow()), int(ok), detail[:2000], dedup_key),
        )
        await self.db.commit()

    async def notification_exists(self, dedup_key: str) -> bool:
        """按审计键查是否发过（审计/外部脚本用；Watcher 刻意不用它做去重门禁）。"""
        row = await self._fetchone(
            "SELECT 1 AS found FROM notifications WHERE dedup_key=? LIMIT 1", (dedup_key,)
        )
        return row is not None

    async def recent_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------------- health
    async def last_ok_at(self) -> str | None:
        row = await self._fetchone("SELECT MAX(last_ok_at) AS last FROM state")
        return row["last"] if row else None

    async def health_age_seconds(self) -> float | None:
        """距离最近一次成功检查过去了多少秒；从未成功过则返回 None。"""
        last = parse_iso(await self.last_ok_at())
        if last is None:
            return None
        return (utcnow() - last).total_seconds()

    async def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("targets", "snapshots", "changes", "notifications"):
            row = await self._fetchone(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            out[table] = int(row["n"]) if row else 0
        return out


__all__ = ["SCHEMA", "Storage"]
