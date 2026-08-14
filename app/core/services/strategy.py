"""评分、聪明钱、防追高与信号（总控文档第 6.5、6.6、7 章）。

核心规则全部为纯函数：输入快照 → 输出决策，不持状态、不调 IO。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal, Optional

from app.core.models import canonical_json, utc_now_ms
from app.core.services.discovery import decide_pool, passes_market_quality

log = logging.getLogger(__name__)

FEATURE_DECIMALS = Decimal("0.00000001")  # 保留 8 位小数


@dataclass
class CandleBar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    open_ts_ms: int | None = None  # 回测 1m 模型需要；实时窗口聚合可不填
    synthetic: bool = False        # 回测合成延续条，不触发成交


@dataclass
class TxBucket:
    buys: int
    sells: int
    buyers: int
    sellers: int


@dataclass
class PoolSnapshot:
    reserve_usd: Decimal | None = None
    fdv_usd: Decimal | None = None
    volume_m5_usd: Decimal | None = None
    volume_m15_usd: Decimal | None = None
    tx_m5: TxBucket | None = None
    tx_m15: TxBucket | None = None
    price_change_5m_pct: Decimal | None = None


@dataclass
class SmartMoneySnapshot:
    token_profitable_net_buy_60s_usd: Decimal = Decimal(0)
    local_smart_net_buy_60s_usd: Decimal = Decimal(0)
    active_smart_wallets_60s: int = 0
    complete: bool = True  # TRUNCATED/UNKNOWN → False（特征计 0 且不阻挡）


@dataclass
class CandidateSnapshot:
    chain: str
    token_address: str
    pool_address: str
    w5: list[CandleBar] | None = None
    w10: list[CandleBar] | None = None
    pool: PoolSnapshot | None = None
    gt_score: Decimal | None = None
    gt_verified: bool | None = None
    holder_count: int | None = None
    top10_holding_pct: Decimal | None = None
    developer_or_creator_holding_pct: Decimal | None = None
    lp_locked_pct: Decimal | None = None
    smart: SmartMoneySnapshot = field(default_factory=SmartMoneySnapshot)


# ---------------------------------------------------------------------------
# 6.1 派生特征（文档第 6.5 节唯一公式）
# ---------------------------------------------------------------------------

def _q(dec: Decimal, places: int = 8) -> Decimal:
    return dec.quantize(FEATURE_DECIMALS, rounding=ROUND_HALF_EVEN)


def compute_features(s: CandidateSnapshot) -> dict[str, Decimal | None]:
    """计算全部派生特征；None 表示缺失（执行各自 missing_action）。"""
    f: dict[str, Decimal | None] = {}
    pool = s.pool

    if pool is not None:
        f["reserve_usd"] = pool.reserve_usd
        if pool.reserve_usd and pool.reserve_usd > 0:
            f["volume_liquidity_ratio_5m"] = (
                pool.volume_m5_usd / pool.reserve_usd
                if pool.volume_m5_usd is not None
                else None
            )
            f["fdv_liquidity_ratio"] = (
                pool.fdv_usd / pool.reserve_usd
                if pool.fdv_usd is not None
                else None
            )
        else:
            f["volume_liquidity_ratio_5m"] = None
            f["fdv_liquidity_ratio"] = None

        f["price_change_5m_pct"] = pool.price_change_5m_pct

        m5, m15 = pool.tx_m5, pool.tx_m15
        if m5 is not None:
            f["tx_count_5m"] = Decimal(m5.buys + m5.sells)
            f["buy_sell_tx_ratio_5m"] = Decimal(m5.buys) / Decimal(max(m5.sells, 1))
            if m5.buys + m5.sells > 0:
                f["net_buy_tx_pct_5m"] = (
                    Decimal(m5.buys - m5.sells)
                    / Decimal(m5.buys + m5.sells)
                    * 100
                )
            else:
                f["net_buy_tx_pct_5m"] = None
            f["independent_buyers_5m"] = Decimal(m5.buyers)
            f["buyer_seller_ratio_5m"] = Decimal(m5.buyers) / Decimal(
                max(m5.sellers, 1)
            )
        else:
            for key in (
                "tx_count_5m",
                "buy_sell_tx_ratio_5m",
                "net_buy_tx_pct_5m",
                "independent_buyers_5m",
                "buyer_seller_ratio_5m",
            ):
                f[key] = None

        vm5, vm15 = pool.volume_m5_usd, pool.volume_m15_usd
        if (
            vm5 is not None
            and vm15 is not None
            and vm15 > vm5
            and (vm15 - vm5) > 0
        ):
            f["volume_rate_ratio_m5_to_previous_10m"] = (vm5 / 5) / ((vm15 - vm5) / 10)
        else:
            f["volume_rate_ratio_m5_to_previous_10m"] = None
        if m5 is not None and m15 is not None:
            tx5 = Decimal(m5.buys + m5.sells)
            tx15 = Decimal(m15.buys + m15.sells)
            if tx15 > tx5 and (tx15 - tx5) > 0:
                f["tx_rate_ratio_m5_to_previous_10m"] = (tx5 / 5) / ((tx15 - tx5) / 10)
            else:
                f["tx_rate_ratio_m5_to_previous_10m"] = None
        else:
            f["tx_rate_ratio_m5_to_previous_10m"] = None
    else:
        for key in (
            "reserve_usd",
            "volume_liquidity_ratio_5m",
            "fdv_liquidity_ratio",
            "price_change_5m_pct",
            "tx_count_5m",
            "volume_rate_ratio_m5_to_previous_10m",
            "tx_rate_ratio_m5_to_previous_10m",
            "buy_sell_tx_ratio_5m",
            "net_buy_tx_pct_5m",
            "independent_buyers_5m",
            "buyer_seller_ratio_5m",
        ):
            f[key] = None

    f["gt_score"] = s.gt_score
    f["gt_verified"] = (
        Decimal(1) if s.gt_verified is True else (Decimal(0) if s.gt_verified is False else None)
    )
    f["holder_count"] = Decimal(s.holder_count) if s.holder_count is not None else None
    f["top10_holding_pct"] = s.top10_holding_pct
    f["developer_or_creator_holding_pct"] = s.developer_or_creator_holding_pct
    f["lp_locked_pct"] = s.lp_locked_pct

    w5 = s.w5 or []
    if len(w5) == 5 and w5[0].open > 0:
        f["price_change_5s_pct"] = (w5[-1].close / w5[0].open - 1) * 100
    else:
        f["price_change_5s_pct"] = None
    w10 = s.w10 or []
    if len(w10) == 10 and w10[0].open > 0:
        f["price_change_10s_pct"] = (w10[-1].close / w10[0].open - 1) * 100
    else:
        f["price_change_10s_pct"] = None

    if len(w10) == 10:
        high = max(c.high for c in w10)
        low = min(c.low for c in w10)
        f["close_location_10s"] = (
            (w10[-1].close - low) / (high - low) if high != low else Decimal("0.5")
        )
        f["positive_1s_bar_ratio_10s"] = (
            Decimal(sum(1 for c in w10 if c.close > c.open)) / Decimal(10)
        )
        front = sum((c.volume for c in w10[:5]), Decimal(0))
        back = sum((c.volume for c in w10[5:]), Decimal(0))
        f["volume_retention_ratio_10s"] = (
            back / front if front > 0 else None
        )
    else:
        f["close_location_10s"] = None
        f["positive_1s_bar_ratio_10s"] = None
        f["volume_retention_ratio_10s"] = None

    if s.smart.complete and s.pool is not None and s.pool.volume_m5_usd:
        denom = s.pool.volume_m5_usd
        f["local_smart_net_buy_60s_as_pct_of_volume_5m"] = (
            s.smart.local_smart_net_buy_60s_usd / denom * 100
        )
        f["token_profitable_net_buy_60s_as_pct_of_volume_5m"] = (
            s.smart.token_profitable_net_buy_60s_usd / denom * 100
        )
        f["active_smart_wallets_60s"] = Decimal(s.smart.active_smart_wallets_60s)
    else:
        f["local_smart_net_buy_60s_as_pct_of_volume_5m"] = None
        f["token_profitable_net_buy_60s_as_pct_of_volume_5m"] = None
        f["active_smart_wallets_60s"] = None

    return f


# ---------------------------------------------------------------------------
# 6.5 评分（正向/反向、缺失动作）
# ---------------------------------------------------------------------------

REVERSE_FEATURES = {
    "fdv_liquidity_ratio",
    "top10_holding_pct",
    "developer_or_creator_holding_pct",
}


@dataclass
class ScoringResult:
    total_score: Decimal
    dimension_scores: dict[str, Decimal]
    feature_scores: dict[str, Decimal]
    missing_applied: dict[str, str] = field(default_factory=dict)
    rejected: bool = False
    capped_setup: bool = False


def score(
    features: dict[str, Decimal | None], chain_strategy
) -> ScoringResult:
    dimensions = chain_strategy.scoring.dimensions
    dimension_scores: dict[str, Decimal] = {}
    feature_scores: dict[str, Decimal] = {}
    missing_applied: dict[str, str] = {}
    rejected = False
    capped_setup = False

    for dim_name, dim in dimensions.items():
        dim_total = Decimal(0)
        for feat_name, cfg in dim.features.items():
            value = features.get(feat_name)
            if value is None:
                missing_applied[feat_name] = cfg.missing_action
                if cfg.missing_action == "REJECT":
                    rejected = True
                elif cfg.missing_action == "CAP_SETUP":
                    capped_setup = True
                feature_scores[feat_name] = Decimal(0)
                continue
            if feat_name in REVERSE_FEATURES:
                raw = (cfg.bad - value) / (cfg.bad - cfg.good)
            else:
                raw = (value - cfg.bad) / (cfg.good - cfg.bad)
            clamped = max(Decimal(0), min(Decimal(1), raw))
            score_value = clamped * 100
            feature_scores[feat_name] = _q(score_value)
            dim_total += score_value * cfg.weight
        dimension_scores[dim_name] = _q(dim_total)

    total = sum(
        (dimension_scores[name] * dim.weight for name, dim in dimensions.items()),
        Decimal(0),
    )
    return ScoringResult(
        total_score=_q(total),
        dimension_scores=dimension_scores,
        feature_scores=feature_scores,
        missing_applied=missing_applied,
        rejected=rejected,
        capped_setup=capped_setup,
    )


def signal_level(
    scoring: ScoringResult, chain_strategy
) -> Literal["WATCHING", "SETUP", "BUY"]:
    cfg = chain_strategy.scoring
    if scoring.total_score >= cfg.buy_score_min and not scoring.capped_setup:
        return "BUY"
    if scoring.total_score >= cfg.setup_score_min or (
        scoring.total_score >= cfg.setup_score_min and scoring.capped_setup
    ):
        return "SETUP"
    return "WATCHING"


# ---------------------------------------------------------------------------
# 6.4/7.1 候选状态机与 signal_generation
# ---------------------------------------------------------------------------


@dataclass
class CandidateRuntime:
    status: str = "WATCHING"
    setup_started_at: int | None = None
    generation: int = 0
    generation_signal_emitted: bool = False
    below_setup_since: int | None = None


@dataclass
class CandidateDecision:
    state: CandidateRuntime
    emit_signal: bool = False
    reason: str | None = None


def evaluate_candidate(
    state: CandidateRuntime,
    *,
    now_ms: int,
    total_score: Decimal,
    level: str,
    anti_allowed: bool,
    security_passed: bool,
    config,
) -> CandidateDecision:
    """候选状态迁移（文档第 7.1 节 + 第 4.3 节 signal_generation 规则）。

    返回新状态与是否发信号。重启时从 SQLite 恢复 state（status/setup_started_at/
    generation），持续满足条件不能重复推送（generation_signal_emitted）。
    """
    setup_min = config.scoring.setup_score_min
    buy_min = config.scoring.buy_score_min

    new = CandidateRuntime(
        status=state.status,
        setup_started_at=state.setup_started_at,
        generation=state.generation,
        generation_signal_emitted=state.generation_signal_emitted,
        below_setup_since=state.below_setup_since,
    )

    # 重新武装：连续低于 setup_score_min 达到 signal_rearm_below_setup_seconds
    if total_score < setup_min:
        new.status = "WATCHING"
        new.setup_started_at = None
        if new.below_setup_since is None:
            new.below_setup_since = now_ms
        elif now_ms - new.below_setup_since >= config.signals.signal_rearm_below_setup_seconds * 1000:
            new.generation += 1
            new.generation_signal_emitted = False
            new.below_setup_since = now_ms
        return CandidateDecision(new, reason="below_setup")

    # 得分回升：清空 below 计时
    new.below_setup_since = None
    if new.generation == 0:
        new.generation = 1

    if level == "SETUP" and new.status == "WATCHING":
        new.status = "SETUP"
        new.setup_started_at = now_ms
        return CandidateDecision(new, reason="enter_setup")

    if level == "BUY" and new.status != "SIGNAL":
        if new.setup_started_at is None:
            new.setup_started_at = now_ms
        confirmed = (
            now_ms - new.setup_started_at
            >= config.signals.setup_confirmation_seconds * 1000
        )
        if confirmed and anti_allowed and security_passed:
            if new.generation_signal_emitted:
                return CandidateDecision(new, reason="generation_already_emitted")
            new.status = "SIGNAL"
            new.generation_signal_emitted = True
            return CandidateDecision(new, emit_signal=True, reason="signal")

    if level == "BUY" and not (anti_allowed and security_passed):
        new.status = "SETUP"
        return CandidateDecision(new, reason="gates_blocked")

    return CandidateDecision(new, reason="keep")


# ---------------------------------------------------------------------------
# 运行编排：候选 → G3 FOCUS → 评分 → 信号（strategy_task 核心，文档第 3.2 节）
# ---------------------------------------------------------------------------


class SignalPipeline:
    """把 G4-G6 组件串成实时信号闭环（文档第 3.2、4.4 节）。"""

    AGGREGATE_WINDOW_MS = 8_000  # 同代币多池聚合窗口
    MAX_INFLIGHT = 4             # 深查并发上限（GoPlus 30/min 硬约束下防堆积）
    NEW_POOL_MAX_INFLIGHT = 1    # new_pool 最多占用 1 个并发槽（文档 §4.3 的 90/10 容量）
    LABEL_PRIORITY = {"hot": 0, "anomaly": 1, "new_pool": 2}

    # 阶段计数（诊断：暴露在 /status）
    STAGES = (
        "seen",
        "trusted_quote_fail",
        "trusted_quote_fail",
        "security_fail",
        "market_quality_fail",
        "g3_incomplete",
        "score_below_setup",
        "anti_chase_block",
        "setup_waiting",
        "pre_exec_fail",
        "signaled",
    )

    def __init__(
        self,
        *,
        chain: str,
        strategy,
        repo,
        cg,
        gp,
        ws,
        candle_buffer,
        pipeline,
        bus,
        admin,
        signal_chat_id: int,
        active_snapshot,
        top_pool_lookup=None,
    ) -> None:
        self.chain = chain
        self.strategy = strategy
        self._repo = repo
        self._cg = cg
        self._gp = gp
        self._ws = ws
        self._candles = candle_buffer
        self._pipeline = pipeline  # CandidatePipeline
        self._bus = bus
        self._admin = admin
        self._signal_chat = signal_chat_id  # 信号频道（文档第 8.1 节推送目标）
        self._active = active_snapshot  # (revision, hash)
        self._top_pool_lookup = top_pool_lookup  # (chain, token) -> list[PoolAttributes]
        self._pending: dict[str, dict] = {}
        self._processing: set[str] = set()
        self._inflight_priority: dict[str, int] = {}
        self._new_pool_inflight = 0
        self._dispatcher: asyncio.Task | None = None
        self.stages: dict[str, int] = {name: 0 for name in self.STAGES}

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(
                self._dispatch_loop(), name=f"pipeline-{self.chain}"
            )

    def _bump(self, stage: str) -> None:
        self.stages[stage] = self.stages.get(stage, 0) + 1

    async def _dispatch_loop(self) -> None:
        """优先级调度：hot > anomaly > new_pool；并发受 MAX_INFLIGHT 与
        new_pool 容量槽位约束（文档第 4.3 节 90/10 容量规则）。"""
        try:
            while True:
                now = utc_now_ms()
                due = [
                    (token, entry)
                    for token, entry in self._pending.items()
                    if now >= entry["deadline"]
                ]
                if not due:
                    await asyncio.sleep(0.2)
                    continue
                due.sort(key=lambda te: (te[1]["priority"], te[0]))
                free = self.MAX_INFLIGHT - len(self._processing)
                for token, entry in due:
                    if free <= 0:
                        break
                    if token in self._processing:
                        continue
                    if entry["priority"] == 2 and self._new_pool_inflight >= self.NEW_POOL_MAX_INFLIGHT:
                        continue
                    self._pending.pop(token, None)
                    self._processing.add(token)
                    self._inflight_priority[token] = entry["priority"]
                    if entry["priority"] == 2:
                        self._new_pool_inflight += 1
                    free -= 1
                    asyncio.create_task(self._process_token(token, entry["pools"]))
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    async def handle_pool(self, pool, label) -> None:
        """发现回调：按代币聚合多池，聚合窗口后按优先级统一处理（文档第 4.4 节）。"""
        if self._admin is not None and getattr(self._admin, "paused", False):
            return
        if not (pool.base_token_address and pool.address):
            return
        token = pool.base_token_address
        self._bump("seen")
        # 候选在发现准入时即落库（WATCHING + 标签），管线只更新状态（文档第 7.1 节）
        if label is not None:
            try:
                await self._repo.upsert_candidate(
                    self.chain,
                    token,
                    pool.address,
                    status="WATCHING",
                    labels=[label.main] if label.main else [],
                    main_label=label.main or None,
                    expires_at=(
                        utc_now_ms()
                        + self.strategy.discovery.candidate_ttl_seconds * 1000
                    ),
                )
            except Exception:
                log.exception("候选落库失败 %s", token)
        entry = self._pending.get(token)
        if entry is None:
            entry = {
                "pools": [],
                "priority": self.LABEL_PRIORITY.get(label.main if label else "", 2),
                "deadline": utc_now_ms() + self.AGGREGATE_WINDOW_MS,
            }
            self._pending[token] = entry
            self._ensure_dispatcher()
        entry["pools"].append(pool)
        if label and label.main in self.LABEL_PRIORITY:
            entry["priority"] = min(entry["priority"], self.LABEL_PRIORITY[label.main])

    async def _process_token(self, token: str, pools) -> None:
        try:
            await self._process_candidates(token, pools)
        except Exception:
            log.exception("候选处理异常 %s", token)
        finally:
            self._processing.discard(token)
            priority = self._inflight_priority.pop(token, 2)
            if priority == 2:
                self._new_pool_inflight = max(0, self._new_pool_inflight - 1)

    async def _process_candidates(self, token: str, pools) -> None:
        # 1. 可信报价过滤 + G1 顶部池映射合并 + 地址去重（文档第 4.4 节）
        candidates: dict = {}
        for pool in pools:
            if self._trusted_quote(pool):
                candidates[pool.address] = pool
        if self._top_pool_lookup is not None:
            try:
                mapped = self._top_pool_lookup(self.chain, token)
            except Exception:
                mapped = []
            for pool in mapped or []:
                if self._trusted_quote(pool):
                    candidates.setdefault(pool.address, pool)
        if not candidates:
            self._bump("trusted_quote_fail")
            return  # 文档第 4.4 节：无可信报价资产池仅观察
        addresses = list(candidates)

        # 2. 深查（token 级一次；池级批量快照）
        result = await self._pipeline.deep_check(token, addresses)
        if result.errors.get("token_info"):
            return
        if result.security is None or not result.security.passed:
            self._bump("security_fail")
            return

        # 3. 并行 G3 FOCUS：全部准入池（每个精确池独立占订阅与计费）
        focus_ms = self.strategy.collection.websocket.focus_seconds_max * 1000
        subscribed = [a for a in addresses if self._ws.add_g3_pool(a, "base")]
        try:
            windows: dict[str, list] = {}
            deadline = utc_now_ms() + focus_ms
            while utc_now_ms() < deadline and len(windows) < len(subscribed):
                for addr in subscribed:
                    window = self._candles.window(addr, "base", 10)
                    if window is not None:
                        windows.setdefault(addr, window)
                await asyncio.sleep(0.25)
            if not windows:
                self._bump("g3_incomplete")
                return  # INCOMPLETE：全部池缺秒或不足 10 根

            # 4. 两份快照本地接收时间相隔至少 10 秒（文档第 4.4 节）
            if result.first_snapshot_at_ms is not None:
                wait_ms = result.first_snapshot_at_ms + 10_000 - utc_now_ms()
                if wait_ms > 0:
                    await asyncio.sleep(wait_ms / 1000)
            try:
                details = await self._cg.pools_multi(
                    self.chain, subscribed, include_volume_breakdown=True
                )
            except Exception as exc:
                log.warning("第二份快照失败 %s: %s", token, exc)
                return
            detail_map = {d.address: d for d in details}

            # 5. 逐池评分（含聪明钱）；市场质量硬门禁先行（文档第 6.4 节）
            scored = []
            for addr, window in windows.items():
                detail = detail_map.get(addr)
                if detail is None or detail.reserve_in_usd is None:
                    continue
                if not passes_market_quality(detail, self.strategy.market_quality):
                    self._bump("market_quality_fail")
                    continue
                snapshot = self._build_snapshot(token, addr, window, detail, result)
                snapshot.smart = await self._eval_smart_money(token, addr, detail, result)
                features = compute_features(snapshot)
                scoring = score(features, self.strategy)
                if scoring.rejected:
                    continue
                scored.append((addr, window, detail, snapshot, scoring))

            # 6. 池选择屏障：trade_allowed > 总分 > 有效流动性 > 5m 成交量 > 地址
            chosen = decide_pool(
                [(entry[2], entry[4].total_score) for entry in scored],
                liquidity={
                    entry[0]: entry[2].reserve_in_usd or Decimal(0) for entry in scored
                },
                volume_5m={
                    entry[0]: entry[2].volume_usd.get("m5") or Decimal(0)
                    for entry in scored
                },
            )
            if chosen is None:
                return
            entry = next(e for e in scored if e[0] == chosen)
            pool_address, window, detail, snapshot, scoring = entry

            # 存活者诊断日志（通过安全+市场质量+G3 的候选）
            if scoring.total_score >= self.strategy.scoring.watch_score_min:
                log.info(
                    "评分 %s %s 池=%s: 总分=%s 维度=%s",
                    self.chain,
                    token[:10],
                    pool_address[:10],
                    scoring.total_score,
                    {k: str(v) for k, v in scoring.dimension_scores.items()},
                )

            # 7. 防追高：决定池判定；触发等待后保持订阅续评至 max_wait_seconds
            anti = anti_chase_check(window, window, self.strategy.anti_chase)
            if anti.wait_triggered and not anti.allowed:
                wait_deadline = utc_now_ms() + self.strategy.anti_chase.max_wait_seconds * 1000
                while utc_now_ms() < wait_deadline:
                    await asyncio.sleep(0.25)
                    wait_bars = self._candles.final_bars(pool_address, "base")
                    anti = anti_chase_check(window, wait_bars, self.strategy.anti_chase)
                    if anti.allowed:
                        break
            if not anti.allowed:
                self._bump("anti_chase_block")
                return

            # 8. 候选状态机 + 信号
            features = compute_features(snapshot)
            level = signal_level(scoring, self.strategy)
            state = await self._load_state(token)
            decision = evaluate_candidate(
                state,
                now_ms=utc_now_ms(),
                total_score=scoring.total_score,
                level=level,
                anti_allowed=anti.allowed,
                security_passed=True,
                config=self.strategy,
            )
            await self._save_state(token, pool_address, decision.state)
            if not decision.emit_signal:
                if scoring.total_score < self.strategy.scoring.setup_score_min:
                    self._bump("score_below_setup")
                else:
                    self._bump("setup_waiting")
                return

            # PRE_EXECUTION_CHECK（120 秒快照；主池随检查传递，Top10 排除 LP 池）
            pre_exec = await self._pipeline._security_check(
                token,
                result.token_info,
                result.holders,
                kind="PRE_EXECUTION_CHECK",
                main_pool_address=pool_address,
            )
            if pre_exec is None or not pre_exec.passed:
                self._bump("pre_exec_fail")
                return
            signal_id = await create_signal(
                self._repo,
                chain=self.chain,
                token_address=token,
                pool_address=pool_address,
                signal_level_value="BUY",
                scoring=scoring,
                features=features,
                reference_price=window[-1].close,
                anti=anti,
                security=result.security,
                pre_execution=pre_exec,
                smart=SmartMoneyEval(snapshot=snapshot.smart, sell_blocked=False),
                generation=decision.state.generation,
                strategy_revision=self._active[0],
                strategy_hash=self._active[1],
                validity_seconds=self.strategy.signals.validity_seconds,
                now_ms=utc_now_ms(),
            )
            self._bump("signaled")
            await self._bus.publish_signal_created(signal_id)
            # Telegram 买入信号推送到信号频道（文档第 8.1 节）
            row = await self._repo.get_signal(signal_id)
            text = _signal_text(row)
            await self._repo.insert_outbox(
                delivery_key=f"sig:{signal_id}",
                kind="BUY_SIGNAL",
                parent_id=signal_id,
                chat_target=str(self._signal_chat),
                text=text,
            )
        finally:
            for addr in subscribed:
                self._ws.remove_g3_pool(addr, "base")

    async def _eval_smart_money(
        self, token: str, pool_address: str, detail, result
    ) -> SmartMoneySnapshot:
        """聪明钱 v1（文档第 4.4.1 节）：TOKEN_PROFITABLE 分类与净买入。"""
        try:
            traders = await self._cg.top_traders(self.chain, token, traders=20)
            trades = await self._cg.pool_trades(
                self.chain, pool_address, token=token
            )
        except Exception as exc:
            log.warning("聪明钱取证失败 %s: %s", token, exc)
            return SmartMoneySnapshot(complete=False)

        values = result.security.values if result.security else {}
        exclude = set(values.get("creator_owner_addresses") or [])
        exclude.add(pool_address)
        if self.chain == "bsc":
            exclude = {a.lower() for a in exclude}
        profitable = {
            t.address
            for t in traders.traders
            if t.realized_pnl_usd is not None and t.realized_pnl_usd > 0
        }
        norm = str.lower if self.chain == "bsc" else (lambda a: a)
        profitable = {a for a in profitable if norm(a) not in exclude}

        window_seconds = self.strategy.smart_money.recent_trade_window_seconds
        cutoff_ms = utc_now_ms() - window_seconds * 1000
        trade_dicts = []
        for trade in trades:
            if trade.block_timestamp_ms is None or trade.block_timestamp_ms < cutoff_ms:
                continue
            trade_dicts.append(
                {
                    "tx_from_address": trade.tx_from_address,
                    "kind": trade.kind,
                    "volume_in_usd": str(trade.volume_in_usd)
                    if trade.volume_in_usd is not None
                    else None,
                }
            )
        volume_5m = detail.volume_usd.get("m5") or Decimal(0)
        evaluated = eval_smart_money(
            token_profitable_wallets=profitable,
            local_smart_wallets=set(),  # v1：LOCAL_SMART 未启用
            trades=trade_dicts,
            recent_window_seconds=window_seconds,
            volume_5m_usd=volume_5m,
            min_confirming_wallets=self.strategy.smart_money.min_confirming_wallets,
            sell_block_net_volume_pct_min=(
                self.strategy.smart_money.sell_block_net_volume_pct_min
            ),
        )
        return evaluated.snapshot

    def _trusted_quote(self, pool) -> bool:
        """可信报价资产门禁：base/quote 任一侧命中该链 trusted_quote_assets（文档第 4.4 节）。"""
        assets = self.strategy.market_quality.trusted_quote_assets
        allowed = {a.address for a in assets}
        if self.chain == "bsc":
            allowed = {a.lower() for a in allowed}

        def _match(address):
            if not address:
                return False
            norm = address.lower() if self.chain == "bsc" else address
            return norm in allowed

        return _match(pool.quote_token_address) or _match(pool.base_token_address)

    def _build_snapshot(self, token, pool_address, window, detail, result) -> CandidateSnapshot:
        from app.core.clients.coingecko import TxBucket as _TB

        m5 = detail.transactions.get("m5")
        m15 = detail.transactions.get("m15")
        values = result.security.values if result.security else {}
        return CandidateSnapshot(
            chain=self.chain,
            token_address=token,
            pool_address=pool_address,
            w5=window[-5:],
            w10=window,
            pool=PoolSnapshot(
                reserve_usd=detail.reserve_in_usd,
                fdv_usd=detail.fdv_usd,
                volume_m5_usd=detail.volume_usd.get("m5"),
                volume_m15_usd=detail.volume_usd.get("m15"),
                tx_m5=_TB(buys=m5.buys, sells=m5.sells, buyers=m5.buyers, sellers=m5.sellers) if m5 else None,
                tx_m15=_TB(buys=m15.buys, sells=m15.sells, buyers=m15.buyers, sellers=m15.sellers) if m15 else None,
                price_change_5m_pct=detail.price_change_pct.get("m5"),
            ),
            gt_score=result.token_info.gt_score if result.token_info else None,
            gt_verified=result.token_info.gt_verified if result.token_info else None,
            holder_count=result.token_info.holder_count if result.token_info else None,
            top10_holding_pct=_as_decimal(values.get("top10_holding_pct")),
            developer_or_creator_holding_pct=_as_decimal(
                values.get("developer_or_creator_holding_pct")
            ),
            lp_locked_pct=_as_decimal(values.get("lp_locked_pct")),
        )

    async def _load_state(self, token: str) -> CandidateRuntime:
        row = await self._repo.get_candidate(self.chain, token)
        if row is None:
            return CandidateRuntime()
        return CandidateRuntime(
            status=row["status"],
            setup_started_at=row["setup_started_at"],
            generation=int(row["signal_generation"]),
            generation_signal_emitted=(row["status"] == "SIGNAL"),
            below_setup_since=row["below_setup_since"],
        )

    async def _save_state(self, token: str, pool_address: str, state: CandidateRuntime) -> None:
        await self._repo.upsert_candidate(
            self.chain,
            token,
            pool_address,
            status=state.status,
            signal_generation=state.generation,
            setup_started_at=state.setup_started_at,
            below_setup_since=state.below_setup_since,
            expires_at=utc_now_ms() + self.strategy.discovery.candidate_ttl_seconds * 1000,
        )


def _field_decimal(field) -> Optional[Decimal]:
    if field is None:
        return None
    detail = getattr(field, "detail", None)
    try:
        return Decimal(str(detail)) if detail else None
    except Exception:
        return None


def _as_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _signal_text(row) -> str:
    from app.core.services.admin import build_signal_message

    return build_signal_message(row)

# ---------------------------------------------------------------------------
# 10.6 策略激活与回退（手动决策）
# ---------------------------------------------------------------------------

import uuid as _uuid2  # noqa: E402


class StrategyActivationService:
    def __init__(self, repo) -> None:
        self._repo = repo

    async def current_active(self) -> dict | None:
        cursor = await self._repo.conn.execute(
            "SELECT * FROM strategy_snapshots WHERE is_active = 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def activate(
        self, strategy, *, admin_id: int, change_reason_override: str | None = None
    ) -> str:
        """手动激活（第 10.6 节）：revision 不复用、parent 指向当前 ACTIVE。"""
        current = await self.current_active()
        if current is not None and strategy.revision == current["revision"]:
            raise ValueError(f"revision 已使用: {strategy.revision}")
        if strategy.parent_revision is None and current is not None:
            raise ValueError("parent_revision 必须等于当前 ACTIVE 的 revision")
        if current is not None and strategy.parent_revision != current["revision"]:
            raise ValueError(
                f"parent_revision={strategy.parent_revision} 与当前 ACTIVE "
                f"{current['revision']} 不一致"
            )
        activation_id = _uuid2.uuid4().hex
        canonical = canonical_json(strategy.model_dump(mode="json", by_alias=True))
        await self._repo.conn.execute("BEGIN")
        try:
            await self._repo.conn.execute(
                "UPDATE strategy_snapshots SET is_active = 0, activated_at = NULL"
            )
            await self._repo.conn.execute(
                """INSERT INTO strategy_snapshots
                   (revision, parent_revision, strategy_hash, canonical,
                    change_reason, is_active, activated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (
                    strategy.revision,
                    strategy.parent_revision,
                    strategy.strategy_hash,
                    canonical,
                    change_reason_override or strategy.change_reason,
                    utc_now_ms(),
                ),
            )
            await self._repo.conn.execute(
                """INSERT INTO strategy_activations
                   (activation_id, kind, before_revision, after_revision,
                    before_hash, after_hash, admin_id, created_at)
                   VALUES (?, 'ACTIVATE', ?, ?, ?, ?, ?, ?)""",
                (
                    activation_id,
                    current["revision"] if current else None,
                    strategy.revision,
                    current["strategy_hash"] if current else None,
                    strategy.strategy_hash,
                    admin_id,
                    utc_now_ms(),
                ),
            )
            await self._repo.conn.commit()
        except Exception:
            await self._repo.conn.rollback()
            raise
        return activation_id

    async def rollback(self, revision: str, *, admin_id: int) -> str:
        """回退：重新激活历史快照原 revision/hash，不伪造新 revision。"""
        cursor = await self._repo.conn.execute(
            "SELECT * FROM strategy_snapshots WHERE revision = ?", (revision,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"历史快照不存在: {revision}")
        current = await self.current_active()
        if current is None:
            raise ValueError("无 ACTIVE 快照")
        if current["revision"] == revision:
            raise ValueError("目标 revision 已是 ACTIVE")
        activation_id = _uuid2.uuid4().hex
        await self._repo.conn.execute("BEGIN")
        try:
            await self._repo.conn.execute(
                "UPDATE strategy_snapshots SET is_active = 0, activated_at = NULL"
            )
            await self._repo.conn.execute(
                "UPDATE strategy_snapshots SET is_active = 1, activated_at = ? "
                "WHERE revision = ?",
                (utc_now_ms(), revision),
            )
            await self._repo.conn.execute(
                """INSERT INTO strategy_activations
                   (activation_id, kind, before_revision, after_revision,
                    before_hash, after_hash, admin_id, created_at)
                   VALUES (?, 'ROLLBACK', ?, ?, ?, ?, ?, ?)""",
                (
                    activation_id,
                    current["revision"],
                    revision,
                    current["strategy_hash"],
                    row["strategy_hash"],
                    admin_id,
                    utc_now_ms(),
                ),
            )
            await self._repo.conn.commit()
        except Exception:
            await self._repo.conn.rollback()
            raise
        return activation_id

# ---------------------------------------------------------------------------
# 6.5/7.2 信号创建（决策快照 + 落库）
# ---------------------------------------------------------------------------

import json as _json
import uuid as _uuid

from app.core.services.security import SecurityResult  # noqa: E402


def _dec_str(value):
    if value is None:
        return None  # 缺失特征序列化为 null（回放解析兼容）
    return str(value)


def build_decision_snapshot(
    *,
    features: dict[str, Decimal | None],
    scoring: ScoringResult,
    anti: AntiChaseResult,
    security: SecurityResult,
    smart: SmartMoneyEval,
    chain: str,
    token_address: str,
    pool_address: str,
) -> dict:
    """完整规范化决策特征向量（文档第 9.4 节：候选与信号决策快照）。"""
    return {
        "chain": chain,
        "token_address": token_address,
        "pool_address": pool_address,
        "features": {k: _dec_str(v) for k, v in features.items()},
        "scoring": {
            "total_score": _dec_str(scoring.total_score),
            "dimension_scores": {
                k: _dec_str(v) for k, v in scoring.dimension_scores.items()
            },
            "feature_scores": {
                k: _dec_str(v) for k, v in scoring.feature_scores.items()
            },
            "missing_applied": scoring.missing_applied,
            "rejected": scoring.rejected,
            "capped_setup": scoring.capped_setup,
        },
        "anti_chase": {
            "allowed": anti.allowed,
            "wait_triggered": anti.wait_triggered,
            "reason": anti.reason,
        },
        "security": {
            "trade_allowed": security.passed,
            "fields": {
                k: {"status": v.status, "reason_code": v.reason_code}
                for k, v in security.fields.items()
            },
        },
        "smart_money": {
            "complete": smart.snapshot.complete,
            "sell_blocked": smart.sell_blocked,
        },
    }


async def create_signal(
    repo,
    *,
    chain: str,
    token_address: str,
    pool_address: str,
    signal_level_value: str,
    scoring: ScoringResult,
    features: dict[str, Decimal | None],
    reference_price: Decimal,
    anti: AntiChaseResult,
    security: SecurityResult,
    pre_execution: SecurityResult,
    smart: SmartMoneyEval,
    generation: int,
    strategy_revision: str,
    strategy_hash: str,
    validity_seconds: int,
    now_ms: int,
) -> str:
    """信号落库（文档第 7.2 节）。返回 signal_id。"""
    signal_id = _uuid.uuid4().hex
    decision_snapshot = build_decision_snapshot(
        features=features,
        scoring=scoring,
        anti=anti,
        security=security,
        smart=smart,
        chain=chain,
        token_address=token_address,
        pool_address=pool_address,
    )
    security_snapshot = {
        "pass": pre_execution.passed,
        "trade_allowed": security.passed,
    }
    expires_at = now_ms + validity_seconds * 1000
    await repo.insert_signal(
        signal_id=signal_id,
        chain=chain,
        token_address=token_address,
        pool_address=pool_address,
        signal_level=signal_level_value,
        total_score=_dec_str(scoring.total_score),
        reference_price=_dec_str(reference_price),
        signal_generation=generation,
        strategy_revision=strategy_revision,
        strategy_hash=strategy_hash,
        security_snapshot=security_snapshot,
        decision_snapshot=decision_snapshot,
        expires_at=expires_at,
    )
    return signal_id


# ---------------------------------------------------------------------------
# 6.6 防追高状态机
# ---------------------------------------------------------------------------


@dataclass
class AntiChaseResult:
    allowed: bool
    wait_triggered: bool = False
    reason: str | None = None


def _typical_price(c: CandleBar) -> Decimal:
    return (c.high + c.low + c.close) / 3


def anti_chase_check(
    w10: list[CandleBar] | None,
    wait_window: list[CandleBar] | None,
    config,
) -> AntiChaseResult:
    """防追高判定（文档第 6.6 节确定性状态机）。

    - w10：初始连续 10 根 final K 线。
    - wait_window：等待开始后的连续 K 线（含初始窗口），用于高位整理/回踩恢复。
    """
    if not w10 or len(w10) < 10:
        return AntiChaseResult(False, reason="窗口不完整")

    vertical_rise = max(Decimal(0), (w10[-1].close / w10[0].open - 1) * 100)
    total_volume = sum((c.volume for c in w10), Decimal(0))
    vwap_deviation = None
    if total_volume > 0:
        vwap = sum((_typical_price(c) * c.volume for c in w10), Decimal(0)) / total_volume
        vwap_deviation = max(Decimal(0), (w10[-1].close / vwap - 1) * 100)

    if (
        vertical_rise <= config.vertical_rise_10s_wait_pct
        and (
            vwap_deviation is None
            or vwap_deviation <= config.vwap_10s_deviation_wait_pct
        )
    ):
        return AntiChaseResult(True)

    # 触发等待：检查高位整理与回踩恢复
    if not wait_window or len(wait_window) < 10:
        return AntiChaseResult(False, wait_triggered=True, reason="等待窗口不足")

    high = max(c.high for c in wait_window)
    wait_started = wait_window[10:]
    drawdown = max(Decimal(0), (high - wait_window[-1].close) / high * 100)
    if drawdown > config.drawdown_from_10s_high_block_pct:
        return AntiChaseResult(False, wait_triggered=True, reason="回撤过大")

    # 高位整理
    if len(wait_started) >= config.consolidation_seconds_min:
        recent = wait_started[-config.consolidation_seconds_min :]
        range_pct = (max(c.high for c in recent) - min(c.low for c in recent)) / min(
            c.low for c in recent
        ) * 100
        pullback_from_high = (high - wait_window[-1].close) / high * 100
        if (
            range_pct <= config.consolidation_range_pct_max
            and pullback_from_high < config.pullback_min_pct
        ):
            return AntiChaseResult(True, wait_triggered=True)

    # 回踩恢复
    low_after = min(c.low for c in wait_started) if wait_started else min(c.low for c in wait_window)
    if high > low_after:
        pullback_depth = (high - low_after) / high * 100
        recovery = (wait_window[-1].close - low_after) / (high - low_after)
        if (
            config.pullback_min_pct <= pullback_depth <= config.pullback_max_pct
            and recovery >= config.pullback_recovery_ratio_min
        ):
            return AntiChaseResult(True, wait_triggered=True)

    return AntiChaseResult(False, wait_triggered=True, reason="等待超时未确认入场结构")


# ---------------------------------------------------------------------------
# 6.2 聪明钱 v1：TOKEN_PROFITABLE 分类与卖出阻挡
# ---------------------------------------------------------------------------


@dataclass
class SmartMoneyEval:
    snapshot: SmartMoneySnapshot
    sell_blocked: bool = False
    block_reason: str | None = None


def eval_smart_money(
    *,
    token_profitable_wallets: set[str],
    local_smart_wallets: set[str],
    trades: list[dict],
    recent_window_seconds: int,
    volume_5m_usd: Decimal,
    min_confirming_wallets: int,
    sell_block_net_volume_pct_min: Decimal,
) -> SmartMoneyEval:
    """按排他分类计算 60 秒净买入（v1：LOCAL_SMART 恒为空集，特征权重为 0）。

    trades: [{tx_from_address, kind, volume_in_usd, block_timestamp_ms}]，
    已截断到 recent_trade_window_seconds 内。
    """
    tp_net: dict[str, Decimal] = {}
    ls_net: dict[str, Decimal] = {}

    for trade in trades:
        address = trade.get("tx_from_address")
        kind = trade.get("kind")
        volume = trade.get("volume_in_usd")
        if not address or kind not in ("buy", "sell") or volume is None:
            continue
        signed = Decimal(volume) if kind == "buy" else -Decimal(volume)
        if address in local_smart_wallets:
            ls_net[address] = ls_net.get(address, Decimal(0)) + signed
        elif address in token_profitable_wallets:
            tp_net[address] = tp_net.get(address, Decimal(0)) + signed

    tp_buy = sum((v for v in tp_net.values() if v > 0), Decimal(0))
    tp_sell = -sum((v for v in tp_net.values() if v < 0), Decimal(0))
    ls_buy = sum((v for v in ls_net.values() if v > 0), Decimal(0))
    ls_sell = -sum((v for v in ls_net.values() if v < 0), Decimal(0))

    active_wallets = {a for a, v in tp_net.items() if v > 0} | {
        a for a, v in ls_net.items() if v > 0
    }
    snapshot = SmartMoneySnapshot(
        token_profitable_net_buy_60s_usd=tp_buy - tp_sell,
        local_smart_net_buy_60s_usd=ls_buy - ls_sell,
        active_smart_wallets_60s=len(active_wallets),
        complete=True,
    )

    # 卖出阻挡：至少 min_confirming_wallets 个 LOCAL_SMART 净卖出钱包，
    # 且 60 秒净卖出达到 5 分钟成交量的 sell_block_net_volume_pct_min。
    net_sellers = [a for a, v in ls_net.items() if v < 0]
    sell_blocked = False
    if (
        len(net_sellers) >= min_confirming_wallets
        and ls_sell - ls_buy > 0
        and volume_5m_usd > 0
        and (ls_sell - ls_buy) / volume_5m_usd * 100 >= sell_block_net_volume_pct_min
    ):
        sell_blocked = True

    return SmartMoneyEval(snapshot=snapshot, sell_blocked=sell_blocked)
