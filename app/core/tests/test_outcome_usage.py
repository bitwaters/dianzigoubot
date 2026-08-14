"""结果追踪与用量控制测试（G9）。"""
import asyncio
from decimal import Decimal

import pytest

from app.core.clients.coingecko import KeyInfo, UsageLedger
from app.core.services.outcome_tracking import HORIZONS, OutcomeTracker
from app.core.services.usage import RestConcurrencyGate, UsageService
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


async def _make_signal(repo, signal_id="s1", created_at=1_000_000, **overrides):
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
        expires_at=9_999_999_999_999,
    )
    defaults.update(overrides)
    await repo.insert_signal(**defaults)
    # 直接改写 created_at 以便触发到期（经 Repository 的连接句柄）
    await repo._conn.execute(
        "UPDATE signals SET created_at = ? WHERE signal_id = ?", (created_at, signal_id)
    )


class FakeCG:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def pools_multi(self, chain, addresses, include_volume_breakdown=False):
        from app.core.clients.coingecko import PoolDetail

        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return [
            PoolDetail(
                address=addresses[0],
                reserve_in_usd=Decimal("50000"),
                base_token_price_usd=Decimal("0.42"),
            )
        ]


class TestOutcomeTracker:
    async def test_due_capture(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo, "s1", created_at=1_000_000)
        cg = FakeCG()
        tracker = OutcomeTracker(repo, cg, RestConcurrencyGate(4))
        now = 1_000_000 + HORIZONS["15m"] * 1000 + 1
        done = await tracker.run_due(now)
        assert done == 1  # 仅 15m 到期
        row = await repo.get_outcome_snapshot("s1", "15m")
        assert row["completeness"] == "COMPLETE"
        assert row["price"] == "0.42"
        assert row["liquidity_usd"] == "50000"
        # 再跑一次不重复
        assert await tracker.run_due(now) == 0
        await db.close()

    async def test_all_horizons_after_24h(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo, "s1", created_at=1_000_000)
        tracker = OutcomeTracker(repo, FakeCG(), RestConcurrencyGate(4))
        now = 1_000_000 + HORIZONS["24h"] * 1000 + 1
        assert await tracker.run_due(now) == 4
        await db.close()

    async def test_data_gap_after_retry(self, db_path):
        db = await _open(db_path)
        repo = Repository(db.conn)
        await _make_signal(repo, "s1", created_at=1_000_000)
        tracker = OutcomeTracker(repo, FakeCG(fail=True), RestConcurrencyGate(4))
        now = 1_000_000 + HORIZONS["15m"] * 1000 + 1
        await tracker.run_due(now)
        row = await repo.get_outcome_snapshot("s1", "15m")
        assert row["completeness"] == "DATA_GAP"
        await db.close()


class TestRestGate:
    async def test_critical_reserved(self):
        gate = RestConcurrencyGate(total=3, reserved_for_critical=1)
        assert gate.normal_slots == 2
        # 占满普通槽位后，普通请求等待，关键请求仍可进入
        async with gate.acquire(False):
            async with gate.acquire(False):
                acquired = asyncio.Event()

                async def normal_try():
                    async with gate.acquire(False):
                        pass

                task = asyncio.create_task(normal_try())
                await asyncio.sleep(0.02)
                assert not task.done()  # 普通槽位已满
                async with gate.acquire(critical=True):
                    pass  # 关键事务可用保留槽位
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class TestUsageService:
    async def test_budget_lines(self):
        ledger = UsageLedger()
        ledger.record_rest("pools-megafilter")

        async def provider():
            return KeyInfo(
                plan="Analyst",
                current_remaining_monthly_calls=497000,
                rate_limit_request_per_minute=500,
            )

        service = UsageService(
            key_provider=provider,
            rest_ledger=ledger,
            alert_threshold=1000,
        )
        lines = await service.budget_lines()
        assert any("Analyst" in line for line in lines)
        assert any("pools-megafilter" in line for line in lines)

    async def test_alert_threshold(self):
        async def provider():
            return KeyInfo(current_remaining_monthly_calls=500)

        service = UsageService(
            key_provider=provider,
            rest_ledger=UsageLedger(),
            alert_threshold=1000,
        )
        lines = await service.budget_lines()
        assert any("告警" in line for line in lines)

    async def test_key_failure(self):
        async def fail():
            raise RuntimeError("down")

        service = UsageService(key_provider=fail, rest_ledger=UsageLedger())
        lines = await service.budget_lines()
        assert lines[0].startswith("/key 查询失败")
