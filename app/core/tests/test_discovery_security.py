"""发现流水线与安全门禁测试（G4）。"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.clients.coingecko import (
    CoinGeckoClient,
    PoolAttributes,
    TxBucket,
)
from app.core.config import load_strategy_file
from app.core.services.discovery import (
    PoolLabel,
    classify_pool,
    decide_pool,
    passes_early_gate,
)
from app.core.services.security import (
    BURN_ADDRESSES_EVM,
    evaluate_bsc,
    evaluate_solana,
    top10_holding_pct,
)

FIXTURES = Path(__file__).parent / "fixtures"
STRATEGY = load_strategy_file(FIXTURES / "strategy_valid.yaml")


def _pool(**overrides) -> PoolAttributes:
    defaults = dict(
        address="P1",
        name="T / SOL",
        reserve_in_usd=Decimal("100000"),
        fdv_usd=Decimal("1000000"),
        pool_created_at_ms=1_000_000,
        price_change_pct={"m5": Decimal("3"), "m15": Decimal("5")},
        transactions={
            "m5": TxBucket(buys=50, sells=30, buyers=40, sellers=25),
            "m15": TxBucket(buys=150, sells=90, buyers=100, sellers=70),
        },
        volume_usd={"m5": Decimal("20000"), "m15": Decimal("60000")},
    )
    defaults.update(overrides)
    return PoolAttributes(**defaults)


class TestClassifyPool:
    def test_hot_from_trending(self):
        template = STRATEGY.solana.collection.query_templates[2]  # trending_pools
        label = classify_pool(
            template=template,
            rank=3,
            received_at_ms=10_000_000,
            pool=_pool(),
            channel=STRATEGY.solana.discovery_channels,
        )
        assert label.hot is True
        assert label.main == "hot"

    def test_hot_rank_exceeded(self):
        template = STRATEGY.solana.collection.query_templates[2]
        label = classify_pool(
            template=template,
            rank=25,  # 新阈值 max_source_rank=20
            received_at_ms=10_000_000,
            pool=_pool(),
            channel=STRATEGY.solana.discovery_channels,
        )
        assert label.hot is False

    def test_anomaly_by_acceleration(self):
        template = STRATEGY.solana.collection.query_templates[1]  # m5_pcp
        pool = _pool(
            volume_usd={"m5": Decimal("50000"), "m15": Decimal("60000")},
        )
        label = classify_pool(
            template=template,
            rank=1,
            received_at_ms=10_000_000,
            pool=pool,
            channel=STRATEGY.solana.discovery_channels,
        )
        assert label.anomaly is True

    def test_anomaly_by_price_change(self):
        template = STRATEGY.solana.collection.query_templates[1]
        pool = _pool(price_change_pct={"m5": Decimal("-8")})
        label = classify_pool(
            template=template,
            rank=1,
            received_at_ms=10_000_000,
            pool=pool,
            channel=STRATEGY.solana.discovery_channels,
        )
        assert label.anomaly is True

    def test_new_pool_by_age(self):
        template = STRATEGY.solana.collection.query_templates[0]  # megafilter
        pool = _pool(pool_created_at_ms=9_999_000)  # 1 秒前（received=10_000_000）
        label = classify_pool(
            template=template,
            rank=1,
            received_at_ms=10_000_000,
            pool=pool,
            channel=STRATEGY.solana.discovery_channels,
        )
        assert label.new_pool is True

    def test_main_label_priority(self):
        assert PoolLabel(True, True, True).main == "hot"
        assert PoolLabel(False, True, True).main == "anomaly"
        assert PoolLabel(False, False, True).main == "new_pool"


class TestEarlyGate:
    def test_pass(self):
        assert passes_early_gate(_pool(), STRATEGY.solana.collection) is True

    def test_low_reserve(self):
        assert passes_early_gate(_pool(reserve_in_usd=Decimal("100")), STRATEGY.solana.collection) is False

    def test_low_tx(self):
        pool = _pool(
            transactions={"m5": TxBucket(buys=5, sells=5), "m15": TxBucket(buys=10, sells=10)}
        )
        assert passes_early_gate(pool, STRATEGY.solana.collection) is False


class TestDecidePool:
    def test_order(self):
        from app.core.clients.coingecko import PoolDetail

        a = PoolDetail(address="A")
        b = PoolDetail(address="B")
        c = PoolDetail(address="C")
        chosen = decide_pool(
            [(a, Decimal("80")), (b, Decimal("90")), (c, None)],  # c 为 INCOMPLETE
            liquidity={"A": Decimal("100"), "B": Decimal("100")},
            volume_5m={"A": Decimal("10"), "B": Decimal("10")},
        )
        assert chosen == "B"  # 总分最高

    def test_tie_breaks(self):
        from app.core.clients.coingecko import PoolDetail

        a = PoolDetail(address="A")
        b = PoolDetail(address="B")
        chosen = decide_pool(
            [(a, Decimal("80")), (b, Decimal("80"))],
            liquidity={"A": Decimal("100"), "B": Decimal("200")},
            volume_5m={"A": Decimal("10"), "B": Decimal("10")},
        )
        assert chosen == "B"  # 同分按流动性

    def test_empty(self):
        assert decide_pool([], liquidity={}, volume_5m={}) is None


class TestSecuritySolana:
    def _load(self, name):
        raw = json.loads(
            (FIXTURES / "vendor_raw" / f"{name}.json").read_text(encoding="utf-8")
        )
        return raw

    def test_values_populated(self):
        from app.core.clients.coingecko import TokenInfo
        from app.core.clients.goplus import parse_solana

        goplus_raw = self._load("goplus_solana_raw")
        goplus = parse_solana(next(iter(goplus_raw["result"])), next(iter(goplus_raw["result"].values())))
        token_info = TokenInfo(
            address="T", gt_score=Decimal("90"), developer_holding_percentage=Decimal("3")
        )
        from app.core.clients.coingecko import TopHolders

        result = evaluate_solana(
            token_info=token_info,
            goplus=goplus,
            security_cfg=STRATEGY.solana.security,
            main_pool_address="POOL1",
            coin_holders=TopHolders(holders=[]),
        )
        # 结构化数值传递（修复 detail 字符串解析问题）
        assert "top10_holding_pct" in result.values
        assert "lp_locked_pct" in result.values
        assert result.values["developer_or_creator_holding_pct"] == Decimal("3")

    def test_evaluate_with_real_fixture(self):
        from app.core.clients.coingecko import TokenInfoBuilder, TopHolders
        from app.core.clients.goplus import parse_solana

        token_raw = self._load("token_info_raw")
        holders_raw = self._load("top_holders_raw")
        goplus_raw = self._load("goplus_solana_raw")

        token_info = TokenInfoBuilder().build(token_raw)
        holders = TopHolders.model_validate(holders_raw["data"]["attributes"])
        goplus = parse_solana(next(iter(goplus_raw["result"])), next(iter(goplus_raw["result"].values())))

        result = evaluate_solana(
            token_info=token_info,
            goplus=goplus,
            security_cfg=STRATEGY.solana.security,
            main_pool_address=None,
            coin_holders=holders,
        )
        # 无主池时 LP 字段为 UNKNOWN，整体不允许发信号
        assert result.trade_allowed is False
        assert result.fields["lp_locked_pct"].status == "UNKNOWN"
        assert "gt_score" in result.fields
        assert "mint_authority" in result.fields
        assert "transfer_fee_bps" in result.fields


class TestSecurityBsc:
    def test_evaluate_bsc_fixture(self):
        from app.core.clients.goplus import parse_evm

        goplus_raw = json.loads(
            (FIXTURES / "vendor_raw" / "goplus_bsc_raw.json").read_text(encoding="utf-8")
        )
        address = next(iter(goplus_raw["result"]))
        goplus = parse_evm(address, goplus_raw["result"][address])

        from app.core.clients.coingecko import TokenInfo

        token_info = TokenInfo(address=address, gt_score=Decimal("90"), is_honeypot=False)
        result = evaluate_bsc(
            token_info=token_info,
            goplus=goplus,
            security_cfg=STRATEGY.bsc.security,
            main_pool_address=None,
            coin_holders=None,
        )
        assert result.fields["open_source"].status in ("SAFE", "RISK")
        assert result.fields["lp_locked_pct"].status == "UNKNOWN"  # 无主池
        assert result.trade_allowed is False

    def test_normal_token_open_source_semantics(self):
        """P1 修复正例：开源 + 已上 DEX 的正常代币，"1" 必须判 SAFE（文档 §5.4 "0 拒绝"）。"""
        from app.core.clients.coingecko import TokenInfo
        from app.core.clients.goplus import EvmSecurity

        goplus = EvmSecurity(
            token_address="0xNORMAL",
            is_open_source="1",
            is_in_dex="1",
            is_honeypot="0",
            cannot_buy="0",
            cannot_sell_all="0",
            buy_tax=Decimal("0"),
            sell_tax=Decimal("0"),
            transfer_tax=Decimal("0"),
            slippage_modifiable="0",
            is_blacklisted="0",
            is_whitelisted="0",
            is_mintable="0",
            transfer_pausable="0",
            is_anti_whale="0",
            anti_whale_modifiable="0",
            trading_cooldown="0",
            hidden_owner="0",
            can_take_back_ownership="0",
            selfdestruct="0",
            external_call="0",
            owner_change_balance="0",
            creator_percent=Decimal("0.02"),
            owner_percent=Decimal("0"),
        )
        token_info = TokenInfo(address="0xNORMAL", gt_score=Decimal("90"), is_honeypot=False)
        result = evaluate_bsc(
            token_info=token_info,
            goplus=goplus,
            security_cfg=STRATEGY.bsc.security,
            main_pool_address="0xPOOL",
            coin_holders=None,
        )
        assert result.fields["open_source"].status == "SAFE"
        assert result.fields["in_dex"].status == "SAFE"
        # LP 锁仓缺数据仍为 UNKNOWN（主池未匹配），但语义不再因 open_source 反转误杀
        assert result.fields["lp_locked_pct"].status == "UNKNOWN"


class TestTop10:
    def test_burn_excluded(self):
        from app.core.clients.coingecko import HolderEntry, TopHolders

        holders = TopHolders(
            holders=[
                HolderEntry(address="0x000000000000000000000000000000000000dead", percentage=Decimal("60")),
                HolderEntry(address="0xABC", percentage=Decimal("30")),
            ]
        )
        pct = top10_holding_pct(holders, [], burn_addresses=BURN_ADDRESSES_EVM, chain_norm=str.lower)
        assert pct == Decimal("30")
