"""回测引擎（总控文档第 10 章）。

- 数据准备：OHLCV 回补计划 + UTC 秒分页 + 管理员知情确认（fetch 由调用方经
  CoinGeckoClient 执行，本模块只做计划与分页参数计算）。
- 1m 成交模型：纯函数，NOT_FILLED / DATA_GAP / synthetic 隔离。
- 报告指标（第 10.5 节）与 run_id 确定性（第 10.3 节）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Literal

from app.core.models import canonical_json, stable_sha256
from app.core.services.strategy import CandleBar

log = logging.getLogger(__name__)

BACKTEST_ENGINE_VERSION = "1"
OHLCV_PAGE_LIMIT = 1000
OHLCV_RANGE_LIMIT_SECONDS = 6 * 30 * 24 * 3600  # 单次 6 个月


@dataclass
class BackfillRequest:
    chain: str
    pool_address: str
    token_address: str
    range_start_sec: int  # UTC 秒（含）
    range_end_sec: int    # UTC 秒（不含）


def plan_backfill(
    *,
    chain: str,
    pool_address: str,
    token_address: str,
    start_ms: int,
    end_ms: int,
) -> list[BackfillRequest]:
    """回补计划：按 6 个月单次范围上限切分（第 10.1 节）。"""
    start_sec = start_ms // 1000
    end_sec = end_ms // 1000
    requests = []
    cursor = end_sec
    while cursor > start_sec:
        piece_start = max(start_sec, cursor - OHLCV_RANGE_LIMIT_SECONDS)
        requests.append(
            BackfillRequest(
                chain=chain,
                pool_address=pool_address,
                token_address=token_address,
                range_start_sec=piece_start,
                range_end_sec=cursor,
            )
        )
        cursor = piece_start
    return list(reversed(requests))


async def fetch_backfill(client, request: BackfillRequest) -> list[CandleBar]:
    """执行单个回补请求：before_timestamp 向前分页（第 10.1 节）。"""
    bars: dict[int, CandleBar] = {}
    before = request.range_end_sec
    while True:
        page = await client.pool_ohlcv(
            request.chain,
            request.pool_address,
            timeframe="minute",
            aggregate=1,
            before_timestamp=before,
            limit=OHLCV_PAGE_LIMIT,
            currency="usd",
            token=request.token_address,
            include_empty_intervals=False,
        )
        if not page:
            break
        earliest = None
        for bar in page:
            ts_sec = bar.timestamp_ms // 1000
            if ts_sec < request.range_start_sec:
                continue
            if earliest is None or ts_sec < earliest:
                earliest = ts_sec
            bars[bar.timestamp_ms] = bar
        if earliest is None or earliest <= request.range_start_sec:
            break
        before = earliest - 1
    return [bars[k] for k in sorted(bars)]


# ---------------------------------------------------------------------------
# 1m 成交模型（第 10.2 节）
# ---------------------------------------------------------------------------

FillStatus = Literal["FILLED", "NOT_FILLED", "DATA_GAP"]


@dataclass
class FillResult:
    status: FillStatus
    entry_price: Decimal | None = None
    entry_bar_ts_ms: int | None = None
    reason: str | None = None


@dataclass
class ExitResult:
    exited: bool
    exit_price: Decimal | None = None
    exit_bar_ts_ms: int | None = None
    exit_reason: str | None = None
    remaining_ratio: Decimal = Decimal(1)  # 未卖出比例（0-1）


@dataclass
class ReplayResult:
    fill: FillResult
    exit: ExitResult | None = None
    realized_pnl_pct: Decimal | None = None
    data_gap: bool = False


def impact_bps(order_usd: Decimal, reserve_usd: Decimal, config) -> Decimal:
    """冲击公式（第 10.2 节）。"""
    if reserve_usd <= 0:
        return config.max_impact_bps
    return min(
        config.max_impact_bps,
        config.impact_coefficient * order_usd / reserve_usd * 10000,
    )


def entry_fill(
    *,
    signal_price: Decimal,
    signal_minute_start_ms: int,
    bars: list[CandleBar],
    reserve_usd: Decimal,
    order_usd: Decimal,
    config,
) -> FillResult:
    """入场：信号后下一根真实完整 1m K 线开盘价（第 10.2 节第 1 步）。"""
    signal_minute_end = signal_minute_start_ms + 60_000
    candidates = [
        b for b in bars if b.open_ts_ms >= signal_minute_end
    ]
    if not candidates:
        return FillResult("DATA_GAP", reason="缺少信号后 K 线")
    entry_bar = candidates[0]
    if entry_bar.open_ts_ms > signal_minute_start_ms + config.fill_deadline_seconds * 1000:
        return FillResult("NOT_FILLED", reason="超出成交截止时间")
    deviation = abs(entry_bar.open - signal_price) / signal_price * 100
    if deviation > config.fill_deviation_pct_max:
        return FillResult("NOT_FILLED", reason="入场价格偏离超限")
    if reserve_usd < config.min_fill_liquidity_usd:
        return FillResult("NOT_FILLED", reason="流动性不足")
    total_adverse_bps = config.base_slippage_bps + impact_bps(order_usd, reserve_usd, config)
    price = entry_bar.open * (1 + total_adverse_bps / 10000)
    return FillResult("FILLED", entry_price=price, entry_bar_ts_ms=entry_bar.open_ts_ms)


def replay_exits(
    *,
    entry_price: Decimal,
    entry_bar_ts_ms: int,
    bars: list[CandleBar],
    order_usd: Decimal,
    reserve_usd: Decimal,
    config,
) -> ExitResult:
    """逐根 K 线回放退出（第 10.2 节第 2-4 步）。

    止盈档位合并：同一 K 线触发的档位合计卖出比例。
    同 K 线同时触及止盈/止损/追踪止损 → 按最低卖出价的全仓保护性退出。
    """
    after = [b for b in bars if b.open_ts_ms > entry_bar_ts_ms]
    if not after:
        return ExitResult(False)

    trailing_high: Decimal | None = None
    remaining = Decimal(1)
    legs = sorted(config.take_profit_legs, key=lambda x: x.trigger_profit_pct)
    leg_remaining = [leg.sell_pct_of_initial / Decimal(100) for leg in legs]
    leg_hit = [False] * len(legs)

    for bar in after:
        synthetic = getattr(bar, "synthetic", False)
        if synthetic:
            continue  # 合成条不触发成交（第 10.2 节）
        if reserve_usd <= 0 or reserve_usd < config.min_fill_liquidity_usd:
            return ExitResult(False)

        adverse = config.base_slippage_bps + impact_bps(order_usd, reserve_usd, config)

        tp_ratio = Decimal(0)
        for i, leg in enumerate(legs):
            if not leg_hit[i] and bar.high >= entry_price * (1 + leg.trigger_profit_pct / 100):
                leg_hit[i] = True
                tp_ratio += leg_remaining[i]
        sl_hit = bar.low <= entry_price * (1 - config.stop_loss_pct / 100)

        if bar.high >= entry_price * (1 + config.trailing_activation_profit_pct / 100):
            if trailing_high is None:
                trailing_high = bar.high
            else:
                trailing_high = max(trailing_high, bar.high)
        trailing_hit = False
        if trailing_high is not None:
            trailing_hit = bar.low <= trailing_high * (1 - config.trailing_stop_pct / 100)

        sell_ratio = Decimal(0)
        reason = None
        if sl_hit:
            sell_ratio = Decimal(1)
            reason = "stop_loss"
        elif trailing_hit:
            sell_ratio = Decimal(1)
            reason = "trailing_stop"
        elif tp_ratio > 0:
            sell_ratio = tp_ratio
            reason = "take_profit"
        else:
            hold_seconds = (bar.open_ts_ms - entry_bar_ts_ms) / 1000
            if hold_seconds >= config.max_hold_seconds:
                sell_ratio = Decimal(1)
                reason = "max_hold"

        if reason is None:
            continue

        if reason == "take_profit":
            price = bar.open * (1 - adverse / 10000)
            remaining = max(Decimal(0), remaining - sell_ratio)
            if remaining == 0:
                return ExitResult(True, exit_price=price, exit_bar_ts_ms=bar.open_ts_ms, exit_reason=reason, remaining_ratio=Decimal(0))
            continue
        # 全仓保护性退出（含同 K 线止盈档位合并后的剩余）
        if tp_ratio > 0 and reason in ("stop_loss", "trailing_stop"):
            sell_ratio = Decimal(1)  # 同 K 线冲突：全仓按保护价
        price = bar.open * (1 - adverse / 10000)
        return ExitResult(True, exit_price=price, exit_bar_ts_ms=bar.open_ts_ms, exit_reason=reason, remaining_ratio=Decimal(0))
    return ExitResult(False, remaining_ratio=remaining)


def replay_signal(
    *,
    signal_price: Decimal,
    signal_minute_start_ms: int,
    bars: list[CandleBar],
    reserve_usd: Decimal,
    order_usd: Decimal,
    config,
) -> ReplayResult:
    fill = entry_fill(
        signal_price=signal_price,
        signal_minute_start_ms=signal_minute_start_ms,
        bars=bars,
        reserve_usd=reserve_usd,
        order_usd=order_usd,
        config=config,
    )
    if fill.status == "DATA_GAP":
        return ReplayResult(fill=fill, data_gap=True)
    if fill.status == "NOT_FILLED":
        return ReplayResult(fill=fill)
    exit_result = replay_exits(
        entry_price=fill.entry_price,
        entry_bar_ts_ms=fill.entry_bar_ts_ms,
        bars=bars,
        order_usd=order_usd,
        reserve_usd=reserve_usd,
        config=config,
    )
    pnl = None
    if exit_result.exited and exit_result.exit_price is not None:
        gross = (exit_result.exit_price - fill.entry_price) / fill.entry_price
        fee_pct = config.network_fee_native / order_usd  # v1 网络费按订单额占比折算
        pnl = (gross - fee_pct) * 100
    return ReplayResult(fill=fill, exit=exit_result, realized_pnl_pct=pnl)


# ---------------------------------------------------------------------------
# 报告（第 10.5 节）与 run_id（第 10.3 节）
# ---------------------------------------------------------------------------

@dataclass
class BacktestReport:
    strategy_revision: str
    strategy_hash: str
    run_id: str
    engine_version: str
    candidate_count: int = 0
    signal_count: int = 0
    filled_count: int = 0
    not_filled_count: int = 0
    data_gap_count: int = 0
    net_expectancy_pct: Decimal | None = None
    profit_factor: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    replay_coverage_pct: Decimal | None = None
    per_signal: list[dict] = field(default_factory=list)


def run_id_for(strategy_hash: str, dataset_hash: str) -> str:
    return stable_sha256(
        [strategy_hash, dataset_hash, BACKTEST_ENGINE_VERSION]
    )


def compute_report(
    *,
    strategy_revision: str,
    strategy_hash: str,
    dataset_hash: str,
    replays: list[ReplayResult],
    candidate_count: int,
    signal_count: int,
) -> BacktestReport:
    report = BacktestReport(
        strategy_revision=strategy_revision,
        strategy_hash=strategy_hash,
        run_id=run_id_for(strategy_hash, dataset_hash),
        engine_version=BACKTEST_ENGINE_VERSION,
        candidate_count=candidate_count,
        signal_count=signal_count,
    )
    equity = Decimal(1000)
    peak = Decimal(1000)
    max_dd = Decimal(0)
    gross_profit = Decimal(0)
    gross_loss = Decimal(0)
    total_planned = Decimal(0)
    total_net = Decimal(0)

    for replay in replays:
        report.per_signal.append(
            {
                "status": replay.fill.status,
                "pnl_pct": str(replay.realized_pnl_pct) if replay.realized_pnl_pct is not None else None,
                "data_gap": replay.data_gap,
            }
        )
        if replay.fill.status == "FILLED":
            report.filled_count += 1
        elif replay.fill.status == "NOT_FILLED":
            report.not_filled_count += 1
        else:
            report.data_gap_count += 1
        if replay.realized_pnl_pct is None:
            continue
        pnl = replay.realized_pnl_pct
        total_planned += Decimal(1)
        total_net += pnl
        if pnl > 0:
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)
        equity += pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)

    total = report.filled_count + report.not_filled_count + report.data_gap_count
    report.replay_coverage_pct = (
        Decimal(report.filled_count + report.not_filled_count) / Decimal(total) * 100
        if total
        else None
    )
    if total_planned > 0:
        report.net_expectancy_pct = total_net / total_planned
    if gross_loss > 0:
        report.profit_factor = gross_profit / gross_loss
    report.max_drawdown_pct = max_dd.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return report
