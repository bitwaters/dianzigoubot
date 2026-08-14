"""插件总线测试：事件投递、至少一次、分发超时、熔断、命令冲突、通知、加载隔离。"""
import asyncio
import json
from pathlib import Path

import pytest

from app.core.bus.commands import CommandConflictError, CommandRegistry
from app.core.bus.events import (
    EventBus,
    build_signal_created_event,
    build_signal_invalidated_event,
)
from app.core.bus.loader import load_plugins
from app.core.bus.notify import NotifyAPI
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
        security_snapshot={"pass": True},
        decision_snapshot={},
        expires_at=9_999_999_999,
    )
    defaults.update(overrides)
    await repo.insert_signal(**defaults)


class TestEventPayload:
    async def test_schema_v1_fields(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo)
        row = await repo.get_signal("s1")
        event = build_signal_created_event(row)
        required = {
            "schema_version", "event_id", "signal_id", "chain", "token_address",
            "pool_address", "signal_level", "total_score", "reference_price",
            "security_snapshot", "expires_at", "strategy_revision", "telegram_status",
        }
        assert required <= set(event)
        assert event["schema_version"] == "1"

        await repo.invalidate_signal("s1", "EXPIRED", invalidated_at=2000)
        row = await repo.get_signal("s1")
        invalidated = build_signal_invalidated_event(row)
        assert invalidated["reason"] == "EXPIRED"
        await db.close()


class TestEventBus:
    async def test_publish_and_deliver(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo)
        bus = EventBus(repo)
        bus.register_plugin("p1")
        got = []

        async def handler(event):
            got.append(event["signal_id"])

        bus.subscribe("p1", "signal_created", handler)
        bus.start()
        assert await bus.publish_signal_created("s1") is True
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.01)
        assert got == ["s1"]
        # 已发布标记
        row = await repo.get_signal("s1")
        assert row["event_published"] == 1
        await bus.stop()
        await db.close()

    async def test_resend_unpublished_on_restart(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo)
        bus = EventBus(repo)
        bus.register_plugin("p1")
        got = []

        async def handler(event):
            got.append(event["signal_id"])

        bus.subscribe("p1", "signal_created", handler)
        bus.start()
        # 模拟重启：未发布信号补发
        count = await bus.resend_unpublished()
        assert count == 1
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.01)
        assert got == ["s1"]
        # 再补发不应重复
        assert await bus.resend_unpublished() == 0
        await bus.stop()
        await db.close()

    async def test_slow_handler_does_not_block(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo)
        bus = EventBus(repo, dispatch_timeout_seconds=0.05, failure_threshold=3)
        bus.register_plugin("slow")
        bus.register_plugin("fast")
        got = []

        async def slow_handler(event):
            await asyncio.sleep(1)

        async def fast_handler(event):
            got.append(event["signal_id"])

        bus.subscribe("slow", "signal_created", slow_handler)
        bus.subscribe("fast", "signal_created", fast_handler)
        bus.start()
        await bus.publish_signal_created("s1")
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.01)
        assert got == ["s1"]  # fast 插件未被 slow 阻塞
        await bus.stop()
        await db.close()

    async def test_circuit_breaker(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        bus = EventBus(repo, dispatch_timeout_seconds=0.05, failure_threshold=3)
        state = bus.register_plugin("p1")
        calls = []

        async def failing(event):
            calls.append(1)
            raise RuntimeError("boom")

        bus.subscribe("p1", "signal_created", failing)
        bus.start()
        for i in range(4):
            await _make_signal(repo, signal_id=f"s{i}", token_address=f"T{i}")
            await bus.publish_signal_created(f"s{i}")
        for _ in range(100):
            if state.tripped:
                break
            await asyncio.sleep(0.01)
        assert state.tripped is True
        assert len(calls) == 3  # 熔断后不再分发
        await bus.stop()
        await db.close()

    async def test_sync_handler_rejected(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        bus = EventBus(repo)
        bus.register_plugin("p1")

        def sync_handler(event):  # noqa: 非 async
            return None

        with pytest.raises(ValueError, match="async"):
            bus.subscribe("p1", "signal_created", sync_handler)
        await db.close()


class TestCommands:
    async def test_conflict_with_core(self):
        registry = CommandRegistry()

        async def handler(msg):
            pass

        with pytest.raises(CommandConflictError, match="核心命令"):
            registry.register("trade", "/status", handler)
        # 普通插件命令可以注册
        registry.register("trade", "/positions", handler, requires_confirmation=True)

    async def test_plugin_conflict(self):
        registry = CommandRegistry()

        async def handler(msg):
            pass

        registry.register("trade", "/positions", handler)
        with pytest.raises(CommandConflictError, match="已被"):
            registry.register("swing", "/positions", handler)

    async def test_unregister(self):
        registry = CommandRegistry()

        async def handler(msg):
            pass

        registry.register("trade", "/positions", handler)
        assert registry.get("/positions")["requires_confirmation"] is False
        registry.unregister_plugin("trade")
        assert registry.get("/positions") is None


class TestNotify:
    async def test_send_to_outbox(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        api = NotifyAPI(repo, target_resolver={"admin": 123, "channel": -100})
        key = await api.send("测试通知", target="admin", priority=2)
        assert key.startswith("n:")
        assert await api.status(key) == "PENDING"
        rows = await repo.list_outbox_pending()
        assert len(rows) == 1
        assert rows[0]["chat_target"] == "123"  # 已解析为数字 chat_id
        await db.close()

    async def test_unknown_target_rejected(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        api = NotifyAPI(repo, target_resolver={"admin": 123})
        with pytest.raises(ValueError, match="未知通知目标"):
            await api.send("x", target="channel")
        assert await repo.list_outbox_pending() == []  # 拒绝时不写 outbox
        await db.close()


class TestLoader:
    async def test_load_ok_and_bad_isolated(self, db_path, tmp_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        bus = EventBus(repo)
        notify = NotifyAPI(repo)
        commands = CommandRegistry()

        plugins_dir = tmp_path / "plugins"
        good = plugins_dir / "good"
        good.mkdir(parents=True)
        (good / "plugin.yaml").write_text(
            "name: good\nversion: 0.1.0\nsdk_version: '1'\nsupported_event_schema_versions: ['1']\n",
            encoding="utf-8",
        )
        (good / "register.py").write_text(
            "async def register(app):\n"
            "    app.loaded = True\n",
            encoding="utf-8",
        )
        bad = plugins_dir / "bad"
        bad.mkdir(parents=True)
        (bad / "plugin.yaml").write_text(
            "name: bad\nversion: 0.1.0\nsdk_version: '1'\nsupported_event_schema_versions: ['1']\n",
            encoding="utf-8",
        )
        (bad / "register.py").write_text(
            "async def register(app):\n"
            "    raise RuntimeError('bad plugin')\n",
            encoding="utf-8",
        )

        loaded = await load_plugins(
            enabled=["good", "bad"],
            events=bus,
            notify=notify,
            commands=commands,
            repo=repo,
            base_dir=plugins_dir,
        )
        assert [c.name for c in loaded] == ["good"]
        assert "good" in bus._plugins
        assert "bad" not in bus._plugins
        await db.close()

    async def test_wrong_sdk_rejected(self, db_path, tmp_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        plugins_dir = tmp_path / "plugins"
        p = plugins_dir / "old"
        p.mkdir(parents=True)
        (p / "plugin.yaml").write_text(
            "name: old\nversion: 0.1.0\nsdk_version: '0'\nsupported_event_schema_versions: ['1']\n",
            encoding="utf-8",
        )
        (p / "register.py").write_text("async def register(app):\n    pass\n", encoding="utf-8")
        loaded = await load_plugins(
            enabled=["old"], events=EventBus(repo), notify=NotifyAPI(repo),
            commands=CommandRegistry(), repo=repo, base_dir=plugins_dir,
        )
        assert loaded == []
        await db.close()
