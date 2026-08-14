"""SignalPipeline 端到端测试（主体补全 F：多池屏障 + 信号产出 + 频道推送）。"""
import asyncio
import json
from decimal import Decimal

import pytest

from app.core.bus.events import EventBus
from app.core.clients.coingecko import (
    PoolAttributes,
    PoolDetail,
    TokenInfo,
    TopHolders,
    TopTraders,
    TraderEntry,
    TradeEntry,
    TxBucket,
)
from app.core.clients.coingecko_ws import G3CandleBuffer
from app.core.config import load_strategy_file
from app.core.services.security import SecurityResult
from app.core.services.strategy import SignalPipeline
from app.core.storage.database import Database, clear_env_db
from app.core.storage.repository import Repository

STRATEGY = load_strategy_file(
    __import__("pathlib").Path(__file__).parent / "fixtures" / "strategy_valid.yaml"
)


def _pool(address: str, *, base="T1", quote="So11111111111111111111111111111111111111112") -> PoolAttributes:
    return PoolAttributes(
        address=address,
        base_token_address=base,
        quote_token_address=quote,
        reserve_in_usd=Decimal("200000"),
        fdv_usd=Decimal("5000000"),
        price_change_pct={"m5": Decimal("15")},
        transactions={
            "m5": TxBucket(buys=60, sells=30, buyers=45, sellers=25),
            "m15": TxBucket(buys=150, sells=90, buyers=100, sellers=70),
        },
        volume_usd={"m5": Decimal("30000"), "m15": Decimal("60000")},
    )


def _detail(address: str, reserve: str = "200000") -> PoolDetail:
    import time

    return PoolDetail(
        address=address,
        reserve_in_usd=Decimal(reserve),
        fdv_usd=Decimal("5000000"),
        pool_created_at_ms=int(time.time() * 1000) - 300_000,  # 池龄 5 分钟
        price_change_pct={"m5": Decimal("15")},
        transactions={
            "m5": TxBucket(buys=60, sells=30, buyers=45, sellers=25),
            "m15": TxBucket(buys=150, sells=90, buyers=100, sellers=70),
        },
        volume_usd={"m5": Decimal("30000"), "m15": Decimal("60000")},
    )


class FakeWS:
    def __init__(self):
        self.g3 = set()

    def add_g3_pool(self, pool, side):
        if pool in self.g3:
            return True
        self.g3.add(pool)
        return True

    def remove_g3_pool(self, pool, side):
        self.g3.discard(pool)


class FakeCG:
    def __init__(self):
        self.details = {}
        self.snapshot_calls = 0

    async def pools_multi(self, chain, addresses, include_volume_breakdown=True):
        self.snapshot_calls += 1
        return [self.details[a] for a in addresses if a in self.details]

    async def top_traders(self, chain, token, traders=20):
        return TopTraders(
            traders=[
                TraderEntry(address="W1", realized_pnl_usd=Decimal("500")),
                TraderEntry(address="W2", realized_pnl_usd=Decimal("-10")),
            ]
        )

    async def pool_trades(self, chain, pool, token="base"):
        now = int(__import__("time").time() * 1000)
        return [
            TradeEntry(
                tx_hash=f"h{pool}",
                tx_from_address="W1",
                from_token_address="So11111111111111111111111111111111111111112",
                to_token_address="T1",
                kind="buy",
                volume_in_usd=Decimal("800"),
                block_timestamp_ms=now,
            )
        ]


class FakePipeline:
    def __init__(self):
        self.security = None
        self.result = None

    async def deep_check(self, token, addresses):
        from app.core.services.discovery import DeepCheckResult
        from app.core.services.security import SecurityResult

        security = SecurityResult()
        for name in (
            "gt_score", "open_source", "in_dex", "honeypot_cg", "mint_authority",
            "freeze_authority", "developer_holding_pct", "top10_holding_pct",
            "lp_locked_pct", "transfer_fee_bps",
        ):
            security.set(name, "SAFE")
        security.set_value("top10_holding_pct", Decimal("20"))
        security.set_value("developer_or_creator_holding_pct", Decimal("3"))
        security.set_value("lp_locked_pct", Decimal("95"))
        security.set_value("creator_owner_addresses", [])
        security.recompute()
        self.security = security
        self.result = DeepCheckResult(
            chain="solana",
            token_address=token,
            token_info=TokenInfo(address=token, gt_score=Decimal("90"), gt_verified=True, holder_count=500),
            holders=TopHolders(holders=[]),
            security=security,
            first_snapshot_at_ms=int(__import__("time").time() * 1000) - 15_000,
            completed=True,
        )
        return self.result

    async def _security_check(self, token, info, holders, *, kind, main_pool_address=None):
        security = self.security or SecurityResult()
        if kind == "PRE_EXECUTION_CHECK":
            fresh = SecurityResult(trade_allowed=True)
            fresh.set("pre_exec", "SAFE")
            fresh.set_value("top10_holding_pct", Decimal("20"))
            return fresh
        return security


class FakeBus:
    def __init__(self):
        self.published = []

    async def publish_signal_created(self, signal_id):
        self.published.append(signal_id)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "bot.sqlite"
    yield path
    clear_env_db(path)


async def _feed_bars(buffer: G3CandleBuffer, pool: str, n: int = 12) -> None:
    from app.core.clients.coingecko_ws import Candle

    latest = buffer.final_bars(pool, "base")
    if latest:
        start = max(b.open_ts_ms for b in latest) + 1000  # 从最后一根继续，保证连续性
    else:
        start = int(__import__("time").time() * 1000) - n * 1000
    for i in range(n):
        buffer.upsert(
            pool,
            "base",
            Candle(
                open_ts_ms=start + i * 1000,
                open=Decimal("1.0"),
                high=Decimal("1.01"),
                low=Decimal("0.99"),
                close=Decimal("1.0"),
                volume=Decimal("100"),
                final=True,
            ),
        )


class TestSignalPipelineE2E:
    def _chain(self):
        """测试链策略：降低阈值以便合成数据可产出信号（其余字段沿用基线）。"""
        from decimal import Decimal

        scoring = STRATEGY.solana.scoring.model_copy(
            update={
                "watch_score_min": Decimal("20"),
                "setup_score_min": Decimal("30"),
                "buy_score_min": Decimal("40"),
            }
        )
        signals = STRATEGY.solana.signals.model_copy(
            update={"setup_confirmation_seconds": 1}
        )
        return STRATEGY.solana.model_copy(
            update={"scoring": scoring, "signals": signals}
        )

    async def test_multi_pool_barrier_signal(self, db_path):
        db = Database(db_path)
        await db.open()
        repo = Repository(db.conn)
        cg = FakeCG()
        ws = FakeWS()
        buffer = G3CandleBuffer()
        bus = FakeBus()
        # 两个可信池：P1 reserve 低 → volume_liq 高 → 总分更高，屏障应选 P1
        cg.details["P1"] = _detail("P1", reserve="100000")
        cg.details["P2"] = _detail("P2", reserve="300000")
        await _feed_bars(buffer, "P1")
        await _feed_bars(buffer, "P2")

        pipeline = SignalPipeline(
            chain="solana",
            strategy=self._chain(),
            repo=repo,
            cg=cg,
            gp=None,
            ws=ws,
            candle_buffer=buffer,
            pipeline=FakePipeline(),
            bus=bus,
            admin=None,
            signal_chat_id=-100123,
            active_snapshot=("strategy-1", "h"),
        )

        await pipeline.handle_pool(_pool("P1"), None)
        await pipeline.handle_pool(_pool("P2"), None)
        # 第一轮：SETUP 确认计时开始（不产信号）；等待聚合与处理完全结束
        for _ in range(600):
            if not pipeline._processing and not pipeline._pending:
                break
            await asyncio.sleep(0.05)
        assert bus.published == []  # 首轮确认期未满
        await asyncio.sleep(1.2)
        # 第二轮：确认期已过 → 产出信号
        await _feed_bars(buffer, "P1", n=12)
        await _feed_bars(buffer, "P2", n=12)
        await pipeline.handle_pool(_pool("P1"), None)
        await pipeline.handle_pool(_pool("P2"), None)
        for _ in range(600):
            if bus.published:
                break
            await asyncio.sleep(0.05)
        assert len(bus.published) == 1
        row = await repo.get_signal(bus.published[0])
        # 屏障按 总分 > 流动性 > 成交量 > 地址：新权重下 reserve 主导 → P2 总分更高
        assert row["pool_address"] == "P2"
        assert row["chain"] == "solana"
        # 信号推送到信号频道（chat_target = -100123）
        cursor = await repo.conn.execute(
            "SELECT chat_target, kind FROM telegram_outbox"
        )
        outbox_rows = await cursor.fetchall()
        assert any(
            r["kind"] == "BUY_SIGNAL" and r["chat_target"] == "-100123"
            for r in outbox_rows
        )
        # 处理完成后订阅已释放
        assert ws.g3 == set()
        await db.close()

    async def test_untrusted_quote_observed_only(self, db_path):
        db = Database(db_path)
        await db.open()
        repo = Repository(db.conn)
        cg = FakeCG()
        ws = FakeWS()
        buffer = G3CandleBuffer()
        bus = FakeBus()

        pipeline = SignalPipeline(
            chain="solana",
            strategy=STRATEGY.solana,
            repo=repo,
            cg=cg,
            gp=None,
            ws=ws,
            candle_buffer=buffer,
            pipeline=FakePipeline(),
            bus=bus,
            admin=None,
            signal_chat_id=-100123,
            active_snapshot=("strategy-1", "h"),
        )
        # 假同名报价代币（不在可信列表）→ 仅观察
        bad = _pool("PBAD", quote="FakeQuote111111111111111111111111111111111")
        await pipeline.handle_pool(bad, None)
        for _ in range(100):
            if pipeline._processing or pipeline._pending:
                await asyncio.sleep(0.01)
            else:
                break
        await asyncio.sleep(0.15)
        assert bus.published == []
        assert cg.snapshot_calls == 0  # 从未进入深查
        await db.close()
