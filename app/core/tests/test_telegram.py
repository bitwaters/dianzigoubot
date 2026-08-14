"""Telegram 模块测试：offset 持久化、outbox 状态机、一次性确认、管理命令。"""
import asyncio
import json

import pytest

from app.core.clients.telegram import (
    ConfirmationManager,
    OutboxSender,
    UpdatePoller,
)
from app.core.services.admin import AdminService, build_signal_message
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


async def _make_signal(repo, signal_id="s1", **overrides):
    defaults = dict(
        signal_id=signal_id,
        chain="solana",
        token_address="T1",
        pool_address="P1",
        signal_level="BUY",
        total_score="85.5",
        reference_price="1.0",
        signal_generation=1,
        strategy_revision="strategy-1",
        strategy_hash="h",
        security_snapshot={"pass": True, "trade_allowed": True},
        decision_snapshot={},
        expires_at=9_999_999_999_999,
    )
    defaults.update(overrides)
    await repo.insert_signal(**defaults)


class TestUpdatePoller:
    async def test_offset_and_dedup(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        queue = asyncio.Queue()
        updates = [
            [{"update_id": 1, "message": {"text": "/status"}}],
            [{"update_id": 2, "message": {"text": "/sol"}}],
            [{"update_id": 2, "message": {"text": "/sol"}}],  # 重复
            [],
        ]

        async def fetch(offset, timeout):
            return await queue.get()

        handled = []

        async def on_update(update):
            handled.append(update["update_id"])

        poller = UpdatePoller(repo, fetch, poll_interval=0.01)
        poller.on_update = on_update
        for batch in updates:
            await queue.put(batch)
        await poller.start()
        for _ in range(200):
            if len(handled) >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await poller.stop()
        assert handled == [1, 2]  # 重复 update 未重放
        assert await repo.get_committed_offset() == 3
        await db.close()

    async def test_update_result_persisted(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        queue = asyncio.Queue()

        async def fetch(offset, timeout):
            return await queue.get()

        async def on_update(update):
            pass

        poller = UpdatePoller(repo, fetch, poll_interval=0.01)
        poller.on_update = on_update
        await queue.put([{"update_id": 1, "message": {"text": "/status"}}])
        await queue.put([])
        await poller.start()
        for _ in range(100):
            row = await repo.get_update(1)
            if row and "ok" in row["result"]:
                break
            await asyncio.sleep(0.01)
        await poller.stop()
        row = await repo.get_update(1)
        assert row is not None
        assert '"handled": "ok"' in row["result"] or "handled" in row["result"]
        assert await repo.get_committed_offset() == 2
        await db.close()

    async def test_fetch_error_recovered(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        calls = []

        async def fetch(offset, timeout):
            calls.append(offset)
            if len(calls) == 1:
                raise RuntimeError("network")
            return []

        poller = UpdatePoller(repo, fetch, poll_interval=0.01)
        await poller.start()
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        await poller.stop()
        assert len(calls) >= 2
        await db.close()


class TestOutbox:
    async def test_sent(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await repo.insert_outbox(
            delivery_key="k1", kind="NOTIFY", parent_id=None,
            chat_target="123", text="hello",
        )
        sent = []

        async def send(chat_id, text, reply_markup=None):
            sent.append((chat_id, text))
            return "mid-1"

        sender = OutboxSender(repo, send)
        await sender.start()
        for _ in range(100):
            row = await repo.get_outbox("k1")
            if row["status"] == "SENT":
                break
            await asyncio.sleep(0.01)
        await sender.stop()
        assert sent == [(123, "hello")]
        assert (await repo.get_outbox("k1"))["telegram_message_id"] == "mid-1"
        await db.close()

    async def test_fail_retry_same_key(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await repo.insert_outbox(
            delivery_key="k1", kind="NOTIFY", parent_id=None,
            chat_target="123", text="hello",
        )
        attempts = []

        async def send(chat_id, text, reply_markup=None):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("boom")
            return "mid-2"

        sender = OutboxSender(repo, send, retry_base_seconds=0.01, retry_max_seconds=0.05)
        await sender.start()
        for _ in range(300):
            row = await repo.get_outbox("k1")
            if row["status"] == "SENT":
                break
            await asyncio.sleep(0.01)
        await sender.stop()
        row = await repo.get_outbox("k1")
        assert row["status"] == "SENT"
        assert row["retry_count"] == 1  # 同一 delivery_key 重试，无新事件
        assert await repo.list_outbox_pending() == []
        await db.close()

    async def test_expired_signal_marked(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo, signal_id="s1", expires_at=1)  # 已过期
        await repo.insert_outbox(
            delivery_key="k1", kind="BUY_SIGNAL", parent_id="s1",
            chat_target="123", text="原始内容",
        )
        sent = []

        async def send(chat_id, text, reply_markup=None):
            sent.append(text)

        sender = OutboxSender(repo, send)
        await sender.start()
        for _ in range(100):
            row = await repo.get_outbox("k1")
            if row["status"] == "SENT":
                break
            await asyncio.sleep(0.01)
        await sender.stop()
        assert "已过期，仅通知" in sent[0]
        await db.close()

    async def test_sending_recovery(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await repo.insert_outbox(
            delivery_key="k1", kind="NOTIFY", parent_id=None,
            chat_target="123", text="hello",
        )
        await repo.update_outbox_status("k1", "SENDING")
        # 启动恢复：SENDING → DELIVERY_UNKNOWN，不自动补发
        sender = OutboxSender(repo, lambda c, t, m=None: "mid")
        await sender.start()
        await asyncio.sleep(0.05)
        await sender.stop()
        row = await repo.get_outbox("k1")
        assert row["status"] == "DELIVERY_UNKNOWN"
        await db.close()


class TestConfirmation:
    async def test_consume_flow(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        mgr = ConfirmationManager(repo)
        nonce = await mgr.create(
            admin_id=7, chat_id=100, update_id=42,
            command="/closeall", args="", payload={"action": "closeall"},
        )
        assert await mgr.try_consume(nonce, admin_id=7, chat_id=100) == {"action": "closeall"}
        assert await mgr.try_consume(nonce, admin_id=7, chat_id=100) is None  # 一次性
        await db.close()

    async def test_wrong_admin(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        mgr = ConfirmationManager(repo)
        nonce = await mgr.create(
            admin_id=7, chat_id=100, update_id=42,
            command="/x", args="", payload={},
        )
        assert await mgr.try_consume(nonce, admin_id=8, chat_id=100) is None
        assert await mgr.try_consume(nonce, admin_id=7, chat_id=999) is None
        await db.close()

    async def test_expired(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        mgr = ConfirmationManager(repo)
        nonce = await mgr.create(
            admin_id=7, chat_id=100, update_id=42,
            command="/x", args="", payload={}, ttl_seconds=-1,  # 立即过期
        )
        assert await mgr.try_consume(nonce, admin_id=7, chat_id=100) is None
        await db.close()


class TestCoreActions:
    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "bot.sqlite"
        yield path
        clear_env_db(path)

    async def test_strategy_status_and_confirm_routing(self, db_path):
        from app.core.main import CoreActions
        from app.core.config import load_strategy_file
        from app.core.services.strategy import StrategyActivationService

        db = await _open(db_path)
        repo = Repository(db.conn)
        activation = StrategyActivationService(repo)
        baseline = load_strategy_file(
            __import__("pathlib").Path(__file__).parent / "fixtures" / "strategy_valid.yaml"
        )
        await activation.activate(baseline, admin_id=7)

        class SettingsStub:
            telegram_admin_id = 7

        core = CoreActions(repo, activation, SettingsStub(), None, None)

        # 确认路由：activate/rollback/telegram retry 需要确认
        assert core.confirm_payload("/strategy", "activate")["action"] == "strategy_activate"
        assert core.confirm_payload("/strategy", "rollback strategy-1")["action"] == "strategy_rollback"
        assert core.confirm_payload("/telegram", "retry k1")["action"] == "telegram_retry"
        assert core.confirm_payload("/strategy", "status") is None
        assert core.confirm_payload("/status", "") is None

        status = await core.strategy_command("status")
        assert "strategy-4" in status
        usage = await core.strategy_command("backtest")
        assert "历史信号" in usage
        await db.close()

    async def test_telegram_retry_requeue(self, db_path):
        from app.core.main import CoreActions

        db = await _open(db_path)
        repo = Repository(db.conn)
        await repo.insert_outbox(
            delivery_key="k1", kind="NOTIFY", parent_id=None,
            chat_target="123", text="hello",
        )
        await repo.update_outbox_status("k1", "FAILED")

        class SettingsStub:
            telegram_admin_id = 7

        class ActivationStub:
            async def current_active(self):
                return None

        core = CoreActions(repo, ActivationStub(), SettingsStub(), None, None)
        result = await core._do_telegram_retry("retry k1")
        assert "已重新入队" in result
        row = await repo.get_outbox("k1")
        assert row["status"] == "PENDING"
        assert row["retry_count"] == 1
        # 已投递成功的消息拒绝补发
        await repo.update_outbox_status("k1", "SENT")
        result = await core._do_telegram_retry("retry k1")
        assert "无需补发" in result
        await db.close()


class TestAdminCommands:
    async def test_pause_resume_status(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        admin = AdminService(repo)
        assert "已暂停" in await admin.handle("/pause", "")
        assert admin.paused is True
        assert "运行中" in await admin.handle("/status", "")
        assert "已恢复" in await admin.handle("/resume", "")
        await db.close()

    async def test_watchlist_and_signals(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await repo.upsert_candidate("solana", "T1", "P1", status="SETUP")
        await _make_signal(repo, "s1")
        admin = AdminService(repo)
        assert "T1" in await admin.handle("/watchlist", "")
        assert "s1" not in await admin.handle("/signals", "")  # 信号列表按地址显示
        assert "T1" in await admin.handle("/signals", "")
        assert "solana 候选: 1" in await admin.handle("/sol", "")
        await db.close()

    async def test_signal_message_builder(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo, "s1")
        row = await repo.get_signal("s1")
        text = build_signal_message(row)
        assert "买入信号" in text
        assert "SOLANA" in text
        assert "85.5" in text
        await db.close()

    async def test_unknown_command(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        admin = AdminService(repo)
        assert "未知命令" in await admin.handle("/bogus", "")
        await db.close()
