"""统一发现调度、标签与候选深查流水线（总控文档第 4.2-4.4 节）。

- 每条链一个调度器：四个查询模板按独立 next_due_at 调度（持久化到
  discovery_schedule 表），只执行到期模板。
- 标签：hot / anomaly / new_pool（纯函数，公式见文档第 4.3 节）。
- 深查流水线按固定依赖图执行，PRE_MONITOR_CHECK 成功快照 TTL 15 分钟可复用。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from app.core.clients.coingecko import (
    CoinGeckoClient,
    PoolAttributes,
    PoolDetail,
    TokenInfo,
    TopHolders,
)
from app.core.clients.goplus import EvmSecurity, GoPlusClient, GoPlusRateLimited, SolSecurity
from app.core.config import ChainStrategy, MegafilterTemplate
from app.core.models import utc_now_ms
from app.core.services.security import (
    SecurityResult,
    apply_snapshot_ttl,
    cg_pregate_bsc,
    cg_pregate_solana,
    evaluate_bsc,
    evaluate_solana,
)
from app.core.storage.repository import Repository

log = logging.getLogger(__name__)

MEGAFILTER_HOT_SORTS = {"m5_trending"}
HOT_TEMPLATE_ENDPOINTS = {"trending_pools"}

PRE_MONITOR_TTL_SECONDS = 15 * 60
PRE_EXECUTION_TTL_SECONDS = 120


# ---------------------------------------------------------------------------
# 标签与早期门禁（纯函数，文档第 4.3 节）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolLabel:
    hot: bool
    anomaly: bool
    new_pool: bool

    @property
    def main(self) -> str:
        if self.hot:
            return "hot"
        if self.anomaly:
            return "anomaly"
        return "new_pool"


def classify_pool(
    *,
    template: MegafilterTemplate,
    rank: int,
    received_at_ms: int,
    pool: PoolAttributes,
    channel: Any,
) -> PoolLabel:
    hot = False
    if template.endpoint == "trending_pools" or (
        template.endpoint == "megafilter" and template.sort in MEGAFILTER_HOT_SORTS
    ):
        hot = rank <= channel.hot.max_source_rank

    vol_ratio = _volume_rate_ratio(pool)
    buy_sell = _buy_sell_ratio(pool)
    price_change = pool.price_change_pct.get("m5")

    anomaly = False
    if (
        vol_ratio is not None
        and buy_sell is not None
        and vol_ratio >= channel.anomaly.volume_acceleration_ratio_min
        and buy_sell >= channel.anomaly.buy_sell_tx_ratio_min
    ):
        anomaly = True
    if (
        price_change is not None
        and abs(price_change) >= channel.anomaly.price_change_5m_abs_pct_min
    ):
        anomaly = True

    new_pool = template.endpoint == "new_pools"
    if pool.pool_created_at_ms is not None:
        age = received_at_ms - pool.pool_created_at_ms
        if 0 <= age <= channel.new_pool.pool_age_seconds_max * 1000:
            new_pool = True

    return PoolLabel(hot=hot, anomaly=anomaly, new_pool=new_pool)


def passes_early_gate(pool: PoolAttributes, collection) -> bool:
    """宽口径最低流动性与最低成交活跃度（文档第 4.2 节）。"""
    if pool.reserve_in_usd is None or pool.reserve_in_usd < collection.reserve_usd_floor:
        return False
    m5 = pool.transactions.get("m5")
    if m5 is None:
        return False
    if m5.buys + m5.sells < collection.tx_count_floor:
        return False
    return True


def passes_market_quality(detail: PoolDetail, mq) -> bool:
    """市场质量硬门禁（文档第 6.4 节），第二份快照后对每个准入池执行。"""
    if detail.reserve_in_usd is None or detail.reserve_in_usd < mq.reserve_usd_min:
        return False
    if detail.fdv_usd is not None and detail.fdv_usd > mq.fdv_usd_max:
        return False
    if (
        detail.fdv_usd is not None
        and detail.reserve_in_usd is not None
        and detail.reserve_in_usd > 0
        and detail.fdv_usd / detail.reserve_in_usd > mq.fdv_liquidity_ratio_max
    ):
        return False
    if detail.pool_created_at_ms is None:
        return False  # 池龄未知无法验证 → 不放行
    age_seconds = (utc_now_ms() - detail.pool_created_at_ms) // 1000
    if not (mq.pool_age_seconds_min <= age_seconds <= mq.pool_age_seconds_max):
        return False
    volume_5m = detail.volume_usd.get("m5")
    if volume_5m is None or volume_5m < mq.volume_5m_usd_min:
        return False
    m5 = detail.transactions.get("m5")
    if m5 is None:
        return False
    if m5.buys + m5.sells < mq.tx_count_5m_min:
        return False
    if m5.buys < mq.buys_5m_min or m5.sells < mq.sells_5m_min:
        return False
    if (
        m5.buyers < mq.independent_buyers_5m_min
        or m5.sellers < mq.independent_sellers_5m_min
    ):
        return False
    if Decimal(m5.buys) / Decimal(max(m5.sells, 1)) < mq.buy_sell_tx_ratio_5m_min:
        return False
    return True


def _volume_rate_ratio(pool: PoolAttributes) -> Decimal | None:
    m5 = pool.volume_usd.get("m5")
    m15 = pool.volume_usd.get("m15")
    if m5 is None or m15 is None or m15 <= m5:
        return None
    denom = (m15 - m5) / 10
    if denom <= 0:
        return None
    return (m5 / 5) / denom


def _buy_sell_ratio(pool: PoolAttributes) -> Decimal | None:
    m5 = pool.transactions.get("m5")
    if m5 is None or m5.sells <= 0:
        return None
    return Decimal(m5.buys) / Decimal(m5.sells)


def decide_pool(
    pools: list[tuple[PoolDetail | None, Decimal | None]],
    *,
    liquidity: dict[str, Decimal],
    volume_5m: dict[str, Decimal],
) -> str | None:
    """按 trade_allowed > 总分 > 有效流动性 > 5 分钟成交量 > pool_address 选择。

    pools: [(detail, total_score)]，detail 为 None 表示该池 INCOMPLETE/REJECT。
    """
    ranked = []
    for detail, score in pools:
        if detail is None or detail.address is None:
            continue
        ranked.append(
            (
                score if score is not None else Decimal("-Infinity"),
                liquidity.get(detail.address, Decimal(0)),
                volume_5m.get(detail.address, Decimal(0)),
                detail.address,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3]), reverse=True)
    return ranked[0][3]


# ---------------------------------------------------------------------------
# 模板调度器（文档第 4.2 节）
# ---------------------------------------------------------------------------


class TemplateScheduler:
    def __init__(self, chain: str, strategy_holder, repo: Repository) -> None:
        self.chain = chain
        self._strategy_holder = strategy_holder
        self.repo = repo

    @property
    def collection(self):
        return getattr(self._strategy_holder.get(), self.chain).collection


    async def due_templates(self, now_ms: int) -> list[MegafilterTemplate]:
        due = []
        for template in self.collection.query_templates:
            next_due = await self.repo.get_next_due(self.chain, template.id)
            if next_due is None or next_due <= now_ms:
                due.append(template)
        return due

    async def mark_done(self, template: MegafilterTemplate, now_ms: int) -> None:
        next_due = now_ms + template.poll_seconds * 1000
        await self.repo.upsert_schedule(self.chain, template.id, next_due)


# ---------------------------------------------------------------------------
# 候选深查流水线（文档第 4.4 节）
# ---------------------------------------------------------------------------

TokenScorer = Callable[[dict[str, Any]], Decimal | None]


@dataclass
class DeepCheckResult:
    chain: str
    token_address: str
    token_info: TokenInfo | None = None
    holders: TopHolders | None = None
    goplus_evm: EvmSecurity | None = None
    goplus_sol: SolSecurity | None = None
    security: SecurityResult | None = None
    security_kind: str | None = None
    pool_details: dict[str, PoolDetail] = field(default_factory=dict)
    first_snapshot_at_ms: int | None = None
    completed: bool = False
    errors: dict[str, str] = field(default_factory=dict)


class CandidatePipeline:
    """单一候选的固定依赖图深查（第 4.4 节），PRE_MONITOR_CHECK 可复用 15 分钟。"""

    def __init__(
        self,
        chain: str,
        strategy_holder,
        cg: CoinGeckoClient,
        gp: GoPlusClient,
        repo: Repository,
    ) -> None:
        self.chain = chain
        self._strategy_holder = strategy_holder
        self.cg = cg
        self.gp = gp
        self.repo = repo
        self._cache: dict[str, tuple[int, DeepCheckResult]] = {}

    @property
    def strategy(self) -> ChainStrategy:
        return getattr(self._strategy_holder.get(), self.chain)

    def _cache_ttl_ms(self) -> int:
        return self.strategy.discovery.rest_refresh_seconds * 1000

    async def deep_check(
        self,
        token_address: str,
        candidate_pool_addresses: list[str],
    ) -> DeepCheckResult:
        # 同一实体在 TTL 内复用缓存（文档第 4.4 节），避免每轮全量深查
        now = utc_now_ms()
        cached = self._cache.get(token_address)
        if cached is not None and now - cached[0] < self._cache_ttl_ms():
            return cached[1]

        result = DeepCheckResult(chain=self.chain, token_address=token_address)

        # 1. Token Info
        try:
            result.token_info = await self.cg.token_info(self.chain, token_address)
        except Exception as exc:
            result.errors["token_info"] = f"{type(exc).__name__}"
            return result

        # 2. Multiple Pools（第一份聚合快照）
        try:
            details = await self.cg.pools_multi(
                self.chain, candidate_pool_addresses, include_volume_breakdown=True
            )
            result.pool_details = {d.address: d for d in details}
            result.first_snapshot_at_ms = utc_now_ms()
        except Exception as exc:
            result.errors["pools_multi"] = f"{type(exc).__name__}"

        # 3. Top Holders（数量按链固定，文档第 4.4 节）
        try:
            holders_count = 40 if self.chain == "solana" else 50
            result.holders = await self.cg.top_holders(
                self.chain, token_address, holders=holders_count
            )
        except Exception as exc:
            result.errors["top_holders"] = f"{type(exc).__name__}"

        # 4. GoPlus PRE_MONITOR_CHECK（成功快照 15 分钟 TTL 可复用）
        main_pool = candidate_pool_addresses[0] if candidate_pool_addresses else None
        security = await self._security_check(
            token_address,
            result.token_info,
            result.holders,
            kind="PRE_MONITOR_CHECK",
            main_pool_address=main_pool,
        )
        if security is None:
            result.errors["security"] = "go_plus_unavailable"
            return result
        result.security = security

        # 5. 持久化安全快照与决策证据
        await _persist_security(self.repo, self.chain, token_address, result)

        result.completed = True
        self._cache[token_address] = (utc_now_ms(), result)
        return result

    async def _security_check(
        self,
        token_address: str,
        token_info: TokenInfo,
        holders: TopHolders,
        *,
        kind: str,
        main_pool_address: str | None = None,
    ) -> SecurityResult | None:
        if kind == "PRE_MONITOR_CHECK":
            row = await self.repo.latest_security_check(
                self.chain, token_address, "PRE_MONITOR_CHECK"
            )
            if row is not None and row["status"] == "PASS":
                age = (utc_now_ms() - int(row["created_at"])) / 1000
                if age <= PRE_MONITOR_TTL_SECONDS:
                    # 缓存命中：从 details 重建结构化数值（top10/lp/dev），
                    # 否则 holding_distribution 维度系统性失分
                    cached = SecurityResult(trade_allowed=True)
                    cached.set("cached_pass", "SAFE")
                    try:
                        details = json.loads(row["details"] or "{}")
                        for name, value in (details.get("values") or {}).items():
                            cached.set_value(
                                name,
                                Decimal(str(value)) if value is not None else None,
                            )
                    except Exception:
                        log.warning("安全缓存 values 解析失败 %s", token_address)
                    return cached

        try:
            if self.chain == "solana":
                # ②段：CoinGecko 本地硬门禁（不消耗 GoPlus）
                cg_gate = cg_pregate_solana(
                    token_info=token_info,
                    coin_holders=holders,
                    security_cfg=self.strategy.security,
                    main_pool_address=main_pool_address,
                )
                if not cg_gate.passed:
                    return cg_gate
                results = await self.gp.solana_token_security([token_address])
                raw = results.get(token_address)
                if raw is None:
                    return None
                security = evaluate_solana(
                    token_info=token_info,
                    goplus=raw,
                    security_cfg=self.strategy.security,
                    main_pool_address=main_pool_address,
                    coin_holders=holders,
                )
            else:
                cg_gate = cg_pregate_bsc(
                    token_info=token_info,
                    coin_holders=holders,
                    security_cfg=self.strategy.security,
                    main_pool_address=main_pool_address,
                )
                if not cg_gate.passed:
                    return cg_gate
                results = await self.gp.evm_token_security(56, [token_address])
                raw = results.get(token_address)
                if raw is None:
                    return None
                security = evaluate_bsc(
                    token_info=token_info,
                    goplus=raw,
                    security_cfg=self.strategy.security,
                    main_pool_address=main_pool_address,
                    coin_holders=holders,
                )
        except GoPlusRateLimited:
            # 业务限流：等待一个令牌周期后重试一次
            await asyncio.sleep(3)
            try:
                return await self._security_check(
                    token_address,
                    token_info,
                    holders,
                    kind=kind,
                    main_pool_address=main_pool_address,
                )
            except Exception:
                return None
        except Exception as exc:
            log.warning("GoPlus 检查失败 %s %s: %s", self.chain, token_address, exc)
            return None
        return security

# ---------------------------------------------------------------------------
# 发现任务（文档第 4.2 节：每模板独立调度，只执行到期模板）
# ---------------------------------------------------------------------------


class DiscoveryTask:
    def __init__(
        self,
        chain: str,
        strategy_holder,
        cg: CoinGeckoClient,
        scheduler: TemplateScheduler,
    ) -> None:
        self.chain = chain
        self._strategy_holder = strategy_holder
        self.cg = cg
        self.scheduler = scheduler
        self.on_pool: Optional[Callable[[PoolAttributes, PoolLabel, MegafilterTemplate], Awaitable[None]]] = None

    @property
    def strategy(self) -> ChainStrategy:
        return self._strategy_holder.get().model_dump() and getattr(
            self._strategy_holder.get(), self.chain
        )

    async def fetch_template(self, template: MegafilterTemplate) -> list[PoolAttributes]:
        """按模板执行一次采集（run 与 collection dry-run 共用）。"""
        return await self._fetch(template)

    async def run(self) -> int:
        """执行所有到期模板，返回本次新发现候选数。"""
        now = utc_now_ms()
        due = await self.scheduler.due_templates(now)
        found = 0
        for template in due:
            try:
                pools = await self._fetch(template)
                for rank, pool in enumerate(pools, start=1):
                    if not passes_early_gate(pool, self.strategy.collection):
                        continue
                    label = classify_pool(
                        template=template,
                        rank=rank,
                        received_at_ms=now,
                        pool=pool,
                        channel=self.strategy.discovery_channels,
                    )
                    found += 1
                    if self.on_pool is not None:
                        await self.on_pool(pool, label, template)
                await self.scheduler.mark_done(template, now)
            except Exception as exc:
                log.warning("发现模板 %s/%s 执行失败: %s", self.chain, template.id, exc)
                await self.scheduler.mark_done(template, now)  # 失败也顺延，避免紧追打
        return found

    async def _fetch(self, template: MegafilterTemplate) -> list[PoolAttributes]:
        query = dict(template.query)
        if template.endpoint == "megafilter":
            injected = {
                "reserve_in_usd_min": str(self.strategy.collection.reserve_usd_floor),
                "tx_count_min": str(self.strategy.collection.tx_count_floor),
                "tx_count_duration": self.strategy.collection.tx_count_duration,
            }
            query = {**injected, **query}
            query["sort"] = template.sort
            query.setdefault("include", "base_token,quote_token,dex,network")
            return await self.cg.megafilter(query, pages=template.pages)
        if template.endpoint == "trending_pools":
            network = str(query.pop("network", self.chain))
            query.setdefault("include", "base_token,quote_token,dex")
            page = int(query.pop("page", 1))
            duration = str(query.pop("duration", "5m"))
            return await self.cg.trending_pools(network, duration=duration, page=page, include=query.get("include", ""))
        if template.endpoint == "new_pools":
            network = str(query.pop("network", self.chain))
            query.setdefault("include", "base_token,quote_token,dex")
            page = int(query.pop("page", 1))
            return await self.cg.new_pools(network, page=page, include=query.get("include", ""))
        raise ValueError(f"未知模板端点: {template.endpoint}")


async def collection_dry_run(
    chain: str, strategy: ChainStrategy, cg: CoinGeckoClient
) -> dict:
    """validate-collection dry-run（第 10.6 节）：只执行模板请求，不深查/信号/事件。"""
    task = DiscoveryTask(chain, strategy, cg, TemplateScheduler(chain, strategy.collection, None))  # type: ignore[arg-type]
    report: dict[str, Any] = {"chain": chain, "templates": {}}
    for template in strategy.collection.query_templates:
        started = asyncio.get_event_loop().time()
        try:
            pools = await task.fetch_template(template)
            report["templates"][template.id] = {
                "ok": True,
                "count": len(pools),
                "latency_seconds": round(asyncio.get_event_loop().time() - started, 2),
            }
        except Exception as exc:
            report["templates"][template.id] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_seconds": round(asyncio.get_event_loop().time() - started, 2),
            }
    return report


async def _persist_security(
    repo: Repository, chain: str, token_address: str, result: "DeepCheckResult"
) -> None:
    status = "PASS" if (result.security and result.security.passed) else "RISK"
    details = {
        "fields": {
            k: v.status
            for k, v in (result.security.fields.items() if result.security else [])
        },
        "values": {
            k: str(v) if v is not None else None
            for k, v in (
                result.security.values.items() if result.security else []
            )
        },
    }
    await repo.insert_security_check(
        chain,
        token_address,
        "PRE_MONITOR_CHECK",
        status,
        details=details,
        expires_at=utc_now_ms() + PRE_MONITOR_TTL_SECONDS * 1000,
    )

