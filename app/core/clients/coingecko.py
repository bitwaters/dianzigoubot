"""CoinGecko REST client 与归一化模型（总控文档第 4 章）。

规则（文档第 4.1 节）：
- 所有响应在 client 层归一化；业务层不读取供应商原始结构。
- 地址按链规则规范化，金额和比例使用 Decimal，时间统一为 UTC 毫秒。
- HTTP 成功但业务数据缺失、类型错误或无法解析时视为无效（ContractError）。
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

BASE_URL = "https://pro-api.coingecko.com/api/v3"
DEFAULT_TIMEOUT = 20.0


class CoinGeckoError(RuntimeError):
    pass


class CoinGeckoTimeout(CoinGeckoError):
    pass


class CoinGeckoRateLimited(CoinGeckoError):
    pass


class CoinGeckoContractError(CoinGeckoError):
    """HTTP 成功但业务数据缺失、类型错误或无法解析（文档第 4.1 节）。"""


class CoinGeckoApiError(CoinGeckoError):
    pass


# ---------------------------------------------------------------------------
# 归一化模型
# ---------------------------------------------------------------------------


class NormalizedModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise CoinGeckoContractError(f"非法数值: {value!r}") from None


def _iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError as exc:
        raise CoinGeckoContractError(f"非法时间: {value!r}") from exc


class TxBucket(NormalizedModel):
    buys: int = 0
    sells: int = 0
    buyers: int = 0
    sellers: int = 0


class PoolAttributes(NormalizedModel):
    """发现类接口（megafilter/trending/new_pools）返回的池属性。"""

    address: str
    name: str | None = None
    pool_created_at_ms: int | None = None
    fdv_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    reserve_in_usd: Decimal | None = None
    price_change_pct: dict[str, Decimal] = Field(default_factory=dict)
    transactions: dict[str, TxBucket] = Field(default_factory=dict)
    volume_usd: dict[str, Decimal] = Field(default_factory=dict)
    base_token_address: str | None = None
    quote_token_address: str | None = None
    base_token_symbol: str | None = None
    quote_token_symbol: str | None = None
    base_token_name: str | None = None
    quote_token_name: str | None = None
    base_token_decimals: int | None = None
    quote_token_decimals: int | None = None
    dex: str | None = None
    network: str | None = None


class PoolAttributesBuilder:
    """JSON:API attributes + included 关系 → PoolAttributes。"""

    def __init__(self, network: str) -> None:
        self._network = network
        self._included: dict[str, dict] = {}

    def feed_included(self, included: list[dict]) -> None:
        for item in included or []:
            self._included[item.get("id", "")] = item

    def build(self, raw: dict) -> PoolAttributes:
        attributes = raw.get("attributes")
        if not isinstance(attributes, dict):
            raise CoinGeckoContractError("池对象缺少 attributes")

        def _bucket(key: str, window: str) -> TxBucket:
            data = attributes.get(key) or {}
            window_data = data.get(window)
            if window_data is None:
                return TxBucket()
            if not isinstance(window_data, dict):
                raise CoinGeckoContractError(f"{key}.{window} 类型错误")
            return TxBucket.model_validate(window_data)

        m5 = _bucket("transactions", "m5")
        m15 = _bucket("transactions", "m15")

        def _vol(window: str) -> Decimal | None:
            data = attributes.get("volume_usd") or {}
            return _to_decimal(data.get(window))

        def _pct(window: str) -> Decimal | None:
            data = attributes.get("price_change_percentage") or {}
            return _to_decimal(data.get(window))

        relationships = raw.get("relationships") or {}
        base_token = self._related(relationships.get("base_token"))
        quote_token = self._related(relationships.get("quote_token"))
        dex = self._related(relationships.get("dex"))

        return PoolAttributes(
            address=_require_str(attributes, "address"),
            name=attributes.get("name"),
            pool_created_at_ms=_iso_to_ms(attributes.get("pool_created_at")),
            fdv_usd=_to_decimal(attributes.get("fdv_usd")),
            market_cap_usd=_to_decimal(attributes.get("market_cap_usd")),
            reserve_in_usd=_to_decimal(attributes.get("reserve_in_usd")),
            price_change_pct={
                k: v
                for k, v in {
                    "m5": _pct("m5"),
                    "m15": _pct("m15"),
                    "h1": _pct("h1"),
                    "h6": _pct("h6"),
                    "h24": _pct("h24"),
                }.items()
                if v is not None
            },
            transactions={"m5": m5, "m15": m15},
            volume_usd={
                k: v
                for k, v in {
                    "m5": _vol("m5"),
                    "m15": _vol("m15"),
                    "h1": _vol("h1"),
                    "h6": _vol("h6"),
                    "h24": _vol("h24"),
                }.items()
                if v is not None
            },
            base_token_address=base_token.get("address") if base_token else None,
            quote_token_address=quote_token.get("address") if quote_token else None,
            base_token_symbol=base_token.get("symbol") if base_token else None,
            quote_token_symbol=quote_token.get("symbol") if quote_token else None,
            base_token_name=base_token.get("name") if base_token else None,
            quote_token_name=quote_token.get("name") if quote_token else None,
            base_token_decimals=base_token.get("decimals") if base_token else None,
            quote_token_decimals=base_token.get("decimals") if quote_token else None,
            dex=(dex or {}).get("name"),
            network=self._network,
        )

    def _related(self, relationship: dict | None) -> dict | None:
        if not relationship:
            return None
        data = relationship.get("data")
        if not isinstance(data, dict):
            return None
        item = self._included.get(data.get("id", ""))
        if not item:
            return None
        return item.get("attributes") or {}


class PoolDetail(NormalizedModel):
    """Multiple Pools 返回的池详情（含买卖/净买入 USD 聚合，文档第 4.4 节）。"""

    address: str
    name: str | None = None
    pool_fee_percentage: Decimal | None = None
    pool_created_at_ms: int | None = None
    fdv_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    reserve_in_usd: Decimal | None = None
    locked_liquidity_percentage: Decimal | None = None
    base_token_price_usd: Decimal | None = None
    quote_token_price_usd: Decimal | None = None
    price_change_pct: dict[str, Decimal] = Field(default_factory=dict)
    transactions: dict[str, TxBucket] = Field(default_factory=dict)
    volume_usd: dict[str, Decimal] = Field(default_factory=dict)
    buy_volume_usd: dict[str, Decimal] = Field(default_factory=dict)
    sell_volume_usd: dict[str, Decimal] = Field(default_factory=dict)
    net_buy_volume_usd: dict[str, Decimal] = Field(default_factory=dict)
    base_token_address: str | None = None
    quote_token_address: str | None = None
    base_token_symbol: str | None = None
    quote_token_symbol: str | None = None
    base_token_balance: Decimal | None = None
    quote_token_balance: Decimal | None = None


class PoolDetailBuilder:
    def __init__(self) -> None:
        self._included: dict[str, dict] = {}

    def feed_included(self, included: list[dict]) -> None:
        for item in included or []:
            self._included[item.get("id", "")] = item

    def build(self, raw: dict) -> PoolDetail:
        attributes = raw.get("attributes")
        if not isinstance(attributes, dict):
            raise CoinGeckoContractError("池对象缺少 attributes")

        def _bucket(window: str) -> TxBucket:
            data = attributes.get("transactions") or {}
            window_data = data.get(window)
            if window_data is None:
                return TxBucket()
            return TxBucket.model_validate(window_data)

        def _table(field: str) -> dict[str, Decimal]:
            data = attributes.get(field) or {}
            out: dict[str, Decimal] = {}
            for window in ("m5", "m15", "m30", "h1", "h6", "h24"):
                value = _to_decimal(data.get(window))
                if value is not None:
                    out[window] = value
            return out

        relationships = raw.get("relationships") or {}
        base = self._related(relationships.get("base_token"))
        quote = self._related(relationships.get("quote_token"))

        return PoolDetail(
            address=_require_str(attributes, "address"),
            name=attributes.get("name"),
            pool_fee_percentage=_to_decimal(attributes.get("pool_fee_percentage")),
            pool_created_at_ms=_iso_to_ms(attributes.get("pool_created_at")),
            fdv_usd=_to_decimal(attributes.get("fdv_usd")),
            market_cap_usd=_to_decimal(attributes.get("market_cap_usd")),
            reserve_in_usd=_to_decimal(attributes.get("reserve_in_usd")),
            locked_liquidity_percentage=_to_decimal(
                attributes.get("locked_liquidity_percentage")
            ),
            base_token_price_usd=_to_decimal(attributes.get("base_token_price_usd")),
            quote_token_price_usd=_to_decimal(attributes.get("quote_token_price_usd")),
            price_change_pct=_table("price_change_percentage"),
            transactions={"m5": _bucket("m5"), "m15": _bucket("m15")},
            volume_usd=_table("volume_usd"),
            buy_volume_usd=_table("buy_volume_usd"),
            sell_volume_usd=_table("sell_volume_usd"),
            net_buy_volume_usd=_table("net_buy_volume_usd"),
            base_token_address=base.get("address") if base else None,
            quote_token_address=quote.get("address") if quote else None,
            base_token_symbol=base.get("symbol") if base else None,
            quote_token_symbol=quote.get("symbol") if quote else None,
            base_token_balance=_to_decimal(attributes.get("base_token_balance")),
            quote_token_balance=_to_decimal(attributes.get("quote_token_balance")),
        )

    def _related(self, relationship: dict | None) -> dict | None:
        if not relationship:
            return None
        data = relationship.get("data")
        if not isinstance(data, dict):
            return None
        item = self._included.get(data.get("id", ""))
        if not item:
            return None
        return item.get("attributes") or {}


class TokenInfo(NormalizedModel):
    """Token Info 归一化（文档第 5.3/5.5 节安全门禁字段）。"""

    address: str
    name: str | None = None
    symbol: str | None = None
    decimals: int | None = None
    coingecko_coin_id: str | None = None
    gt_score: Decimal | None = None
    gt_verified: bool | None = None
    holder_count: int | None = None
    holders_last_updated_ms: int | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    is_honeypot: bool | str | None = None
    developer_address: str | None = None
    developer_holding_percentage: Decimal | None = None


class TokenInfoBuilder:
    def build(self, raw: dict) -> TokenInfo:
        data = raw.get("data")
        if not isinstance(data, dict):
            raise CoinGeckoContractError("Token Info 缺少 data")
        attributes = data.get("attributes") or {}
        holders = attributes.get("holders") or {}
        return TokenInfo(
            address=_require_str(attributes, "address"),
            name=attributes.get("name"),
            symbol=attributes.get("symbol"),
            decimals=attributes.get("decimals"),
            coingecko_coin_id=attributes.get("coingecko_coin_id"),
            gt_score=_to_decimal(attributes.get("gt_score")),
            gt_verified=attributes.get("gt_verified"),
            holder_count=holders.get("count"),
            holders_last_updated_ms=_iso_to_ms(holders.get("last_updated")),
            mint_authority=attributes.get("mint_authority"),
            freeze_authority=attributes.get("freeze_authority"),
            is_honeypot=attributes.get("is_honeypot"),
            developer_address=attributes.get("developer_address"),
            developer_holding_percentage=_to_decimal(
                attributes.get("developer_holding_percentage")
            ),
        )


class HolderEntry(NormalizedModel):
    rank: int | None = None
    address: str
    label: str | None = None
    amount: Decimal | None = None
    percentage: Decimal | None = None
    value: Decimal | None = None


class TopHolders(NormalizedModel):
    last_updated_at_ms: int | None = None
    holders: list[HolderEntry] = Field(default_factory=list)


class TraderEntry(NormalizedModel):
    address: str
    realized_pnl_usd: Decimal | None = None
    unrealized_pnl_usd: Decimal | None = None
    token_balance: Decimal | None = None
    average_buy_price_usd: Decimal | None = None
    average_sell_price_usd: Decimal | None = None
    total_buy_count: int | None = None
    total_sell_count: int | None = None
    total_buy_token_amount: Decimal | None = None
    total_sell_token_amount: Decimal | None = None
    total_buy_usd: Decimal | None = None
    total_sell_usd: Decimal | None = None


class TopTraders(NormalizedModel):
    traders: list[TraderEntry] = Field(default_factory=list)


class TradeEntry(NormalizedModel):
    tx_hash: str
    tx_from_address: str
    from_token_address: str
    to_token_address: str
    from_token_amount: Decimal | None = None
    to_token_amount: Decimal | None = None
    kind: str | None = None
    volume_in_usd: Decimal | None = None
    block_timestamp_ms: int | None = None


class OhlcvBar(NormalizedModel):
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class KeyInfo(NormalizedModel):
    plan: str | None = None
    rate_limit_request_per_minute: int | None = None
    monthly_call_credit: int | None = None
    current_total_monthly_calls: int | None = None
    current_remaining_monthly_calls: int | None = None
    api_key_rate_limit_request_per_minute: int | None = None
    api_key_monthly_call_credit: int | None = None
    api_key_current_total_monthly_calls: int | None = None


class TokenTopPool(NormalizedModel):
    """Tokens Multi（include=top_pools）返回的代币 + 顶部池。"""

    address: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    top_pools: list[dict] = Field(default_factory=list)


def _require_str(mapping: dict, key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise CoinGeckoContractError(f"字段 {key} 缺失或类型错误")
    return value


# ---------------------------------------------------------------------------
# 用量账本（进程内计数，G9 定期落库）
# ---------------------------------------------------------------------------


class UsageLedger:
    """按接口记录 REST 尝试次数与 WS 计费响应数。"""

    def __init__(self) -> None:
        self._rest: Counter[str] = Counter()
        self._ws_charged: Counter[str] = Counter()

    def record_rest(self, interface: str) -> None:
        self._rest[interface] += 1

    def record_ws_charged(self, channel: str) -> None:
        self._ws_charged[channel] += 1

    def snapshot(self) -> dict[str, int]:
        return {f"rest:{k}": v for k, v in self._rest.items()}

    def ws_total(self) -> int:
        return sum(self._ws_charged.values())

    def clear(self) -> None:
        self._rest.clear()
        self._ws_charged.clear()


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class CoinGeckoClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: asyncio.Semaphore | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        headers = {"x-cg-pro-api-key": api_key}
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )
        self._concurrency = concurrency
        self.usage = UsageLedger()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CoinGeckoClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _get(self, path: str, params: dict[str, Any], interface: str) -> Any:
        self.usage.record_rest(interface)
        if self._concurrency is not None:
            async with self._concurrency:
                return await self._request(path, params)
        return await self._request(path, params)

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CoinGeckoTimeout(f"{path} 超时") from exc
        if response.status_code == 429:
            raise CoinGeckoRateLimited(f"{path} 限流 (429)")
        if response.status_code >= 500:
            raise CoinGeckoApiError(f"{path} 服务端错误 {response.status_code}")
        if response.status_code != 200:
            raise CoinGeckoApiError(f"{path} 状态码 {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise CoinGeckoContractError(f"{path} 响应非 JSON") from exc

    # -- 发现接口（文档第 4.2 节） -----------------------------------------

    async def megafilter(
        self, params: dict[str, Any], *, pages: int = 1, interface: str = "pools-megafilter"
    ) -> list[PoolAttributes]:
        network = params.get("networks", "unknown")
        builder = PoolAttributesBuilder(str(network))
        results: list[PoolAttributes] = []
        for page in range(1, pages + 1):
            body = await self._get(
                "/onchain/pools/megafilter", {**params, "page": page}, interface
            )
            builder.feed_included(body.get("included"))
            for item in body.get("data") or []:
                results.append(builder.build(item))
        return results

    async def trending_pools(
        self, network: str, *, duration: str = "5m", page: int = 1, include: str = ""
    ) -> list[PoolAttributes]:
        builder = PoolAttributesBuilder(network)
        params: dict[str, Any] = {"duration": duration, "page": page}
        if include:
            params["include"] = include
        body = await self._get(
            f"/onchain/networks/{network}/trending_pools",
            params,
            "trending-pools-network",
        )
        builder.feed_included(body.get("included"))
        return [builder.build(item) for item in body.get("data") or []]

    async def new_pools(
        self, network: str, *, page: int = 1, include: str = ""
    ) -> list[PoolAttributes]:
        builder = PoolAttributesBuilder(network)
        params: dict[str, Any] = {"page": page}
        if include:
            params["include"] = include
        body = await self._get(
            f"/onchain/networks/{network}/new_pools",
            params,
            "latest-pools-network",
        )
        builder.feed_included(body.get("included"))
        return [builder.build(item) for item in body.get("data") or []]

    # -- 详情接口（文档第 4.4 节） ------------------------------------------

    async def pools_multi(
        self,
        network: str,
        addresses: list[str],
        *,
        include_volume_breakdown: bool = False,
    ) -> list[PoolDetail]:
        builder = PoolDetailBuilder()
        params: dict[str, Any] = {}
        # 契约测试锁定：buy/sell/net buy volume 仅在 include_volume_breakdown=true 时返回
        if include_volume_breakdown:
            params["include_volume_breakdown"] = "true"
        body = await self._get(
            f"/onchain/networks/{network}/pools/multi/{','.join(addresses)}",
            params,
            "pools-addresses",
        )
        builder.feed_included(body.get("included"))
        return [builder.build(item) for item in body.get("data") or []]

    async def top_pools_by_token(
        self, network: str, token_address: str, *, include: str = ""
    ) -> list[PoolAttributes]:
        """顶部池映射（文档第 4.4 节）。

        契约测试锁定：Tokens Multi 的 include=top_pools 不返回顶部池，
        必须使用 Top Pools by Token Address 端点逐代币解析。
        """
        builder = PoolAttributesBuilder(network)
        params: dict[str, Any] = {}
        if include:
            params["include"] = include
        body = await self._get(
            f"/onchain/networks/{network}/tokens/{token_address}/pools",
            params,
            "top-pools-contract-address",
        )
        builder.feed_included(body.get("included"))
        return [builder.build(item) for item in body.get("data") or []]

    async def tokens_multi(
        self, network: str, addresses: list[str], *, include_top_pools: bool = False
    ) -> list[TokenTopPool]:
        include = "top_pools" if include_top_pools else ""
        body = await self._get(
            f"/onchain/networks/{network}/tokens/multi/{','.join(addresses)}",
            {"include": include},
            "tokens-data-contract-addresses",
        )
        results: list[TokenTopPool] = []
        for item in body.get("data") or []:
            attributes = item.get("attributes") or {}
            results.append(
                TokenTopPool(
                    address=_require_str(attributes, "address"),
                    symbol=attributes.get("symbol"),
                    name=attributes.get("name"),
                    decimals=attributes.get("decimals"),
                    top_pools=attributes.get("top_pools") or [],
                )
            )
        return results

    async def token_info(self, network: str, address: str) -> TokenInfo:
        body = await self._get(
            f"/onchain/networks/{network}/tokens/{address}/info",
            {},
            "token-info-contract-address",
        )
        return TokenInfoBuilder().build(body)

    async def top_holders(
        self, network: str, address: str, *, holders: int
    ) -> TopHolders:
        body = await self._get(
            f"/onchain/networks/{network}/tokens/{address}/top_holders",
            {"holders": str(holders), "include_pnl_details": "false"},
            "top-token-holders-token-address",
        )
        data = body.get("data") or {}
        attributes = data.get("attributes") or {}
        entries = []
        for item in attributes.get("holders") or []:
            if not isinstance(item, dict):
                raise CoinGeckoContractError("Top Holders 条目类型错误")
            entries.append(
                HolderEntry(
                    rank=item.get("rank"),
                    address=_require_str(item, "address"),
                    label=item.get("label"),
                    amount=_to_decimal(item.get("amount")),
                    percentage=_to_decimal(item.get("percentage")),
                    value=_to_decimal(item.get("value")),
                )
            )
        return TopHolders(
            last_updated_at_ms=_iso_to_ms(attributes.get("last_updated_at")),
            holders=entries,
        )

    async def top_traders(
        self, network: str, address: str, *, traders: int = 20
    ) -> TopTraders:
        body = await self._get(
            f"/onchain/networks/{network}/tokens/{address}/top_traders",
            {"traders": str(traders), "include_address_label": "false"},
            "top-token-traders-token-address",
        )
        data = body.get("data") or {}
        attributes = data.get("attributes") or {}
        entries = []
        for item in attributes.get("traders") or []:
            if not isinstance(item, dict):
                raise CoinGeckoContractError("Top Traders 条目类型错误")
            entries.append(
                TraderEntry(
                    address=_require_str(item, "address"),
                    realized_pnl_usd=_to_decimal(item.get("realized_pnl_usd")),
                    unrealized_pnl_usd=_to_decimal(item.get("unrealized_pnl_usd")),
                    token_balance=_to_decimal(item.get("token_balance")),
                    average_buy_price_usd=_to_decimal(item.get("average_buy_price_usd")),
                    average_sell_price_usd=_to_decimal(item.get("average_sell_price_usd")),
                    total_buy_count=item.get("total_buy_count"),
                    total_sell_count=item.get("total_sell_count"),
                    total_buy_token_amount=_to_decimal(item.get("total_buy_token_amount")),
                    total_sell_token_amount=_to_decimal(item.get("total_sell_token_amount")),
                    total_buy_usd=_to_decimal(item.get("total_buy_usd")),
                    total_sell_usd=_to_decimal(item.get("total_sell_usd")),
                )
            )
        return TopTraders(traders=entries)

    async def pool_trades(
        self,
        network: str,
        pool_address: str,
        *,
        token: str = "base",
    ) -> list[TradeEntry]:
        body = await self._get(
            f"/onchain/networks/{network}/pools/{pool_address}/trades",
            {"token": token},
            "pool-trades-contract-address",
        )
        entries = []
        for item in body.get("data") or []:
            attributes = item.get("attributes") or {}
            if not isinstance(attributes, dict):
                raise CoinGeckoContractError("Pool Trades 条目类型错误")
            entries.append(
                TradeEntry(
                    tx_hash=_require_str(attributes, "tx_hash"),
                    tx_from_address=_require_str(attributes, "tx_from_address"),
                    from_token_address=_require_str(attributes, "from_token_address"),
                    to_token_address=_require_str(attributes, "to_token_address"),
                    from_token_amount=_to_decimal(attributes.get("from_token_amount")),
                    to_token_amount=_to_decimal(attributes.get("to_token_amount")),
                    kind=attributes.get("kind"),
                    volume_in_usd=_to_decimal(attributes.get("volume_in_usd")),
                    block_timestamp_ms=_iso_to_ms(attributes.get("block_timestamp")),
                )
            )
        return entries

    async def pool_ohlcv(
        self,
        network: str,
        pool_address: str,
        *,
        timeframe: str = "minute",
        aggregate: int = 1,
        before_timestamp: int | None = None,
        limit: int = 100,
        currency: str = "usd",
        token: str = "base",
        include_empty_intervals: bool = False,
    ) -> list[OhlcvBar]:
        params: dict[str, Any] = {
            "aggregate": str(aggregate),
            "limit": str(limit),
            "currency": currency,
            "token": token,
            "include_empty_intervals": str(include_empty_intervals).lower(),
        }
        if before_timestamp is not None:
            params["before_timestamp"] = str(before_timestamp)
        body = await self._get(
            f"/onchain/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}",
            params,
            "pool-ohlcv-contract-address",
        )
        data = body.get("data") or {}
        attributes = data.get("attributes") or {}
        bars = []
        for row in attributes.get("ohlcv_list") or []:
            if not isinstance(row, list) or len(row) != 6:
                raise CoinGeckoContractError("OHLCV 行结构错误")
            bars.append(
                OhlcvBar(
                    timestamp_ms=int(row[0]) * 1000,
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[5])),
                )
            )
        return bars

    # -- 额度（文档第 4.6 节） ----------------------------------------------

    async def key(self) -> KeyInfo:
        body = await self._get("/key", {}, "api-usage")
        try:
            return KeyInfo.model_validate(body)
        except Exception as exc:
            raise CoinGeckoContractError(f"/key 响应无法解析: {body}") from exc
