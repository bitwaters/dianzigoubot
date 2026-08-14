"""安全门禁状态机（总控文档第 5 章）。

纯函数：输入归一化的 CoinGecko/GoPlus 数据与链策略，输出逐字段安全结论与
整体 trade_allowed。拒绝语义：不允许产生交易信号。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Optional

from app.core.clients.coingecko import TokenInfo, TopHolders
from app.core.clients.goplus import EvmSecurity, SolSecurity

log = logging.getLogger(__name__)

SecurityStatus = Literal["SAFE", "RISK", "UNKNOWN", "NOT_APPLICABLE", "STALE"]
RiskAction = Literal["reject", "observe"]

# 明确 burn/zero 地址（双链 Top10 排除，总控文档第 4.4 节）
BURN_ADDRESSES_SOL = {
    "11111111111111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",  # WSOL 也排除于持仓集中度外
}
BURN_ADDRESSES_EVM = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000001",
}


@dataclass
class FieldResult:
    status: SecurityStatus
    reason_code: str | None = None
    detail: str | None = None


@dataclass
class SecurityResult:
    fields: dict[str, FieldResult] = field(default_factory=dict)
    values: dict[str, Decimal | int | str | None] = field(default_factory=dict)
    trade_allowed: bool = True

    def set(
        self,
        name: str,
        status: SecurityStatus,
        reason_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.fields[name] = FieldResult(status, reason_code, detail)

    def set_field(self, name: str, field: FieldResult) -> None:
        self.fields[name] = field

    def set_value(self, name: str, value: Decimal | int | str | None) -> None:
        self.values[name] = value

    def recompute(self) -> None:
        self.trade_allowed = all(
            f.status in ("SAFE", "NOT_APPLICABLE") for f in self.fields.values()
        )

    @property
    def passed(self) -> bool:
        return self.trade_allowed


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _action_to_status(action: RiskAction, *, detail: str = "") -> SecurityStatus:
    return "RISK" if action == "reject" else "SAFE"


def _flag_result(name: str, flag: str | None, result: SecurityResult, *, invert: bool = False) -> None:
    """布尔字段 "0"/"1" → SAFE/RISK；缺失 → UNKNOWN。"""
    if flag is None:
        result.set(name, "UNKNOWN", "FIELD_MISSING")
    elif flag == "1":
        result.set(name, "RISK" if not invert else "SAFE")
    elif flag == "0":
        result.set(name, "SAFE" if not invert else "RISK")
    else:
        result.set(name, "UNKNOWN", "BAD_VALUE", f"value={flag}")


def _flag_optional(name: str, flag: str | None, result: SecurityResult) -> None:
    """布尔字段；官方允许缺失（记 SAFE_NO_EVIDENCE）。"""
    if flag is None:
        result.set(name, "SAFE", "SAFE_NO_EVIDENCE")
    elif flag == "1":
        result.set(name, "RISK")
    elif flag == "0":
        result.set(name, "SAFE")
    else:
        result.set(name, "UNKNOWN", "BAD_VALUE", f"value={flag}")


def top10_holding_pct(
    coin_holders: TopHolders | None,
    goplus_holders: list,
    *,
    burn_addresses: set[str],
    exclude_addresses: set[str] | None = None,
    chain_norm=None,
) -> Decimal | None:
    """双源 Top 10 计算：各自排除 burn/zero 与主池等明确地址后求和，取最大值。

    返回 None 表示两个来源都无法解析（禁止发信号）。
    """
    exclude = set(exclude_addresses or set())

    def _pct(address: str, percent: Decimal | None) -> Decimal | None:
        if not address or percent is None:
            return None
        norm = chain_norm(address) if chain_norm else address
        if norm in burn_addresses or norm in exclude:
            return None
        return percent

    results: list[Decimal] = []
    if coin_holders is not None:
        parts = [
            p
            for h in coin_holders.holders
            if (p := _pct(h.address, h.percentage)) is not None
        ]
        if parts:
            results.append(min(sum(sorted(parts, reverse=True)[:10]), Decimal(100)))
    if goplus_holders:
        parts = [
            p
            for h in goplus_holders
            if (p := _pct(getattr(h, "token_account", None) or getattr(h, "address", None), getattr(h, "percent", None))) is not None
        ]
        if parts:
            results.append(min(sum(sorted(parts, reverse=True)[:10]), Decimal(100)))
    return max(results) if results else None


# ---------------------------------------------------------------------------
# 两段安检：CoinGecko 本地硬门禁（②段，不消耗 GoPlus）
# ---------------------------------------------------------------------------


def cg_pregate_solana(
    *,
    token_info: TokenInfo,
    coin_holders: TopHolders | None,
    security_cfg,
    main_pool_address: str | None = None,
) -> SecurityResult:
    """CoinGecko 本地安检（文档第 5.2 节②段）：不通过即拒绝，不进入 GoPlus。"""
    result = SecurityResult()

    gt = token_info.gt_score
    if gt is None:
        result.set("gt_score", "UNKNOWN", "GT_SCORE_MISSING")
    elif gt < Decimal(security_cfg.gt_score_min):
        result.set("gt_score", "RISK", "GT_SCORE_LOW", f"gt={gt}")
    else:
        result.set("gt_score", "SAFE")

    honeypot = token_info.is_honeypot
    if honeypot == "unknown" or honeypot is None:
        result.set("honeypot_cg", "NOT_APPLICABLE", "SOLANA_HONEYPOT_CHECK_UNSUPPORTED")
    elif isinstance(honeypot, bool):
        result.set("honeypot_cg", "UNKNOWN", "CONTRACT_CHANGE", f"value={honeypot!r}")
    else:
        result.set("honeypot_cg", "UNKNOWN", "CONTRACT_CHANGE", f"value={honeypot!r}")

    for name, value in [
        ("mint_authority", token_info.mint_authority),
        ("freeze_authority", token_info.freeze_authority),
    ]:
        if value is None:
            result.set(name, "UNKNOWN", "AUTHORITY_MISSING")
        elif value == "no":
            result.set(name, "SAFE")
        else:
            result.set(name, "RISK", "AUTHORITY_PRESENT", str(value))

    dev = token_info.developer_holding_percentage
    result.set_value("developer_or_creator_holding_pct", dev)
    if dev is None:
        result.set("developer_holding_pct", "UNKNOWN", "DEV_HOLDING_MISSING")
    elif dev > security_cfg.developer_holding_pct_max:
        result.set("developer_holding_pct", "RISK", "DEV_HOLDING_TOO_HIGH", f"{dev}%")
    else:
        result.set("developer_holding_pct", "SAFE")

    top10 = top10_holding_pct(
        coin_holders,
        [],
        burn_addresses=BURN_ADDRESSES_SOL,
        exclude_addresses={main_pool_address} if main_pool_address else None,
    )
    result.set_value("top10_holding_pct", top10)
    if top10 is None:
        result.set("top10_holding_pct", "UNKNOWN", "NO_VALID_SOURCE")
    elif top10 > security_cfg.top10_holding_pct_max:
        result.set("top10_holding_pct", "RISK", "TOP10_TOO_HIGH", f"{top10}%")
    else:
        result.set("top10_holding_pct", "SAFE")

    result.recompute()
    return result


def cg_pregate_bsc(
    *,
    token_info: TokenInfo,
    coin_holders: TopHolders | None,
    security_cfg,
    main_pool_address: str | None = None,
) -> SecurityResult:
    """CoinGecko 本地安检（文档第 5.2 节②段）：不通过即拒绝，不进入 GoPlus。"""
    result = SecurityResult()

    gt = token_info.gt_score
    if gt is None:
        result.set("gt_score", "UNKNOWN", "GT_SCORE_MISSING")
    elif gt < Decimal(security_cfg.gt_score_min):
        result.set("gt_score", "RISK", "GT_SCORE_LOW", f"gt={gt}")
    else:
        result.set("gt_score", "SAFE")

    honeypot = token_info.is_honeypot
    if isinstance(honeypot, bool):
        result.set("honeypot_cg", "RISK" if honeypot else "SAFE")
    elif honeypot == "unknown" or honeypot is None:
        result.set("honeypot_cg", "UNKNOWN", "HONEYPOT_UNKNOWN")
    else:
        result.set("honeypot_cg", "UNKNOWN", "CONTRACT_CHANGE", f"value={honeypot!r}")

    top10 = top10_holding_pct(
        coin_holders,
        [],
        burn_addresses=BURN_ADDRESSES_EVM,
        exclude_addresses={main_pool_address} if main_pool_address else None,
        chain_norm=str.lower,
    )
    result.set_value("top10_holding_pct", top10)
    if top10 is None:
        result.set("top10_holding_pct", "UNKNOWN", "NO_VALID_SOURCE")
    elif top10 > security_cfg.top10_holding_pct_max:
        result.set("top10_holding_pct", "RISK", "TOP10_TOO_HIGH", f"{top10}%")
    else:
        result.set("top10_holding_pct", "SAFE")

    result.recompute()
    return result


# ---------------------------------------------------------------------------
# BSC（总控文档第 5.3、5.4 节）
# ---------------------------------------------------------------------------


def evaluate_bsc(
    *,
    token_info: TokenInfo,
    goplus: EvmSecurity,
    security_cfg,
    main_pool_address: str | None,
    coin_holders: TopHolders | None,
    lp_locked_source: bool = True,
) -> SecurityResult:
    result = SecurityResult()

    # -- CoinGecko 门禁（第 5.3 节） --
    gt = token_info.gt_score
    if gt is None:
        result.set("gt_score", "UNKNOWN", "GT_SCORE_MISSING")
    elif gt < Decimal(security_cfg.gt_score_min):
        result.set("gt_score", "RISK", "GT_SCORE_LOW", f"gt={gt}")
    else:
        result.set("gt_score", "SAFE")

    honeypot = token_info.is_honeypot
    if isinstance(honeypot, bool):
        result.set("honeypot_cg", "RISK" if honeypot else "SAFE")
    elif honeypot == "unknown" or honeypot is None:
        result.set("honeypot_cg", "UNKNOWN", "HONEYPOT_UNKNOWN")
    else:
        result.set("honeypot_cg", "UNKNOWN", "CONTRACT_CHANGE", f"value={honeypot!r}")

    # -- GoPlus 固定拒绝（第 5.4 节） --
    # open_source/in_dex 语义反转："0"（未开源/未上 DEX）才是危险值
    _flag_result("open_source", goplus.is_open_source, result, invert=True)
    _flag_result("in_dex", goplus.is_in_dex, result, invert=True)
    _flag_result("honeypot_gp", goplus.is_honeypot, result)
    _flag_result("cannot_buy", goplus.cannot_buy, result)
    _flag_result("cannot_sell_all", goplus.cannot_sell_all, result)

    if goplus.is_honeypot == "1" or goplus.sell_tax == Decimal(1):
        result.set("sell_blocked", "RISK", "HONEYPOT_OR_100PCT_SELL_TAX")
    else:
        result.set("sell_blocked", "SAFE")

    _flag_result("fake_token", _fake_token_flag(goplus), result)
    _flag_optional("gas_abuse", goplus.gas_abuse, result)
    _flag_optional("airdrop_scam", goplus.is_airdrop_scam, result)
    if goplus.owner_change_balance == "1":
        result.set("honeypot_same_creator", "RISK", "OWNER_CHANGE_BALANCE")
    else:
        result.set("honeypot_same_creator", "SAFE")
    if goplus.hidden_owner == "1" or goplus.can_take_back_ownership == "1":
        result.set("ownership_risk", "RISK", "HIDDEN_OWNER_OR_TAKE_BACK")
    else:
        result.set("ownership_risk", "SAFE")
    if goplus.selfdestruct == "1" or goplus.external_call == "1":
        result.set("code_risk", "RISK", "SELFDESTRUCT_OR_EXTERNAL_CALL")
    else:
        result.set("code_risk", "SAFE")
    if goplus.slippage_modifiable == "1" or goplus.personal_slippage_modifiable == "1":
        result.set("slippage_risk", "RISK", "SLIPPAGE_MODIFIABLE")
    else:
        result.set("slippage_risk", "SAFE")

    # -- 策略动作字段（第 5.4 节 configurable_risk） --
    for name, flag, action in [
        ("proxy", goplus.is_proxy, security_cfg.proxy_action),
        ("mintable", goplus.is_mintable, security_cfg.mintable_action),
        ("transfer_pausable", goplus.transfer_pausable, security_cfg.transfer_pausable_action),
        ("blacklist", goplus.is_blacklisted, security_cfg.blacklist_action),
        ("whitelist", goplus.is_whitelisted, security_cfg.whitelist_action),
        ("anti_whale", goplus.is_anti_whale, security_cfg.anti_whale_action),
        ("anti_whale_modifiable", goplus.anti_whale_modifiable, security_cfg.anti_whale_action),
        ("trading_cooldown", goplus.trading_cooldown, security_cfg.trading_cooldown_action),
    ]:
        if flag == "1":
            result.set(name, _action_to_status(action), "STRATEGY_ACTION", action)
        elif flag == "0":
            result.set(name, "SAFE")
        else:
            result.set(name, "UNKNOWN", "FIELD_MISSING")

    if goplus.other_potential_risks:
        result.set(
            "other_potential_risks",
            _action_to_status(security_cfg.other_risks_action),
            "STRATEGY_ACTION",
            str(goplus.other_potential_risks)[:200],
        )
    else:
        result.set("other_potential_risks", "SAFE", "SAFE_NO_EVIDENCE")

    # -- 税率与持仓（第 5.4 节） --
    for name, tax, limit in [
        ("buy_tax", goplus.buy_tax, security_cfg.buy_tax_pct_max),
        ("sell_tax", goplus.sell_tax, security_cfg.sell_tax_pct_max),
        ("transfer_tax", goplus.transfer_tax, security_cfg.transfer_tax_pct_max),
    ]:
        if tax is None:
            result.set(name, "UNKNOWN", "TAX_MISSING")
        else:
            pct = tax * 100
            if pct > limit:
                result.set(name, "RISK", "TAX_TOO_HIGH", f"{pct}%>{limit}%")
            else:
                result.set(name, "SAFE")

    creator_pct = max(
        (goplus.creator_percent or Decimal(0)),
        (goplus.owner_percent or Decimal(0)),
    ) * 100
    if creator_pct > security_cfg.creator_owner_holding_pct_max:
        result.set("creator_owner_pct", "RISK", "CREATOR_OWNER_TOO_HIGH", f"{creator_pct}%")
    else:
        result.set("creator_owner_pct", "SAFE")

    # -- Top 10（双源取最大） --
    top10 = top10_holding_pct(
        coin_holders,
        goplus.holders,
        burn_addresses=BURN_ADDRESSES_EVM,
        exclude_addresses={main_pool_address} if main_pool_address else None,
        chain_norm=str.lower,
    )
    result.set_value("top10_holding_pct", top10)
    if top10 is None:
        result.set("top10_holding_pct", "UNKNOWN", "NO_VALID_SOURCE")
    elif top10 > security_cfg.top10_holding_pct_max:
        result.set("top10_holding_pct", "RISK", "TOP10_TOO_HIGH", f"{top10}%")
    else:
        result.set("top10_holding_pct", "SAFE")

    # -- LP 锁仓（主池匹配 V2） --
    lp_field = _bsc_lp_locked(goplus, main_pool_address, security_cfg)
    result.set_field("lp_locked_pct", lp_field)
    result.set_value("lp_locked_pct", _pct_from_detail(lp_field))
    result.set_value(
        "developer_or_creator_holding_pct",
        max(
            (goplus.creator_percent or Decimal(0)),
            (goplus.owner_percent or Decimal(0)),
        )
        * 100,
    )
    result.set_value(
        "creator_owner_addresses",
        [
            a
            for a in (goplus.creator_address, goplus.owner_address)
            if a
        ],
    )

    # -- 父风险级联（第 5.4 节字段规则） --
    if goplus.is_open_source == "0":
        _apply_parent_risk(result, "open_source")
    if goplus.is_in_dex == "0":
        _apply_parent_risk(result, "in_dex")

    result.recompute()
    return result


def _fake_token_flag(goplus: EvmSecurity) -> str | None:
    if goplus.fake_token is None:
        return None
    value = goplus.fake_token.value
    if value in (1, "1"):
        return "1"
    if value in (0, "0"):
        return "0"
    return "bad"


def _apply_parent_risk(result: SecurityResult, parent: str) -> None:
    for name in list(result.fields):
        if name == parent:
            continue
        reason_code = "PARENT_RISK_OPEN_SOURCE" if parent == "open_source" else "PARENT_RISK_NOT_IN_DEX"
        if result.fields[name].status == "UNKNOWN":
            result.fields[name] = FieldResult("NOT_APPLICABLE", reason_code)


def _bsc_lp_locked(goplus: EvmSecurity, main_pool_address: str | None, security_cfg) -> FieldResult:
    if not main_pool_address:
        return FieldResult("UNKNOWN", "NO_MAIN_POOL")
    matched = [d for d in goplus.dex if d.pair and d.pair.lower() == main_pool_address.lower()]
    if not matched:
        return FieldResult("UNKNOWN", "POOL_NOT_MATCHED")
    lp = matched[0]
    if lp.liquidity_type not in ("UniV2",):
        return FieldResult("NOT_APPLICABLE", "NOT_V2_POOL")
    locked = Decimal(0)
    for holder in goplus.lp_holders:
        if holder.is_locked == "1":
            locked += holder.percent or Decimal(0)
    if locked <= 0:
        return FieldResult("UNKNOWN", "NO_LOCKED_LP")
    pct = locked * 100
    if pct < security_cfg.lp_locked_pct_min:
        return FieldResult("RISK", "LP_LOCKED_TOO_LOW", f"{pct}%")
    return FieldResult("SAFE", detail=f"{pct}%")


# ---------------------------------------------------------------------------
# Solana（总控文档第 5.5、5.6 节）
# ---------------------------------------------------------------------------


def evaluate_solana(
    *,
    token_info: TokenInfo,
    goplus: SolSecurity,
    security_cfg,
    main_pool_address: str | None,
    coin_holders: TopHolders | None,
) -> SecurityResult:
    result = SecurityResult()

    # -- CoinGecko 门禁（第 5.5 节） --
    gt = token_info.gt_score
    if gt is None:
        result.set("gt_score", "UNKNOWN", "GT_SCORE_MISSING")
    elif gt < Decimal(security_cfg.gt_score_min):
        result.set("gt_score", "RISK", "GT_SCORE_LOW", f"gt={gt}")
    else:
        result.set("gt_score", "SAFE")

    honeypot = token_info.is_honeypot
    if honeypot == "unknown" or honeypot is None:
        result.set("honeypot_cg", "NOT_APPLICABLE", "SOLANA_HONEYPOT_CHECK_UNSUPPORTED")
    elif isinstance(honeypot, bool):
        result.set("honeypot_cg", "UNKNOWN", "CONTRACT_CHANGE", f"value={honeypot!r}")
    else:
        result.set("honeypot_cg", "UNKNOWN", "CONTRACT_CHANGE", f"value={honeypot!r}")

    for name, value in [("mint_authority", token_info.mint_authority), ("freeze_authority", token_info.freeze_authority)]:
        if value is None:
            result.set(name, "UNKNOWN", "AUTHORITY_MISSING")
        elif value == "no":
            result.set(name, "SAFE")
        else:
            result.set(name, "RISK", "AUTHORITY_PRESENT", str(value))

    # developer holding
    dev = token_info.developer_holding_percentage
    if dev is None:
        result.set("developer_holding_pct", "UNKNOWN", "DEV_HOLDING_MISSING")
    elif dev > security_cfg.developer_holding_pct_max:
        result.set("developer_holding_pct", "RISK", "DEV_HOLDING_TOO_HIGH", f"{dev}%")
    else:
        result.set("developer_holding_pct", "SAFE")

    # -- GoPlus 固定拒绝与策略动作（第 5.6 节） --
    _sol_status_field("mintable", goplus.mintable, result, action="reject")
    _sol_status_field("freezable", goplus.freezable, result, action="reject")
    _sol_status_field("closable", goplus.closable, result, action=security_cfg.closable_action)
    _sol_status_field("metadata_mutable", goplus.metadata_mutable, result, action=security_cfg.metadata_mutable_action)
    _sol_status_field("balance_mutable", goplus.balance_mutable_authority, result, action=security_cfg.balance_mutable_action)
    _sol_status_field("default_state_upgradable", goplus.default_account_state_upgradable, result, action=security_cfg.default_state_upgradable_action)
    _sol_status_field("transfer_fee_upgradable", goplus.transfer_fee_upgradable, result, action=security_cfg.transfer_fee_upgradable_action)
    _sol_status_field("transfer_hook", _hook_field(goplus), result, action=security_cfg.transfer_hook_action)
    _sol_status_field("transfer_hook_upgradable", goplus.transfer_hook_upgradable, result, action=security_cfg.token2022_risks_action)

    if goplus.default_account_state == "2":
        result.set("default_account_frozen", "RISK", "DEFAULT_FROZEN")
    elif goplus.default_account_state is None:
        result.set("default_account_frozen", "UNKNOWN", "FIELD_MISSING")
    else:
        result.set("default_account_frozen", "SAFE")

    if goplus.non_transferable == "1":
        result.set("non_transferable", "RISK")
    elif goplus.non_transferable == "0":
        result.set("non_transferable", "SAFE")
    else:
        result.set("non_transferable", "UNKNOWN", "FIELD_MISSING")

    # transfer fee bps
    if goplus.transfer_fee is None:
        result.set("transfer_fee_bps", "UNKNOWN", "FIELD_MISSING")
    else:
        current = goplus.transfer_fee.current_fee_rate
        scheduled_max = Decimal(0)
        for item in goplus.transfer_fee.scheduled_fee_rate or []:
            rate = _to_decimal_safe(item.get("fee_rate"))
            if rate is not None:
                scheduled_max = max(scheduled_max, rate)
        current_rate = current.fee_rate if current and current.fee_rate is not None else None
        if current_rate is None and scheduled_max == 0:
            result.set("transfer_fee_bps", "SAFE", "NO_FEE")
        else:
            effective = max(current_rate or Decimal(0), scheduled_max)
            bps = effective * 10000
            if bps > security_cfg.transfer_fee_bps_max:
                result.set("transfer_fee_bps", "RISK", "TRANSFER_FEE_TOO_HIGH", f"{bps}bps")
            else:
                result.set("transfer_fee_bps", "SAFE")

    # Top 10（按 owner account 汇总，第 5.6 节）
    top10 = top10_holding_pct(
        coin_holders,
        goplus.holders,
        burn_addresses=BURN_ADDRESSES_SOL,
        exclude_addresses={main_pool_address} if main_pool_address else None,
    )
    result.set_value("top10_holding_pct", top10)
    if top10 is None:
        result.set("top10_holding_pct", "UNKNOWN", "NO_VALID_SOURCE")
    elif top10 > security_cfg.top10_holding_pct_max:
        result.set("top10_holding_pct", "RISK", "TOP10_TOO_HIGH", f"{top10}%")
    else:
        result.set("top10_holding_pct", "SAFE")

    # LP 保护（主池匹配，第 5.6 节 liquidity_lock_risk）
    lp_field = _sol_lp_locked(goplus, main_pool_address, security_cfg)
    result.set_field("lp_locked_pct", lp_field)
    result.set_value("lp_locked_pct", _pct_from_detail(lp_field))
    result.set_value(
        "developer_or_creator_holding_pct",
        token_info.developer_holding_percentage,
    )
    result.set_value(
        "creator_owner_addresses",
        [token_info.developer_address] if token_info.developer_address else [],
    )

    result.recompute()
    return result


def _sol_status_field(name: str, field_obj, result: SecurityResult, *, action: RiskAction) -> None:
    if field_obj is None:
        result.set(name, "UNKNOWN", "FIELD_MISSING")
        return
    status = getattr(field_obj, "status", None)
    authorities = getattr(field_obj, "authority", None) or []
    malicious = any(getattr(a, "malicious_address", None) == 1 for a in authorities)
    if malicious:
        result.set(name, "RISK", "MALICIOUS_AUTHORITY")
    elif status == "1":
        result.set(name, _action_to_status(action), "STRATEGY_ACTION", action)
    elif status == "0":
        result.set(name, "SAFE")
    else:
        result.set(name, "UNKNOWN", "FIELD_MISSING")


class _HookField:
    status = None
    authority = []

    def __init__(self, hooks):
        self.authority = hooks or []
        self.status = "1" if (hooks or []) else "0"


def _hook_field(goplus: SolSecurity):
    return _HookField(goplus.transfer_hook)


def _sol_lp_locked(goplus: SolSecurity, main_pool_address: str | None, security_cfg) -> FieldResult:
    if not main_pool_address:
        return FieldResult("UNKNOWN", "NO_MAIN_POOL")
    matched = [d for d in goplus.dex if d.id == main_pool_address]
    if not matched:
        return FieldResult("UNKNOWN", "POOL_NOT_MATCHED")
    lp = matched[0]
    protected = lp.burn_percent or Decimal(0)
    if lp.lp_amount and lp.lp_amount > 0:
        locked = Decimal(0)
        for holder in goplus.lp_holders:
            if holder.percent is not None and getattr(holder, "is_locked", 0) == 1:
                locked += holder.percent
        locked = min(locked, Decimal(100))
        protected = min(protected + locked, Decimal(100))
    if protected <= 0:
        return FieldResult("UNKNOWN", "NO_PROTECTION_DATA")
    if protected < security_cfg.lp_locked_pct_min:
        return FieldResult("RISK", "LP_LOCKED_TOO_LOW", f"{protected}%")
    return FieldResult("SAFE", detail=f"{protected}%")


def _to_decimal_safe(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _pct_from_detail(field: FieldResult) -> Decimal | None:
    """从 FieldResult.detail（如 "95%"）解析百分比数值。"""
    if field.detail is None:
        return None
    try:
        return Decimal(str(field.detail).rstrip("%"))
    except Exception:
        return None


def apply_snapshot_ttl(
    result: SecurityResult, *, ttl_seconds: int, snapshot_age_seconds: float
) -> SecurityResult:
    """快照超过 TTL 时整体置 STALE（总控文档第 5.2 节）。"""
    if snapshot_age_seconds > ttl_seconds:
        for name in list(result.fields):
            if result.fields[name].status in ("SAFE", "NOT_APPLICABLE"):
                result.fields[name] = FieldResult("STALE", "SNAPSHOT_EXPIRED")
        result.trade_allowed = False
    return result
