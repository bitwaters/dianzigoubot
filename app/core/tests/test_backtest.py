"""回测与策略激活测试（G10）。"""
from decimal import Decimal

import pytest

from app.core.backtest import (
    FillResult,
    ReplayResult,
    compute_report,
    entry_fill,
    fetch_backfill,
    plan_backfill,
    replay_signal,
    run_id_for,
)
from app.core.config import load_strategy_file
from app.core.services.strategy import CandleBar

STRATEGY = load_strategy_file(
    __import__("pathlib").Path(__file__).parent / "fixtures" / "strategy_valid.yaml"
)


def _bars(prices: list[tuple[float, float, float]], start_ms: int = 1_000_000) -> list[CandleBar]:
    """[(high, low, open)] 生成 1m K 线（60 秒间隔）。"""
    bars = []
    ts = start_ms
    for high, low, open_ in prices:
        bars.append(
            CandleBar(
                open=Decimal(str(open_)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(open_)),
                volume=Decimal("1000"),
                open_ts_ms=ts,
            )
        )
        ts += 60_000
    return bars


class TestBackfillPlan:
    def test_single_piece(self):
        plans = plan_backfill(
            chain="solana", pool_address="P", token_address="T",
            start_ms=1_000_000_000, end_ms=2_000_000_000,
        )
        assert len(plans) == 1
        assert plans[0].range_start_sec == 1_000_000
        assert plans[0].range_end_sec == 2_000_000

    def test_split_by_6_months(self):
        start_ms = 0
        end_ms = 1000 * 400 * 24 * 3600 * 1000  # 约 400 天
        plans = plan_backfill(
            chain="solana", pool_address="P", token_address="T",
            start_ms=start_ms, end_ms=end_ms,
        )
        assert len(plans) > 1
        for prev, cur in zip(plans, plans[1:]):
            assert prev.range_end_sec == cur.range_start_sec


class TestFetchBackfill:
    async def test_pagination(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def pool_ohlcv(self, chain, pool_address, **kwargs):
                from app.core.clients.coingecko import OhlcvBar

                self.calls.append(kwargs["before_timestamp"])
                before = kwargs["before_timestamp"]
                # 模拟两页：每页 3 条，向前递减
                if before > 300:
                    return [
                        OhlcvBar(timestamp_ms=t * 1000, open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=Decimal(1))
                        for t in (400, 300, 200)
                    ]
                return [
                    OhlcvBar(timestamp_ms=t * 1000, open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=Decimal(1))
                    for t in (100, 0)
                ]

        from app.core.backtest import BackfillRequest

        bars = await fetch_backfill(
            FakeClient(),
            BackfillRequest("solana", "P", "T", range_start_sec=0, range_end_sec=500),
        )
        timestamps = sorted(b.timestamp_ms // 1000 for b in bars)
        assert timestamps == [0, 100, 200, 300, 400]
        assert len(bars) == 5


class TestEntryFill:
    def _cfg(self):
        return STRATEGY.solana.backtest_model

    def test_filled_next_bar(self):
        bars = _bars([(1.05, 0.95, 1.02), (1.10, 1.00, 1.02)])
        result = entry_fill(
            signal_price=Decimal("1.0"),
            signal_minute_start_ms=1_000_000,
            bars=bars,
            reserve_usd=Decimal("50000"),
            order_usd=Decimal("100"),
            config=self._cfg(),
        )
        assert result.status == "FILLED"
        assert result.entry_price > Decimal("1.02")  # 含滑点与冲击

    def test_deviation_exceeded(self):
        # 信号分钟后一根 K 线开盘 1.9，偏离 90% > 3%
        bars = _bars([(1.02, 0.99, 1.0), (2.0, 1.8, 1.9)])
        result = entry_fill(
            signal_price=Decimal("1.0"),
            signal_minute_start_ms=1_000_000,
            bars=bars,
            reserve_usd=Decimal("50000"),
            order_usd=Decimal("100"),
            config=self._cfg(),
        )
        assert result.status == "NOT_FILLED"
        assert result.reason == "入场价格偏离超限"

    def test_data_gap(self):
        result = entry_fill(
            signal_price=Decimal("1.0"),
            signal_minute_start_ms=1_000_000,
            bars=[],
            reserve_usd=Decimal("50000"),
            order_usd=Decimal("100"),
            config=self._cfg(),
        )
        assert result.status == "DATA_GAP"


class TestReplay:
    def _cfg(self):
        return STRATEGY.solana.backtest_model

    def test_stop_loss(self):
        bars = _bars([(1.02, 0.99, 1.0), (1.01, 1.00, 1.005), (1.0, 0.75, 0.95)])
        result = replay_signal(
            signal_price=Decimal("1.0"),
            signal_minute_start_ms=1_000_000,
            bars=bars,
            reserve_usd=Decimal("50000"),
            order_usd=Decimal("100"),
            config=self._cfg(),
        )
        assert result.fill.status == "FILLED"
        assert result.exit is not None
        assert result.exit.exit_reason == "stop_loss"
        assert result.realized_pnl_pct is not None
        assert result.realized_pnl_pct < 0

    def test_take_profit_leg(self):
        # 第三根高点 +69%（档1 触发 50% 卖出 50%）；低点不触发追踪止损，保持剩余 50%
        bars = _bars([(1.02, 0.99, 1.0), (1.01, 1.00, 1.005), (1.7, 1.5, 1.55)])
        result = replay_signal(
            signal_price=Decimal("1.0"),
            signal_minute_start_ms=1_000_000,
            bars=bars,
            reserve_usd=Decimal("50000"),
            order_usd=Decimal("100"),
            config=self._cfg(),
        )
        assert result.fill.status == "FILLED"
        assert result.exit is not None
        assert result.exit.exited is False  # 只卖了一半，未退出
        assert result.exit.remaining_ratio == Decimal("0.5")
        assert result.realized_pnl_pct is None

    def test_same_bar_conflict_protective(self):
        # 同根 K 线高点触发止盈、低点触发止损 → 保护性全仓退出
        bars = _bars([(1.02, 0.99, 1.0), (1.01, 1.00, 1.005), (1.7, 0.7, 1.0)])
        result = replay_signal(
            signal_price=Decimal("1.0"),
            signal_minute_start_ms=1_000_000,
            bars=bars,
            reserve_usd=Decimal("50000"),
            order_usd=Decimal("100"),
            config=self._cfg(),
        )
        assert result.exit is not None
        assert result.exit.exit_reason == "stop_loss"


class TestReport:
    def test_report_metrics(self):
        replays = [
            ReplayResult(fill=FillResult("FILLED", Decimal("1.0")), realized_pnl_pct=Decimal("10")),
            ReplayResult(fill=FillResult("FILLED", Decimal("1.0")), realized_pnl_pct=Decimal("-5")),
            ReplayResult(fill=FillResult("NOT_FILLED")),
            ReplayResult(fill=FillResult("DATA_GAP"), data_gap=True),
        ]
        report = compute_report(
            strategy_revision="strategy-1",
            strategy_hash="h",
            dataset_hash="d",
            replays=replays,
            candidate_count=100,
            signal_count=4,
        )
        assert report.signal_count == 4
        assert report.filled_count == 2
        assert report.not_filled_count == 1
        assert report.data_gap_count == 1
        assert report.net_expectancy_pct == Decimal("2.5")
        assert report.profit_factor == Decimal(2)
        assert report.replay_coverage_pct == Decimal(75)

    def test_run_id_deterministic(self):
        assert run_id_for("h", "d") == run_id_for("h", "d")
        assert run_id_for("h", "d") != run_id_for("h2", "d")


class TestStrategyActivation:
    @pytest.fixture
    def db_path(self, tmp_path):
        from app.core.storage.database import clear_env_db

        path = tmp_path / "bot.sqlite"
        yield path
        clear_env_db(path)

    async def test_activate_and_rollback(self, db_path):
        from app.core.config import load_strategy_file
        from app.core.services.strategy import StrategyActivationService
        from app.core.storage.database import Database
        from app.core.storage.repository import Repository

        db = Database(db_path)
        await db.open()
        service = StrategyActivationService(Repository(db.conn))
        assert await service.current_active() is None

        baseline = load_strategy_file(
            __import__("pathlib").Path(__file__).parent / "fixtures" / "strategy_valid.yaml"
        )
        # 基准初始化：parent=null 允许
        aid = await service.activate(baseline, admin_id=7)
        assert aid
        current = await service.current_active()
        assert current["revision"] == "strategy-1"

        # 普通激活：parent 必须等于当前 ACTIVE
        v2 = baseline.model_copy(
            update={"revision": "strategy-2", "parent_revision": "strategy-1"}
        )
        await service.activate(v2, admin_id=7)
        assert (await service.current_active())["revision"] == "strategy-2"

        # revision 复用拒绝
        with pytest.raises(ValueError, match="已使用"):
            await service.activate(v2, admin_id=7)

        # 回退：复用原 revision/hash
        await service.rollback("strategy-1", admin_id=7)
        current = await service.current_active()
        assert current["revision"] == "strategy-1"
        assert current["strategy_hash"] == baseline.strategy_hash
        await db.close()

    async def test_parent_mismatch_rejected(self, db_path):
        from app.core.config import load_strategy_file
        from app.core.services.strategy import StrategyActivationService
        from app.core.storage.database import Database
        from app.core.storage.repository import Repository

        db = Database(db_path)
        await db.open()
        service = StrategyActivationService(Repository(db.conn))
        baseline = load_strategy_file(
            __import__("pathlib").Path(__file__).parent / "fixtures" / "strategy_valid.yaml"
        )
        await service.activate(baseline, admin_id=7)
        bad = baseline.model_copy(
            update={"revision": "strategy-3", "parent_revision": "strategy-99"}
        )
        with pytest.raises(ValueError, match="不一致"):
            await service.activate(bad, admin_id=7)
        await db.close()
