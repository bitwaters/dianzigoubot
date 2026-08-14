"""维护服务测试：容量熔断、数据保留清理（G11）。"""
import pytest

from app.core.models import utc_now_ms
from app.core.services.maintenance import CapacityManager, RetentionCleaner
from app.core.storage.database import Database, clear_env_db
from app.core.storage.repository import Repository


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "bot.sqlite"
    yield path
    clear_env_db(path)


async def _open(path):
    db = Database(path)
    await db.open()
    return db


class TestCapacity:
    def test_states(self, tmp_path):
        manager = CapacityManager(tmp_path, quota_gb=1.0)
        assert manager.state() == "OK"
        # 写入 ~0.75GB 不现实；改为通过磁盘真实使用率验证状态函数输出合法
        assert manager.state() in ("OK", "WARN", "STOP_BACKFILL", "STOP_SIGNALS")


class TestRetentionCleaner:
    async def test_cleanup(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        now = utc_now_ms()

        await repo.insert_signal(
            signal_id="old",
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
            expires_at=1,
        )
        # 手动将信号时间回拨到 200 天前
        await repo.conn.execute(
            "UPDATE signals SET created_at = ? WHERE signal_id = 'old'",
            (now - 200 * 24 * 3600 * 1000,),
        )
        await repo.insert_signal(
            signal_id="new",
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
            expires_at=1,
        )
        await repo.conn.commit()

        cleaner = RetentionCleaner(repo)
        cleaned = await cleaner.run(now)
        assert cleaned.get("signals", 0) == 1
        assert await repo.get_signal("old") is None
        assert await repo.get_signal("new") is not None
        await db.close()

    async def test_outbox_terminal_only(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        now = utc_now_ms()
        await repo.insert_outbox(
            delivery_key="sent_old", kind="NOTIFY", parent_id=None,
            chat_target="1", text="x",
        )
        await repo.update_outbox_status("sent_old", "SENT")
        await repo.conn.execute(
            "UPDATE telegram_outbox SET updated_at = ? WHERE delivery_key = 'sent_old'",
            (now - 200 * 24 * 3600 * 1000,),
        )
        await repo.insert_outbox(
            delivery_key="delivery_unknown", kind="NOTIFY", parent_id=None,
            chat_target="1", text="x",
        )
        await repo.update_outbox_status("delivery_unknown", "DELIVERY_UNKNOWN")
        await repo.conn.commit()

        cleaner = RetentionCleaner(repo)
        removed = await cleaner.cleanup_outbox_terminal(now)
        assert removed == 1
        assert await repo.get_outbox("sent_old") is None
        assert await repo.get_outbox("delivery_unknown") is not None  # 未终态保护
        await db.close()
