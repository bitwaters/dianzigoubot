"""评分、防追高、聪明钱纯函数测试（G6，对应总控文档第 13.1 节）。"""
from decimal import Decimal

import pytest

from app.core.config import load_strategy_file
from app.core.services.strategy import (
    CandleBar,
    CandidateRuntime,
    CandidateSnapshot,
    PoolSnapshot,
    SmartMoneyEval,
    SmartMoneySnapshot,
    TxBucket,
    anti_chase_check,
    compute_features,
    eval_smart_money,
    evaluate_candidate,
    score,
    signal_level,
)

STRATEGY = load_strategy_file(
    __import__("pathlib").Path(__file__).parent / "fixtures" / "strategy_valid.yaml"
)


def _bars(n: int, *, start_price: str = "1.0", step: str = "0.01") -> list[CandleBar]:
    bars = []
    price = Decimal(start_price)
    for _ in range(n):
        bars.append(
            CandleBar(
                open=price,
                high=price + Decimal("0.005"),
                low=price - Decimal("0.005"),
                close=price + Decimal(step),
                volume=Decimal("100"),
            )
        )
        price += Decimal(step)
    return bars


def _snapshot(**overrides) -> CandidateSnapshot:
    defaults = dict(
        chain="solana",
        token_address="T1",
        pool_address="P1",
        w5=_bars(5),
        w10=_bars(10),
        pool=PoolSnapshot(
            reserve_usd=Decimal("200000"),
            fdv_usd=Decimal("5000000"),
            volume_m5_usd=Decimal("30000"),
            volume_m15_usd=Decimal("60000"),
            tx_m5=TxBucket(buys=60, sells=30, buyers=45, sellers=25),
            tx_m15=TxBucket(buys=150, sells=90, buyers=100, sellers=70),
            price_change_5m_pct=Decimal("15"),
        ),
        gt_score=Decimal("90"),
        gt_verified=True,
        holder_count=500,
        top10_holding_pct=Decimal("20"),
        developer_or_creator_holding_pct=Decimal("3"),
        lp_locked_pct=Decimal("95"),
        smart=SmartMoneySnapshot(
            token_profitable_net_buy_60s_usd=Decimal("1200"),
            active_smart_wallets_60s=3,
        ),
    )
    defaults.update(overrides)
    return CandidateSnapshot(**defaults)


class TestFeatures:
    def test_basic_formulas(self):
        f = compute_features(_snapshot())
        assert f["reserve_usd"] == Decimal("200000")
        assert f["volume_liquidity_ratio_5m"] == Decimal("30000") / Decimal("200000")
        assert f["fdv_liquidity_ratio"] == Decimal("5000000") / Decimal("200000")
        assert f["tx_count_5m"] == Decimal(90)
        assert f["buy_sell_tx_ratio_5m"] == Decimal(60) / Decimal(30)
        assert f["net_buy_tx_pct_5m"] == Decimal(30) / Decimal(90) * 100
        assert f["volume_rate_ratio_m5_to_previous_10m"] == (
            Decimal("30000") / 5
        ) / ((Decimal("60000") - Decimal("30000")) / 10)
        assert f["gt_verified"] == Decimal(1)
        assert f["holder_count"] == Decimal(500)

    def test_price_change_windows(self):
        f = compute_features(_snapshot())
        w5 = _bars(5)
        expected_5s = (w5[-1].close / w5[0].open - 1) * 100
        assert f["price_change_5s_pct"] == expected_5s

    def test_close_location_flat(self):
        bars = _bars(10, step="0")
        f = compute_features(_snapshot(w10=bars, w5=_bars(5, step="0")))
        assert f["close_location_10s"] == Decimal("0.5")

    def test_microstructure(self):
        f = compute_features(_snapshot())
        w10 = _bars(10)
        assert f["positive_1s_bar_ratio_10s"] == Decimal(10) / Decimal(10)
        front = sum((c.volume for c in w10[:5]), Decimal(0))
        back = sum((c.volume for c in w10[5:]), Decimal(0))
        assert f["volume_retention_ratio_10s"] == back / front

    def test_missing_pool(self):
        f = compute_features(_snapshot(pool=None))
        assert f["reserve_usd"] is None
        assert f["price_change_5m_pct"] is None

    def test_smart_money_incomplete(self):
        f = compute_features(
            _snapshot(smart=SmartMoneySnapshot(complete=False))
        )
        assert f["token_profitable_net_buy_60s_as_pct_of_volume_5m"] is None


class TestScoring:
    def test_forward_and_reverse(self):
        f = compute_features(_snapshot())
        result = score(f, STRATEGY.solana)
        assert 0 <= result.total_score <= 100
        # 反向特征 fdv_liquidity_ratio = 25，bad=300 good=25 → 恰好 good 满分
        assert result.feature_scores["fdv_liquidity_ratio"] == Decimal(100)
        assert result.rejected is False

    def test_missing_reject(self):
        f = compute_features(_snapshot(pool=None))
        result = score(f, STRATEGY.solana)
        assert result.rejected is True
        assert "reserve_usd" in result.missing_applied

    def test_missing_cap_setup(self):
        snap = _snapshot()
        snap.pool.volume_m5_usd = None
        f = compute_features(snap)
        result = score(f, STRATEGY.solana)
        assert result.capped_setup is True

    def test_zero_score_missing(self):
        f = compute_features(_snapshot(gt_score=None))
        result = score(f, STRATEGY.solana)
        assert result.feature_scores["gt_score"] == Decimal(0)
        assert result.rejected is False  # gt_score 的 missing_action 是 ZERO_SCORE

    def test_local_smart_weight_zero(self):
        # v1：LOCAL_SMART 特征权重为 "0"，不影响总分
        a = score(compute_features(_snapshot()), STRATEGY.solana)
        snap = _snapshot(
            smart=SmartMoneySnapshot(
                token_profitable_net_buy_60s_usd=Decimal("1200"),
                local_smart_net_buy_60s_usd=Decimal("999999"),
                active_smart_wallets_60s=3,
            )
        )
        b = score(compute_features(snap), STRATEGY.solana)
        assert a.total_score == b.total_score

    def test_dimension_weight_sum(self):
        f = compute_features(_snapshot())
        result = score(f, STRATEGY.solana)
        recomputed = sum(
            (result.dimension_scores[n] * d.weight for n, d in STRATEGY.solana.scoring.dimensions.items()),
            Decimal(0),
        )
        assert abs(recomputed - result.total_score) < Decimal("0.00000001")


class TestSignalLevel:
    def test_buy_level(self):
        result = score(compute_features(_snapshot()), STRATEGY.solana)
        level = signal_level(result, STRATEGY.solana)
        assert level in ("WATCHING", "SETUP", "BUY")


class TestAntiChase:
    def test_no_wait(self):
        bars = _bars(10, step="0.001")  # 微小涨幅
        result = anti_chase_check(bars, bars, STRATEGY.solana.anti_chase)
        assert result.allowed is True
        assert result.wait_triggered is False

    def test_vertical_rise_triggers_wait_then_consolidation(self):
        # 垂直拉升超过阈值
        bars = _bars(10, step="0.02")  # 每根 +2%，10 根约 +20%
        config = STRATEGY.solana.anti_chase
        result = anti_chase_check(bars, bars, config)
        assert result.wait_triggered is True

        # 高位整理：后段价格平稳、回撤小
        w10 = _bars(10, step="0.02")
        tail = _bars(5, start_price=str(w10[-1].close), step="0.0001")
        wait = w10 + tail
        result = anti_chase_check(w10, wait, config)
        assert result.allowed is True

    def test_drawdown_block(self):
        w10 = _bars(10, step="0.02")
        # 等待期大幅回撤
        tail = _bars(5, start_price=str(w10[-1].close), step="-0.02")
        wait = w10 + tail
        result = anti_chase_check(w10, wait, STRATEGY.solana.anti_chase)
        assert result.allowed is False
        assert result.reason == "回撤过大"


class TestSmartMoney:
    def test_token_profitable_classification(self):
        trades = [
            {"tx_from_address": "W1", "kind": "buy", "volume_in_usd": "500"},
            {"tx_from_address": "W1", "kind": "sell", "volume_in_usd": "100"},
            {"tx_from_address": "W2", "kind": "buy", "volume_in_usd": "800"},
            {"tx_from_address": "UNKNOWN", "kind": "buy", "volume_in_usd": "999"},
        ]
        result = eval_smart_money(
            token_profitable_wallets={"W1", "W2"},
            local_smart_wallets=set(),
            trades=trades,
            recent_window_seconds=60,
            volume_5m_usd=Decimal("30000"),
            min_confirming_wallets=2,
            sell_block_net_volume_pct_min=Decimal("5"),
        )
        assert result.snapshot.token_profitable_net_buy_60s_usd == Decimal("1200")
        assert result.snapshot.active_smart_wallets_60s == 2
        assert result.sell_blocked is False

    def test_local_smart_sell_block(self):
        trades = [
            {"tx_from_address": "L1", "kind": "sell", "volume_in_usd": "1500"},
            {"tx_from_address": "L2", "kind": "sell", "volume_in_usd": "500"},
        ]
        result = eval_smart_money(
            token_profitable_wallets=set(),
            local_smart_wallets={"L1", "L2"},
            trades=trades,
            recent_window_seconds=60,
            volume_5m_usd=Decimal("30000"),
            min_confirming_wallets=2,
            sell_block_net_volume_pct_min=Decimal("5"),
        )
        assert result.sell_blocked is True


class TestCandidateStateMachine:
    def _config(self):
        return STRATEGY.solana

    def test_setup_confirmation_then_signal(self):
        cfg = self._config()
        state = CandidateRuntime()
        # 首次 BUY 级得分但未达确认时长
        decision = evaluate_candidate(
            state,
            now_ms=10_000,
            total_score=Decimal("85"),
            level="BUY",
            anti_allowed=True,
            security_passed=True,
            config=cfg,
        )
        assert decision.emit_signal is False
        # setup 确认 5 秒后
        decision = evaluate_candidate(
            decision.state,
            now_ms=10_000 + cfg.signals.setup_confirmation_seconds * 1000 + 1,
            total_score=Decimal("85"),
            level="BUY",
            anti_allowed=True,
            security_passed=True,
            config=cfg,
        )
        assert decision.emit_signal is True
        assert decision.state.status == "SIGNAL"

    def test_no_double_signal_same_generation(self):
        cfg = self._config()
        # SIGNAL 状态持续高分 → 保持，不重复发信号
        state = CandidateRuntime(
            status="SIGNAL",
            setup_started_at=5_000,
            generation=1,
            generation_signal_emitted=True,
        )
        decision = evaluate_candidate(
            state,
            now_ms=20_000,
            total_score=Decimal("90"),
            level="BUY",
            anti_allowed=True,
            security_passed=True,
            config=cfg,
        )
        assert decision.emit_signal is False

        # 状态回到 SETUP 但本世代已发过 → 明确拒绝重发
        state2 = CandidateRuntime(
            status="SETUP",
            setup_started_at=5_000,
            generation=1,
            generation_signal_emitted=True,
        )
        decision2 = evaluate_candidate(
            state2,
            now_ms=20_000,
            total_score=Decimal("90"),
            level="BUY",
            anti_allowed=True,
            security_passed=True,
            config=cfg,
        )
        assert decision2.emit_signal is False
        assert decision2.reason == "generation_already_emitted"

    def test_gates_blocked(self):
        cfg = self._config()
        state = CandidateRuntime(status="SETUP", setup_started_at=1_000)
        decision = evaluate_candidate(
            state,
            now_ms=10_000,
            total_score=Decimal("90"),
            level="BUY",
            anti_allowed=False,
            security_passed=True,
            config=cfg,
        )
        assert decision.emit_signal is False
        assert decision.state.status == "SETUP"

    def test_rearm_new_generation(self):
        cfg = self._config()
        state = CandidateRuntime(
            status="SIGNAL",
            generation=1,
            generation_signal_emitted=True,
        )
        # 持续低于 setup 达重新武装时长
        rearm_ms = cfg.signals.signal_rearm_below_setup_seconds * 1000
        d1 = evaluate_candidate(
            state, now_ms=1_000, total_score=Decimal("10"), level="WATCHING",
            anti_allowed=True, security_passed=True, config=cfg,
        )
        d2 = evaluate_candidate(
            d1.state, now_ms=1_000 + rearm_ms + 1, total_score=Decimal("10"),
            level="WATCHING", anti_allowed=True, security_passed=True, config=cfg,
        )
        assert d2.state.generation == 2
        assert d2.state.generation_signal_emitted is False


class TestSignalCreation:
    @pytest.fixture
    def db_path(self, tmp_path):
        from app.core.storage.database import clear_env_db

        path = tmp_path / "bot.sqlite"
        yield path
        clear_env_db(path)

    async def test_create_signal(self, db_path):
        from app.core.storage.database import Database, clear_env_db
        from app.core.storage.repository import Repository
        from app.core.services.security import SecurityResult
        from app.core.services.strategy import (
            AntiChaseResult,
            SmartMoneyEval,
            create_signal,
        )

        db = Database(db_path)
        await db.open()
        repo = Repository(db.conn)
        snapshot = _snapshot()
        features = compute_features(snapshot)
        scoring = score(features, STRATEGY.solana)
        security = SecurityResult()
        security.set("gt_score", "SAFE")
        security.recompute()
        smart = SmartMoneyEval(snapshot=SmartMoneySnapshot())
        signal_id = await create_signal(
            repo,
            chain="solana",
            token_address="T1",
            pool_address="P1",
            signal_level_value="BUY",
            scoring=scoring,
            features=features,
            reference_price=Decimal("1.1"),
            anti=AntiChaseResult(True),
            security=security,
            pre_execution=security,
            smart=smart,
            generation=1,
            strategy_revision="strategy-1",
            strategy_hash="h",
            validity_seconds=60,
            now_ms=1_000_000,
        )
        row = await repo.get_signal(signal_id)
        assert row is not None
        assert row["telegram_status"] == "PENDING"
        assert row["event_published"] == 0
        assert row["reference_price"] == "1.1"
        import json

        decision = json.loads(row["decision_snapshot"])
        assert decision["scoring"]["total_score"] == str(scoring.total_score)
        await db.close()
        clear_env_db(db_path)
