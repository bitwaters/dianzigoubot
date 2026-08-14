"""运行配置与策略 Schema（总控文档第 6 章）。

模块分区：
  1. 环境变量与日志
  2. 共享 Pydantic 类型
  3. 策略 Schema（strategy.yaml 唯一可编辑策略文件）
  4. 插件配置（plugins.yaml）
  5. 加载入口
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from app.core.models import stable_sha256

# ---------------------------------------------------------------------------
# 1. 环境变量与日志
# ---------------------------------------------------------------------------

_ENV_FILE = Path("dianzigou.env")


def load_env_file(path: Path = _ENV_FILE) -> None:
    """读取本地 KEY=VALUE 环境文件（不覆盖已存在的环境变量）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


class _SecretFilter(logging.Filter):
    """日志脱敏：替换已知 Secret 的取值。"""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            msg = msg.replace(secret, "***")
        record.msg = msg
        record.args = ()
        return True


@dataclass
class Settings:
    coingecko_api_key: str
    telegram_bot_token: str
    telegram_admin_id: int
    telegram_signal_chat_id: int
    db_path: Path = Path("bot.sqlite")
    log_level: str = "INFO"
    dispatch_timeout_seconds: float = 5.0
    plugins_config_path: Path = Path("config/plugins.yaml")
    strategy_path: Path = Path("config/strategy.yaml")
    secrets: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()

        def require(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise RuntimeError(f"缺少必需环境变量: {name}")
            return value

        settings = cls(
            coingecko_api_key=require("COINGECKO_API_KEY"),
            telegram_bot_token=require("TELEGRAM_BOT_TOKEN"),
            telegram_admin_id=int(require("TELEGRAM_ADMIN_ID")),
            telegram_signal_chat_id=int(require("TELEGRAM_SIGNAL_CHAT_ID")),
        )
        settings.db_path = Path(os.environ.get("BOT_DB_PATH", "bot.sqlite"))
        settings.log_level = os.environ.get("LOG_LEVEL", "INFO")
        settings.dispatch_timeout_seconds = float(
            os.environ.get("DISPATCH_TIMEOUT_SECONDS", "5")
        )
        settings.plugins_config_path = Path(
            os.environ.get("PLUGINS_CONFIG_PATH", "config/plugins.yaml")
        )
        settings.strategy_path = Path(
            os.environ.get("STRATEGY_PATH", "config/strategy.yaml")
        )
        settings.secrets = [
            settings.coingecko_api_key,
            settings.telegram_bot_token,
        ]
        return settings


def setup_logging(level: str, secrets: list[str]) -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime  # 日志时间统一 UTC（总控文档第 4.1 节）
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(_SecretFilter(secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


# ---------------------------------------------------------------------------
# 2. 共享 Pydantic 类型
# ---------------------------------------------------------------------------


def _decimal_str_only(value: Any) -> Decimal:
    """策略数值必须是带引号的十进制字符串（总控文档第 6.1 节）。"""
    if not isinstance(value, str):
        raise ValueError("金额/百分比/权重必须写为带引号的十进制字符串")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"非法十进制字符串: {value!r}") from exc


DecimalStr = Annotated[Decimal, BeforeValidator(_decimal_str_only)]


def _non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("不允许负值")
    return value


def _within(low: Decimal, high: Decimal, *, inclusive: bool = True):
    def check(value: Decimal) -> Decimal:
        low_ok = value >= low if inclusive else value > low
        high_ok = value <= high if inclusive else value < high
        if not (low_ok and high_ok):
            raise ValueError(f"超出值域 [{low}, {high}]")
        return value

    return check


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# 3. 策略 Schema
# ---------------------------------------------------------------------------

RiskAction = Literal["reject", "observe"]


class MegafilterTemplate(StrictModel):
    id: str
    endpoint: Literal["megafilter", "trending_pools", "new_pools"]
    sort: Literal[
        "m5_trending", "m5_price_change_percentage_desc", "endpoint_default"
    ]
    poll_seconds: int = Field(gt=0)
    pages: int = Field(gt=0)
    query: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_injected_keys(self) -> "MegafilterTemplate":
        if self.endpoint != "megafilter":
            return self
        forbidden = {"reserve_in_usd_min", "tx_count_min", "tx_count_duration"}
        overlap = forbidden & set(self.query)
        if overlap:
            raise ValueError(
                f"Megafilter 模板 query 禁止声明注入参数: {sorted(overlap)}"
            )
        return self


class WebsocketConfig(StrictModel):
    g1_max_subscriptions: Literal[100] = 100
    g3_max_subscriptions: Literal[100] = 100
    top_pool_mapping_ttl_seconds: int = Field(gt=0)
    watch_seconds_max: Literal[15] = 15
    focus_seconds_max: Literal[15] = 15
    watch_messages_max: Literal[15] = 15
    focus_messages_max: Literal[15] = 15
    g1_watch_rounds_max: Literal[2] = 2
    g3_focus_rounds_max: Literal[2] = 2
    g2_enabled: Literal[False] = False
    daily_credit_budget: DecimalStr


class CollectionConfig(StrictModel):
    version: str = Field(min_length=1)
    query_templates: list[MegafilterTemplate]
    reserve_usd_floor: Annotated[DecimalStr, AfterValidator(_non_negative)]
    tx_count_floor: int = Field(ge=0)
    tx_count_duration: Literal["5m"] = "5m"
    rest_concurrency_max: int = Field(gt=0)
    websocket: WebsocketConfig

    @model_validator(mode="after")
    def _fixed_four_templates(self) -> "CollectionConfig":
        required = {
            ("megafilter", "m5_trending"): 30,
            ("megafilter", "m5_price_change_percentage_desc"): 120,
            ("trending_pools", "endpoint_default"): 120,
            ("new_pools", "endpoint_default"): 180,
        }
        actual: dict[tuple[str, str], int] = {}
        for tpl in self.query_templates:
            key = (tpl.endpoint, tpl.sort)
            if key in actual:
                raise ValueError(f"重复模板: {key}")
            actual[key] = tpl.poll_seconds
        if actual != required:
            raise ValueError(
                "每条链必须固定四个入口模板: "
                "(megafilter,m5_trending,30s)、(megafilter,m5_price_change_percentage_desc,120s)、"
                "(trending_pools,endpoint_default,120s)、(new_pools,endpoint_default,180s)"
            )
        return self


class HotChannel(StrictModel):
    enabled: bool
    max_source_rank: int = Field(gt=0)


class AnomalyChannel(StrictModel):
    enabled: bool
    volume_acceleration_ratio_min: Annotated[
        DecimalStr, AfterValidator(_non_negative)
    ]
    buy_sell_tx_ratio_min: Annotated[DecimalStr, AfterValidator(_non_negative)]
    price_change_5m_abs_pct_min: Annotated[
        DecimalStr, AfterValidator(_non_negative)
    ]


class NewPoolChannel(StrictModel):
    enabled: bool
    pool_age_seconds_max: int = Field(ge=0)


class DiscoveryChannels(StrictModel):
    hot: HotChannel
    anomaly: AnomalyChannel
    new_pool: NewPoolChannel


class DiscoveryConfig(StrictModel):
    candidate_ttl_seconds: int = Field(gt=0)
    rest_refresh_seconds: Literal[30] = 30
    new_pool_min_age_seconds: int = Field(ge=0)


class TrustedQuoteAsset(StrictModel):
    symbol: str = Field(min_length=1)
    address: str = Field(min_length=1)


class MarketQualityConfig(StrictModel):
    trusted_quote_assets: list[TrustedQuoteAsset]
    reserve_usd_min: Annotated[DecimalStr, AfterValidator(_non_negative)]
    fdv_usd_max: Annotated[DecimalStr, AfterValidator(_non_negative)]
    fdv_liquidity_ratio_max: Annotated[DecimalStr, AfterValidator(_non_negative)]
    pool_age_seconds_min: int = Field(ge=0)
    pool_age_seconds_max: int = Field(ge=0)
    volume_5m_usd_min: Annotated[DecimalStr, AfterValidator(_non_negative)]
    tx_count_5m_min: int = Field(ge=0)
    buys_5m_min: int = Field(ge=0)
    sells_5m_min: int = Field(ge=0)
    independent_buyers_5m_min: int = Field(ge=0)
    independent_sellers_5m_min: int = Field(ge=0)
    buy_sell_tx_ratio_5m_min: Annotated[DecimalStr, AfterValidator(_non_negative)]

    @model_validator(mode="after")
    def _unique_assets(self) -> "MarketQualityConfig":
        addresses = [asset.address for asset in self.trusted_quote_assets]
        if len(addresses) != len(set(addresses)):
            raise ValueError("trusted_quote_assets 地址不得重复")
        return self


class SolanaSecurityConfig(StrictModel):
    gt_score_min: int = Field(ge=0, le=100)
    top10_holding_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    developer_holding_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    transfer_fee_bps_max: Annotated[DecimalStr, AfterValidator(_non_negative)]
    lp_locked_pct_min: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    allowed_pool_models: list[str]

    closable_action: RiskAction
    metadata_mutable_action: RiskAction
    transfer_fee_upgradable_action: RiskAction
    default_state_upgradable_action: RiskAction
    balance_mutable_action: RiskAction
    transfer_hook_action: RiskAction
    token2022_risks_action: RiskAction

    @model_validator(mode="after")
    def _pool_model(self) -> "SolanaSecurityConfig":
        if "LP_TOKEN_OR_BURN_VERIFIABLE" not in self.allowed_pool_models:
            raise ValueError("Solana allowed_pool_models 必须包含 LP_TOKEN_OR_BURN_VERIFIABLE")
        return self


class BscSecurityConfig(StrictModel):
    gt_score_min: int = Field(ge=0, le=100)
    top10_holding_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    creator_owner_holding_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    buy_tax_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    sell_tax_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    transfer_tax_pct_max: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    lp_locked_pct_min: Annotated[DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))]
    allowed_pool_models: list[str]

    proxy_action: RiskAction
    mintable_action: RiskAction
    transfer_pausable_action: RiskAction
    blacklist_action: RiskAction
    whitelist_action: RiskAction
    anti_whale_action: RiskAction
    trading_cooldown_action: RiskAction
    other_risks_action: RiskAction

    @model_validator(mode="after")
    def _pool_model(self) -> "BscSecurityConfig":
        if "V2_LP_TOKEN" not in self.allowed_pool_models:
            raise ValueError("BSC allowed_pool_models 必须包含 V2_LP_TOKEN")
        return self


class FeatureConfig(StrictModel):
    bad: DecimalStr
    good: DecimalStr
    weight: Annotated[DecimalStr, AfterValidator(_non_negative)]
    missing_action: Literal["REJECT", "CAP_SETUP", "ZERO_SCORE"]


class DimensionConfig(StrictModel):
    weight: Annotated[DecimalStr, AfterValidator(_non_negative)]
    features: dict[str, FeatureConfig]


class ScoringConfig(StrictModel):
    watch_score_min: DecimalStr
    setup_score_min: DecimalStr
    buy_score_min: DecimalStr
    dimensions: dict[str, DimensionConfig]


class SmartMoneyConfig(StrictModel):
    top_traders_limit: int = Field(ge=1, le=50)
    recent_trade_window_seconds: int = Field(gt=0)
    trade_response_limit: Literal[300] = 300
    local_wallet_min_observed_tokens: int = Field(gt=0)
    local_wallet_success_rate_min_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))
    ]
    local_wallet_rug_rate_max_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))
    ]
    success_horizon_seconds: int = Field(gt=0)
    success_gain_pct_min: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    failure_drawdown_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    min_confirming_wallets: int = Field(gt=0)
    sell_block_net_volume_pct_min: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]


class AntiChaseConfig(StrictModel):
    vertical_rise_10s_wait_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    vwap_10s_deviation_wait_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    max_wait_seconds: int = Field(gt=0)
    drawdown_from_10s_high_block_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    observed_liquidity_drop_between_responses_block_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    consolidation_seconds_min: int = Field(gt=0)
    consolidation_range_pct_max: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    pullback_min_pct: Annotated[DecimalStr, AfterValidator(_non_negative)]
    pullback_max_pct: Annotated[DecimalStr, AfterValidator(_non_negative)]
    pullback_recovery_ratio_min: Annotated[DecimalStr, AfterValidator(_non_negative)]

    @model_validator(mode="after")
    def _pullback_order(self) -> "AntiChaseConfig":
        if self.pullback_max_pct < self.pullback_min_pct:
            raise ValueError("pullback_max_pct 不得小于 pullback_min_pct")
        return self


class SignalsConfig(StrictModel):
    validity_seconds: int = Field(gt=0)
    setup_confirmation_seconds: int = Field(gt=0)
    signal_rearm_below_setup_seconds: int = Field(gt=0)
    min_g3_final_1s_bars: int = Field(gt=0)
    g3_bar_final_delay_seconds: int = Field(gt=0)
    max_g3_age_seconds: int = Field(gt=0)
    max_rest_market_age_seconds: int = Field(gt=0)
    max_participant_age_seconds: int = Field(gt=0)


class TakeProfitLeg(StrictModel):
    leg_index: int = Field(ge=1)
    trigger_profit_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))
    ]
    sell_pct_of_initial: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100)))
    ]


class BacktestModelConfig(StrictModel):
    base_slippage_bps: Annotated[DecimalStr, AfterValidator(_non_negative)]
    max_impact_bps: Annotated[DecimalStr, AfterValidator(_non_negative)]
    impact_coefficient: Annotated[DecimalStr, AfterValidator(_non_negative)]
    network_fee_native: Annotated[DecimalStr, AfterValidator(_non_negative)]
    min_fill_liquidity_usd: Annotated[DecimalStr, AfterValidator(_non_negative)]
    fill_deviation_pct_max: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    fill_deadline_seconds: int = Field(ge=180)
    take_profit_legs: list[TakeProfitLeg] = Field(default_factory=list)
    stop_loss_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    trailing_stop_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    trailing_activation_profit_pct: Annotated[
        DecimalStr, AfterValidator(_within(Decimal(0), Decimal(100), inclusive=False))
    ]
    max_hold_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _legs_order_and_sum(self) -> "BacktestModelConfig":
        legs = sorted(self.take_profit_legs, key=lambda x: x.leg_index)
        if [x.leg_index for x in legs] != list(range(1, len(legs) + 1)):
            raise ValueError("take_profit_legs 的 leg_index 必须从 1 连续递增")
        for prev, cur in zip(legs, legs[1:]):
            if not cur.trigger_profit_pct > prev.trigger_profit_pct:
                raise ValueError("take_profit_legs 触发涨幅必须按档位严格递增")
        total = sum((x.sell_pct_of_initial for x in legs), Decimal(0))
        if total > 100:
            raise ValueError("take_profit_legs 卖出比例总和不得超过 100")
        return self


class ChainStrategy(StrictModel):
    collection: CollectionConfig
    discovery_channels: DiscoveryChannels
    discovery: DiscoveryConfig
    market_quality: MarketQualityConfig
    security: SolanaSecurityConfig | BscSecurityConfig
    scoring: ScoringConfig
    smart_money: SmartMoneyConfig
    anti_chase: AntiChaseConfig
    signals: SignalsConfig
    backtest_model: BacktestModelConfig

    @model_validator(mode="after")
    def _validate_scoring(self) -> "ChainStrategy":
        scoring = self.scoring
        dimensions = scoring.dimensions

        known_dims = {
            "market_quality",
            "short_momentum",
            "activity_acceleration",
            "trade_flow",
            "participant_structure",
            "holding_distribution",
            "smart_money_flow",
            "microstructure",
        }
        if set(dimensions) != known_dims:
            raise ValueError(
                f"评分维度必须是固定八维，多余或缺失: "
                f"{sorted(set(dimensions) ^ known_dims)}"
            )

        expected_features: dict[str, set[str]] = {
            "market_quality": {
                "reserve_usd",
                "volume_liquidity_ratio_5m",
                "fdv_liquidity_ratio",
                "gt_score",
                "gt_verified",
            },
            "short_momentum": {
                "price_change_5s_pct",
                "price_change_10s_pct",
                "price_change_5m_pct",
            },
            "activity_acceleration": {
                "tx_count_5m",
                "volume_rate_ratio_m5_to_previous_10m",
                "tx_rate_ratio_m5_to_previous_10m",
            },
            "trade_flow": {"buy_sell_tx_ratio_5m", "net_buy_tx_pct_5m"},
            "participant_structure": {
                "independent_buyers_5m",
                "buyer_seller_ratio_5m",
                "holder_count",
            },
            "holding_distribution": {
                "top10_holding_pct",
                "developer_or_creator_holding_pct",
                "lp_locked_pct",
            },
            "smart_money_flow": {
                "local_smart_net_buy_60s_as_pct_of_volume_5m",
                "token_profitable_net_buy_60s_as_pct_of_volume_5m",
                "active_smart_wallets_60s",
            },
            "microstructure": {
                "close_location_10s",
                "positive_1s_bar_ratio_10s",
                "volume_retention_ratio_10s",
            },
        }
        for dim, feature_set in dimensions.items():
            if set(feature_set.features) != expected_features[dim]:
                raise ValueError(
                    f"维度 {dim} 的特征集合必须固定，多余或缺失: "
                    f"{sorted(set(feature_set.features) ^ expected_features[dim])}"
                )

        total = sum(
            (dim.weight for dim in dimensions.values()), Decimal(0)
        )
        if total != 1:
            raise ValueError(f"维度权重之和必须为 1，当前 {total}")

        reverse_features = {
            "fdv_liquidity_ratio",
            "top10_holding_pct",
            "developer_or_creator_holding_pct",
        }
        for dim, dim_cfg in dimensions.items():
            feature_total = sum(
                (f.weight for f in dim_cfg.features.values()), Decimal(0)
            )
            if dim_cfg.weight > 0 and feature_total != 1:
                raise ValueError(
                    f"启用维度 {dim} 的特征权重之和必须为 1，当前 {feature_total}"
                )
            for name, feature in dim_cfg.features.items():
                if feature.good == feature.bad:
                    raise ValueError(f"特征 {name} 的 good 与 bad 不得相等")
                if name in reverse_features:
                    if not feature.good < feature.bad:
                        raise ValueError(f"反向特征 {name} 必须 good < bad")
                else:
                    if not feature.good > feature.bad:
                        raise ValueError(f"正向特征 {name} 必须 good > bad")

        return self


class StrategyFile(StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, populate_by_name=True
    )

    schema_version: Literal[1] = Field(default=1, alias="schema")
    revision: str = Field(pattern=r"^strategy-\d+$")
    parent_revision: str | None = Field(default=None, pattern=r"^strategy-\d+$")
    change_reason: str = Field(min_length=1)
    solana: ChainStrategy
    bsc: ChainStrategy

    @model_validator(mode="after")
    def _chain_security_types(self) -> "StrategyFile":
        if not isinstance(self.solana.security, SolanaSecurityConfig):
            raise ValueError("solana.security 必须使用 SolanaSecurityConfig 结构")
        if not isinstance(self.bsc.security, BscSecurityConfig):
            raise ValueError("bsc.security 必须使用 BscSecurityConfig 结构")
        return self

    @property
    def strategy_hash(self) -> str:
        return stable_sha256(self.model_dump(mode="json", by_alias=True))


def load_strategy_file(path: Path) -> StrategyFile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"策略文件 {path} 顶层必须是对象")
    return StrategyFile.model_validate(raw)


class StrategyHolder:
    """进程内策略持有器：激活/回退时热替换（文档第 10.6 节）。"""

    def __init__(self, strategy: StrategyFile) -> None:
        self._strategy = strategy

    def get(self) -> StrategyFile:
        return self._strategy

    def set(self, strategy: StrategyFile) -> None:
        self._strategy = strategy


# ---------------------------------------------------------------------------
# 4. 插件配置
# ---------------------------------------------------------------------------


class PluginsConfig(StrictModel):
    enabled: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "PluginsConfig":
        if len(self.enabled) != len(set(self.enabled)):
            raise ValueError("enabled 列表不得重复")
        return self


def load_plugins_config(path: Path) -> PluginsConfig:
    if not path.exists():
        return PluginsConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PluginsConfig.model_validate(raw)
