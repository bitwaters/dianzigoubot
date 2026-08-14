"""存储层测试：迁移、优先级写入队列、Repository、备份。"""
import asyncio
import sqlite3

import pytest

from app.core.storage.database import (
    Database,
    PriorityWriter,
    StorageFatalError,
    clear_env_db,
)
from app.core.storage.repository import Repository


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "bot.sqlite"
    yield path
    clear_env_db(path)


async def _open_db(path) -> Database:
    db = Database(path)
    await db.open()
    return db


class TestMigrations:
    async def test_apply_fresh(self, db_path):
        db = await _open_db(db_path)
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        expected = {
            "tokens",
            "pools",
            "candidates",
            "market_bars",
            "security_checks",
            "signals",
            "telegram_updates",
            "telegram_offset",
            "telegram_outbox",
            "telegram_confirmations",
            "strategy_snapshots",
            "strategy_activations",
            "api_usage",
            "discovery_snapshots",
            "wallet_observations",
            "wallet_outcome_labels",
            "wallet_reputation",
            "outcome_snapshots",
            "historical_ranges",
            "backtest_runs",
            "schema_migrations",
        }
        assert expected <= tables
        cursor = await db.conn.execute("SELECT MAX(version) FROM schema_migrations")
        row = await cursor.fetchone()
        assert int(row[0]) >= 1
        await db.close()

    async def test_idempotent_reopen(self, db_path):
        from app.core.storage.database import MIGRATIONS_DIR

        db = await _open_db(db_path)
        await db.close()
        db2 = await _open_db(db_path)
        cursor = await db2.conn.execute("SELECT COUNT(*) FROM schema_migrations")
        row = await cursor.fetchone()
        expected = len(list(MIGRATIONS_DIR.glob("*.sql")))
        assert int(row[0]) == expected
        await db2.close()

    async def test_prisma(self, db_path):
        db = await _open_db(db_path)
        for name, expected in [
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("busy_timeout", 5000),
            ("synchronous", 2),  # FULL
        ]:
            cursor = await db.conn.execute(f"PRAGMA {name}")
            row = await cursor.fetchone()
            assert row[0] == expected, f"PRAGMA {name} = {row[0]}"
        await db.close()


class TestPriorityWriter:
    async def test_priority_order(self, db_path):
        db = await _open_db(db_path)
        writer = PriorityWriter(db)
        writer.start()
        await writer.enqueue(writer.CANDIDATE, _insert_marker(3), (), kind="candidate")
        await writer.enqueue(writer.STRATEGY, _insert_marker(1), (), kind="strategy")
        await writer.enqueue(writer.SIGNAL, _insert_marker(2), (), kind="signal")
        await writer.enqueue(writer.CANDIDATE, _insert_marker(4), (), kind="candidate")
        for _ in range(200):
            if await _markers(db) == [1, 2, 3, 4]:
                break
            await asyncio.sleep(0.01)
        assert await _markers(db) == [1, 2, 3, 4]
        await writer.stop()
        await db.close()

    async def test_candidate_dropped_when_full(self, db_path):
        db = await _open_db(db_path)
        writer = PriorityWriter(db, maxsize=2)
        writer.start()
        await writer.enqueue(writer.CANDIDATE, _insert_marker(1), (), kind="candidate")
        await writer.enqueue(writer.CANDIDATE, _insert_marker(2), (), kind="candidate")
        accepted = await writer.enqueue(
            writer.CANDIDATE, _insert_marker(3), (), kind="candidate"
        )
        assert accepted is False
        await writer.stop()
        await db.close()

    async def test_critical_full_raises(self, db_path):
        db = await _open_db(db_path)
        writer = PriorityWriter(db, maxsize=2)
        writer.start()
        await writer.enqueue(writer.STRATEGY, _insert_marker(1), (), kind="strategy")
        await writer.enqueue(writer.STRATEGY, _insert_marker(2), (), kind="strategy")
        with pytest.raises(StorageFatalError):
            await writer.enqueue(writer.STRATEGY, _insert_marker(3), (), kind="strategy")
        await writer.stop()
        await db.close()

    async def test_backpressure_transitions(self, db_path):
        db = await _open_db(db_path)
        writer = PriorityWriter(db, maxsize=10)
        writer.start()
        # enqueue 内部无 await 调度点，8 次入队之间 worker 不会运行
        for i in range(8):
            await writer.enqueue(
                writer.CANDIDATE, _insert_marker(i), (), kind="candidate"
            )
        assert writer.is_backpressured is True
        # 等待 worker 排空后解除背压
        for _ in range(200):
            if not writer.is_backpressured:
                break
            await asyncio.sleep(0.01)
        assert writer.is_backpressured is False
        await writer.stop()
        await db.close()


def _insert_marker(value: int) -> str:
    return (
        "INSERT INTO telegram_updates (update_id, processed_at) "
        f"VALUES ({value}, 0)"
    )


async def _markers(db: Database) -> list[int]:
    cursor = await db.conn.execute("SELECT update_id FROM telegram_updates ORDER BY update_id")
    return [int(row["update_id"]) for row in await cursor.fetchall()]


class TestRepository:
    async def test_token_pool_upsert(self, db_path):
        db = await _open_db(db_path)
        repo = Repository(db.conn)
        token_id = await repo.upsert_token(
            "solana", "T1", name="Token", symbol="TKN", decimals=6
        )
        again = await repo.upsert_token("solana", "T1", symbol="TKN2")
        assert token_id == again
        cursor = await db.conn.execute("SELECT symbol FROM tokens WHERE id = ?", (token_id,))
        row = await cursor.fetchone()
        assert row["symbol"] == "TKN2"  # COALESCE 不覆盖已有值
        pool_id = await repo.upsert_pool("solana", "P1", token_id, dex="raydium")
        assert pool_id > 0
        assert await repo.get_token_id("solana", "T1") == token_id
        assert await repo.get_token_id("solana", "NOPE") is None
        await db.close()

    async def test_candidate_upsert_and_status_check(self, db_path):
        db = await _open_db(db_path)
        repo = Repository(db.conn)
        await repo.upsert_candidate("solana", "T1", "P1", labels=["hot"], main_label="hot")
        row = await repo.get_candidate("solana", "T1")
        assert row["status"] == "WATCHING"
        assert row["labels"] == '["hot"]'
        await repo.upsert_candidate("solana", "T1", "P2", status="SETUP")
        row = await repo.get_candidate("solana", "T1")
        assert row["pool_address"] == "P2"
        with pytest.raises(ValueError):
            await repo.upsert_candidate("solana", "T1", "P2", status="BOGUS")
        await db.close()

    async def test_signal_lifecycle(self, db_path):
        db = await _open_db(db_path)
        repo = Repository(db.conn)
        await repo.insert_signal(
            signal_id="s1",
            chain="solana",
            token_address="T1",
            pool_address="P1",
            signal_level="BUY",
            total_score="85.5",
            reference_price="0.5",
            signal_generation=1,
            strategy_revision="strategy-1",
            strategy_hash="h",
            security_snapshot={"pass": True},
            decision_snapshot={"score": "85.5"},
            expires_at=1000,
        )
        unpublished = await repo.list_unpublished_signals()
        assert len(unpublished) == 1
        await repo.mark_signal_event_published("s1")
        assert await repo.list_unpublished_signals() == []
        await repo.invalidate_signal("s1", "EXPIRED", invalidated_at=2000)
        row = await repo.get_signal("s1")
        assert row["invalidated_reason"] == "EXPIRED"
        await db.close()

    async def test_expired_scan_and_ws_usage(self, db_path):
        db = await _open_db(db_path)
        repo = Repository(db.conn)
        await repo.insert_signal(
            signal_id="expired1",
            chain="solana",
            token_address="T1",
            pool_address="P1",
            signal_level="BUY",
            total_score="80",
            reference_price="1",
            signal_generation=1,
            strategy_revision="strategy-1",
            strategy_hash="h",
            security_snapshot={},
            decision_snapshot={},
            expires_at=100,
        )
        await repo.insert_signal(
            signal_id="valid1",
            chain="solana",
            token_address="T2",
            pool_address="P2",
            signal_level="BUY",
            total_score="80",
            reference_price="1",
            signal_generation=1,
            strategy_revision="strategy-1",
            strategy_hash="h",
            security_snapshot={},
            decision_snapshot={},
            expires_at=9_999_999_999_999,
        )
        expired = await repo.list_expired_signals(now_ms=1000)
        assert [r["signal_id"] for r in expired] == ["expired1"]
        await repo.invalidate_signal("expired1", "EXPIRED", invalidated_at=1000)
        assert await repo.list_expired_signals(now_ms=1000) == []

        await repo.record_api_usage("WS", "all", charged_responses=42)
        await repo.conn.commit()
        assert await repo.sum_ws_charged_since(0) == 42
        assert await repo.sum_ws_charged_since(10**15) == 0
        await db.close()


class TestBackup:
    async def test_backup_file(self, db_path, tmp_path):
        db = await _open_db(db_path)
        repo = Repository(db.conn)
        await repo.upsert_token("solana", "T1")
        await db.conn.commit()
        target = tmp_path / "backup.sqlite"
        await db.backup(target)
        assert target.exists()
        conn = sqlite3.connect(target)
        assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1
        conn.close()
        await db.close()
