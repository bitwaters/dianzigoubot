"""SQLite 连接、迁移与优先级写入队列（总控文档第 9.1、9.2 节）。

- 单连接串行：启动时执行 PRAGMA 与未应用迁移。
- PriorityWriter：策略事务(0) > 信号与安全快照(1) > 候选行情(2)。
  队列 80% 触发背压（暂停发现/订阅/新信号），50% 以下恢复；
  队列满时只允许丢弃候选行情，关键事务无法入队立即进入故障状态。
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.core.models import utc_now_ms

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

QUEUE_MAXSIZE = 5000
BACKPRESSURE_PAUSE_PCT = 0.8
BACKPRESSURE_RESUME_PCT = 0.5
BATCH_SIZE = 100


class StorageFatalError(RuntimeError):
    """关键写入无法入队时的故障状态（总控文档第 9.2 节）。"""


@dataclass(frozen=True, order=True)
class _WriteItem:
    priority: int
    seq: int
    sql: str
    params: tuple
    kind: str


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.execute("PRAGMA synchronous = FULL")
        await self.apply_pending_migrations()
        return self

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database 未打开")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def apply_pending_migrations(self) -> None:
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at INTEGER NOT NULL
               )"""
        )
        await self.conn.commit()
        cursor = await self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        row = await cursor.fetchone()
        applied_max = int(row[0])

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for file in files:
            version = int(file.name.split("_", 1)[0])
            if version <= applied_max:
                continue
            sql = file.read_text(encoding="utf-8")
            await self.conn.execute("BEGIN")
            try:
                await self.conn.executescript(sql)
                await self.conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (version, file.name, utc_now_ms()),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
            log.info("迁移已应用: %s", file.name)

    async def backup(self, target: Path | str) -> None:
        """使用 SQLite Backup API 生成一致性备份。"""
        target = Path(target)
        async with aiosqlite.connect(target) as dest:
            await self.conn.backup(dest)
        log.info("数据库备份完成: %s", target)


class PriorityWriter:
    """进程内优先级写入队列（总控文档第 9.2 节）。"""

    STRATEGY = 0
    SIGNAL = 1
    CANDIDATE = 2

    def __init__(self, db: Database, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._db = db
        self._queue: asyncio.PriorityQueue[_WriteItem] = asyncio.PriorityQueue(
            maxsize=maxsize
        )
        self._seq = 0
        self._worker_task: asyncio.Task | None = None
        self._paused = False
        self._kind = {0: "strategy", 1: "signal", 2: "candidate"}
        self.maxsize = maxsize

    @property
    def is_backpressured(self) -> bool:
        return self._paused

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker(), name="storage_writer")

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def enqueue(
        self, priority: int, sql: str, params: tuple, *, kind: str
    ) -> bool:
        """入队一条写入。

        - 候选行情：队列满时丢弃（返回 False）。
        - 关键事务（strategy/signal）：队列满时抛 StorageFatalError。
        """
        self._seq += 1
        item = _WriteItem(priority, self._seq, sql, params, kind)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            if priority >= self.CANDIDATE:
                return False
            raise StorageFatalError(
                f"关键写入队列已满，无法入队: kind={kind}, sql={sql[:80]!r}"
            )
        self._update_backpressure()
        return True

    async def _worker(self) -> None:
        batch: list[_WriteItem] = []
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                raise
            batch.append(item)
            drain = not self._queue.empty()
            while drain and len(batch) < BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    drain = False
            await self._flush(batch)
            batch.clear()
            self._update_backpressure()

    async def _flush(self, items: list[_WriteItem]) -> None:
        try:
            await self._db.conn.execute("BEGIN")
            for item in items:
                await self._db.conn.execute(item.sql, item.params)
            await self._db.conn.commit()
        except Exception:
            await self._db.conn.rollback()
            log.exception("存储批次写入失败，批次大小 %d", len(items))
            raise

    def _update_backpressure(self) -> None:
        ratio = self._queue.qsize() / self.maxsize
        if not self._paused and ratio >= BACKPRESSURE_PAUSE_PCT:
            self._paused = True
            log.warning("写入队列达到 80%%，进入背压：暂停发现/订阅/新信号")
        elif self._paused and ratio < BACKPRESSURE_RESUME_PCT:
            self._paused = False
            log.info("写入队列回落至 50%% 以下，解除背压")


def clear_env_db(path: Path) -> None:
    """测试辅助：删除数据库及 WAL 文件。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            os.remove(p)
