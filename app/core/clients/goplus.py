"""GoPlus 安全接口 client 与归一化模型（总控文档第 5.4、5.6 节）。

- 双模式鉴权（文档第 5.2 节）：默认无 Authorization（客户端限速 30 次/分钟）；
  配置 GOPLUS_API_TOKEN 后使用 Authorization: Bearer，限速按 GOPLUS_RATE_PER_MINUTE。
- 归一化保留"字段是否存在"语义：布尔字段以字符串 "0"/"1" 保存，
  缺失为 None，由业务层映射 SAFE/RISK/UNKNOWN/NOT_APPLICABLE。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

BASE_URL = "https://api.gopluslabs.io"
DEFAULT_RATE_PER_MINUTE = 30
DEFAULT_TIMEOUT = 20.0


class GoPlusError(RuntimeError):
    pass


class GoPlusTimeout(GoPlusError):
    pass


class GoPlusRateLimited(GoPlusError):
    pass


class GoPlusContractError(GoPlusError):
    """HTTP 成功但业务数据缺失、类型错误或无法解析。"""


class GoPlusApiError(GoPlusError):
    pass


class RawModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise GoPlusContractError(f"非法数值: {value!r}") from None


def _flag(value: Any) -> str | None:
    """布尔字段归一化为 "0"/"1" 字符串；缺失为 None。

    官方文档声明字符串 "0"/"1"，实测部分字段（holders.is_locked 等）
    返回整数 0/1，两种形态都接受。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        if value in (0, 1):
            return str(value)
        raise GoPlusContractError(f"非法布尔字段值: {value!r}")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in ("0", "1"):
            return stripped
        raise GoPlusContractError(f"非法布尔字段值: {value!r}")
    raise GoPlusContractError(f"非法布尔字段值: {value!r}")


class TokenBucket:
    """简单 token bucket：全局限速、初始不突发（生产 4029 教训）。"""

    def __init__(
        self, rate: int = DEFAULT_RATE_PER_MINUTE, initial: float = 0.0
    ) -> None:
        self._rate = rate
        self._tokens = initial
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    float(self._rate),
                    self._tokens + (now - self._updated) * self._rate / 60.0,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * 60.0 / self._rate
            await asyncio.sleep(max(wait, 0.05))


# ---------------------------------------------------------------------------
# EVM 归一化模型（总控文档第 5.4 节）
# ---------------------------------------------------------------------------


class EvmHolder(RawModel):
    address: str | None = None
    is_locked: str | None = None
    tag: str | None = None
    is_contract: str | None = None
    balance: Decimal | None = None
    percent: Decimal | None = None
    locked_detail: list[dict] = Field(default_factory=list)


class EvmDex(RawModel):
    liquidity_type: str | None = None
    name: str | None = None
    liquidity: Decimal | None = None
    pair: str | None = None
    pool_manager: str | None = None
    pool_fee: Decimal | None = None


class EvmFakeToken(RawModel):
    true_token_address: str | None = None
    value: Any = None


class EvmSecurity(RawModel):
    token_address: str
    is_open_source: str | None = None
    is_proxy: str | None = None
    is_mintable: str | None = None
    owner_address: str | None = None
    can_take_back_ownership: str | None = None
    owner_change_balance: str | None = None
    hidden_owner: str | None = None
    selfdestruct: str | None = None
    external_call: str | None = None
    gas_abuse: str | None = None
    is_in_dex: str | None = None
    buy_tax: Decimal | None = None
    sell_tax: Decimal | None = None
    transfer_tax: Decimal | None = None
    cannot_buy: str | None = None
    cannot_sell_all: str | None = None
    slippage_modifiable: str | None = None
    is_honeypot: str | None = None
    transfer_pausable: str | None = None
    is_blacklisted: str | None = None
    is_whitelisted: str | None = None
    is_anti_whale: str | None = None
    anti_whale_modifiable: str | None = None
    trading_cooldown: str | None = None
    personal_slippage_modifiable: str | None = None
    is_airdrop_scam: str | None = None
    trust_list: str | None = None
    other_potential_risks: str | None = None
    note: str | None = None
    fake_token: EvmFakeToken | None = None
    holders: list[EvmHolder] = Field(default_factory=list)
    owner_balance: Decimal | None = None
    owner_percent: Decimal | None = None
    creator_address: str | None = None
    creator_balance: Decimal | None = None
    creator_percent: Decimal | None = None
    lp_holder_count: int | None = None
    lp_total_supply: Decimal | None = None
    lp_holders: list[EvmHolder] = Field(default_factory=list)
    dex: list[EvmDex] = Field(default_factory=list)


def parse_evm(token_address: str, raw: dict) -> EvmSecurity:
    if not isinstance(raw, dict):
        raise GoPlusContractError(f"EVM 结果条目类型错误: {token_address}")
    holders = []
    for item in raw.get("holders") or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("EVM holders 条目类型错误")
        holders.append(
            EvmHolder(
                address=item.get("address"),
                is_locked=_flag(item.get("is_locked")),
                tag=item.get("tag"),
                is_contract=_flag(item.get("is_contract")),
                balance=_dec(item.get("balance")),
                percent=_dec(item.get("percent")),
                locked_detail=item.get("locked_detail") or [],
            )
        )
    lp_holders = []
    for item in raw.get("lp_holders") or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("EVM lp_holders 条目类型错误")
        lp_holders.append(
            EvmHolder(
                address=item.get("address"),
                is_locked=_flag(item.get("is_locked")),
                tag=item.get("tag"),
                is_contract=_flag(item.get("is_contract")),
                balance=_dec(item.get("balance")),
                percent=_dec(item.get("percent")),
                locked_detail=item.get("locked_detail") or [],
            )
        )
    dexes = []
    for item in raw.get("dex") or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("EVM dex 条目类型错误")
        dexes.append(
            EvmDex(
                liquidity_type=item.get("liquidity_type"),
                name=item.get("name"),
                liquidity=_dec(item.get("liquidity")),
                pair=item.get("pair"),
                pool_manager=item.get("pool_manager"),
                pool_fee=_dec(item.get("pool_fee")),
            )
        )
    fake = raw.get("fake_token")
    return EvmSecurity(
        token_address=token_address,
        is_open_source=_flag(raw.get("is_open_source")),
        is_proxy=_flag(raw.get("is_proxy")),
        is_mintable=_flag(raw.get("is_mintable")),
        owner_address=raw.get("owner_address"),
        can_take_back_ownership=_flag(raw.get("can_take_back_ownership")),
        owner_change_balance=_flag(raw.get("owner_change_balance")),
        hidden_owner=_flag(raw.get("hidden_owner")),
        selfdestruct=_flag(raw.get("selfdestruct")),
        external_call=_flag(raw.get("external_call")),
        gas_abuse=_flag(raw.get("gas_abuse")),
        is_in_dex=_flag(raw.get("is_in_dex")),
        buy_tax=_dec(raw.get("buy_tax")),
        sell_tax=_dec(raw.get("sell_tax")),
        transfer_tax=_dec(raw.get("transfer_tax")),
        cannot_buy=_flag(raw.get("cannot_buy")),
        cannot_sell_all=_flag(raw.get("cannot_sell_all")),
        slippage_modifiable=_flag(raw.get("slippage_modifiable")),
        is_honeypot=_flag(raw.get("is_honeypot")),
        transfer_pausable=_flag(raw.get("transfer_pausable")),
        is_blacklisted=_flag(raw.get("is_blacklisted")),
        is_whitelisted=_flag(raw.get("is_whitelisted")),
        is_anti_whale=_flag(raw.get("is_anti_whale")),
        anti_whale_modifiable=_flag(raw.get("anti_whale_modifiable")),
        trading_cooldown=_flag(raw.get("trading_cooldown")),
        personal_slippage_modifiable=_flag(raw.get("personal_slippage_modifiable")),
        is_airdrop_scam=_flag(raw.get("is_airdrop_scam")),
        trust_list=_flag(raw.get("trust_list")),
        other_potential_risks=raw.get("other_potential_risks"),
        note=raw.get("note"),
        fake_token=EvmFakeToken(**fake) if isinstance(fake, dict) else None,
        holders=holders,
        owner_balance=_dec(raw.get("owner_balance")),
        owner_percent=_dec(raw.get("owner_percent")),
        creator_address=raw.get("creator_address"),
        creator_balance=_dec(raw.get("creator_balance")),
        creator_percent=_dec(raw.get("creator_percent")),
        lp_holder_count=raw.get("lp_holder_count"),
        lp_total_supply=_dec(raw.get("lp_total_supply")),
        lp_holders=lp_holders,
        dex=dexes,
    )


# ---------------------------------------------------------------------------
# Solana 归一化模型（总控文档第 5.6 节）
# ---------------------------------------------------------------------------


class SolAuthority(RawModel):
    address: str | None = None
    malicious_address: int | None = None


class SolStatusField(RawModel):
    status: str | None = None
    authority: list[SolAuthority] = Field(default_factory=list)


class SolHolder(RawModel):
    balance: Decimal | None = None
    is_locked: int | None = None
    locked_detail: list[dict] = Field(default_factory=list)
    percent: Decimal | None = None
    tag: str | None = None
    token_account: str | None = None


class SolDex(RawModel):
    burn_percent: Decimal | None = None
    dex_name: str | None = None
    fee_rate: Decimal | None = None
    id: str | None = None
    lp_amount: Decimal | None = None
    open_time: Decimal | None = None
    price: Decimal | None = None
    tvl: Decimal | None = None
    type: str | None = None


class SolTransferFeeRate(RawModel):
    fee_rate: Decimal | None = None
    maximum_fee: Decimal | None = None


class SolTransferFee(RawModel):
    current_fee_rate: SolTransferFeeRate | None = None
    scheduled_fee_rate: list[dict] = Field(default_factory=list)


class SolSecurity(RawModel):
    token_address: str
    balance_mutable_authority: SolStatusField | None = None
    closable: SolStatusField | None = None
    creators: list[SolAuthority] = Field(default_factory=list)
    default_account_state: str | None = None
    default_account_state_upgradable: SolStatusField | None = None
    dex: list[SolDex] = Field(default_factory=list)
    freezable: SolStatusField | None = None
    holders: list[SolHolder] = Field(default_factory=list)
    lp_holders: list[SolHolder] = Field(default_factory=list)
    metadata_mutable: SolStatusField | None = None
    mintable: SolStatusField | None = None
    non_transferable: str | None = None
    transfer_fee: SolTransferFee | None = None
    transfer_fee_upgradable: SolStatusField | None = None
    transfer_hook: list[SolAuthority] = Field(default_factory=list)
    transfer_hook_upgradable: SolStatusField | None = None
    trusted_token: int | None = None


def _status_field(raw: dict | None) -> SolStatusField | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise GoPlusContractError("权限对象类型错误")
    authorities = []
    for item in raw.get("authority") or raw.get("metadata_upgrade_authority") or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("权限 authority 条目类型错误")
        authorities.append(
            SolAuthority(
                address=item.get("address"),
                malicious_address=item.get("malicious_address"),
            )
        )
    return SolStatusField(status=raw.get("status"), authority=authorities)


def _holders(raw: list | None) -> list[SolHolder]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("Solana holders 条目类型错误")
        out.append(
            SolHolder(
                balance=_dec(item.get("balance")),
                is_locked=item.get("is_locked"),
                locked_detail=item.get("locked_detail") or [],
                percent=_dec(item.get("percent")),
                tag=item.get("tag"),
                token_account=item.get("token_account"),
            )
        )
    return out


def parse_solana(token_address: str, raw: dict) -> SolSecurity:
    if not isinstance(raw, dict):
        raise GoPlusContractError(f"Solana 结果条目类型错误: {token_address}")
    dexes = []
    for item in raw.get("dex") or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("Solana dex 条目类型错误")
        dexes.append(
            SolDex(
                burn_percent=_dec(item.get("burn_percent")),
                dex_name=item.get("dex_name"),
                fee_rate=_dec(item.get("fee_rate")),
                id=item.get("id"),
                lp_amount=_dec(item.get("lp_amount")),
                open_time=_dec(item.get("open_time")),
                price=_dec(item.get("price")),
                tvl=_dec(item.get("tvl")),
                type=item.get("type"),
            )
        )
    transfer_fee = raw.get("transfer_fee")
    fee_model = None
    if isinstance(transfer_fee, dict):
        current = transfer_fee.get("current_fee_rate")
        fee_model = SolTransferFee(
            current_fee_rate=(
                SolTransferFeeRate(
                    fee_rate=_dec(current.get("fee_rate")),
                    maximum_fee=_dec(current.get("maximum_fee")),
                )
                if isinstance(current, dict)
                else None
            ),
            scheduled_fee_rate=transfer_fee.get("scheduled_fee_rate") or [],
        )
    hooks = []
    for item in raw.get("transfer_hook") or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("transfer_hook 条目类型错误")
        hooks.append(
            SolAuthority(
                address=item.get("address"),
                malicious_address=item.get("malicious_address"),
            )
        )
    non_transferable = raw.get("non_transferable")
    if non_transferable is None:
        non_transferable = raw.get("none_transferable")
    return SolSecurity(
        token_address=token_address,
        balance_mutable_authority=_status_field(raw.get("balance_mutable_authority")),
        closable=_status_field(raw.get("closable")),
        creators=_authorities(raw.get("creators")),
        default_account_state=raw.get("default_account_state"),
        default_account_state_upgradable=_status_field(
            raw.get("default_account_state_upgradable")
        ),
        dex=dexes,
        freezable=_status_field(raw.get("freezable")),
        holders=_holders(raw.get("holders")),
        lp_holders=_holders(raw.get("lp_holders")),
        metadata_mutable=_status_field(raw.get("metadata_mutable")),
        mintable=_status_field(raw.get("mintable")),
        non_transferable=non_transferable,
        transfer_fee=fee_model,
        transfer_fee_upgradable=_status_field(raw.get("transfer_fee_upgradable")),
        transfer_hook=hooks,
        transfer_hook_upgradable=_status_field(raw.get("transfer_hook_upgradable")),
        trusted_token=raw.get("trusted_token"),
    )


def _authorities(raw: list | None) -> list[SolAuthority]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise GoPlusContractError("authority 条目类型错误")
        out.append(
            SolAuthority(
                address=item.get("address"),
                malicious_address=item.get("malicious_address"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class GoPlusClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        api_token: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        rate_per_minute: int | None = None,
    ) -> None:
        # 鉴权优先级（文档第 5.2 节）：显式参数 > 环境变量；全部缺失则无鉴权
        self._api_token = (
            api_token
            if api_token is not None
            else os.environ.get("GOPLUS_API_TOKEN")
        )
        self._app_key = (
            app_key if app_key is not None else os.environ.get("GOPLUS_APP_KEY")
        )
        self._app_secret = (
            app_secret
            if app_secret is not None
            else os.environ.get("GOPLUS_APP_SECRET")
        )
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, transport=transport, timeout=timeout
        )
        rate = rate_per_minute
        if rate is None:
            rate = int(os.environ.get("GOPLUS_RATE_PER_MINUTE", DEFAULT_RATE_PER_MINUTE))
        self._bucket = TokenBucket(rate=rate)
        self.rate_per_minute = rate

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GoPlusClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- 鉴权 ---------------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        if self._api_token:
            return {"Authorization": f"Bearer {self._api_token}"}
        if self._app_key and self._app_secret:
            token = await self._ensure_access_token()
            return {"Authorization": f"Bearer {token}"}
        return {}

    async def _ensure_access_token(self) -> str:
        """APP_KEY + APP_SECRET 签名换 access_token（官方契约：
        sign = sha1(app_key + time + app_secret)，time 为秒级时间戳）。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and now < self._token_expires_at:
                return self._access_token
            ts = int(time.time())
            sign = hashlib.sha1(
                f"{self._app_key}{ts}{self._app_secret}".encode()
            ).hexdigest()
            response = await self._client.post(
                "/api/v1/token",
                json={"app_key": self._app_key, "sign": sign, "time": ts},
            )
            if response.status_code != 200:
                raise GoPlusApiError(f"access token 请求失败 {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                raise GoPlusContractError("access token 响应非 JSON") from exc
            if body.get("code") != 1:
                raise GoPlusContractError(
                    f"access token 业务失败 code={body.get('code')} "
                    f"message={body.get('message')}"
                )
            result = body.get("result") or {}
            self._access_token = result.get("access_token")
            if not self._access_token:
                raise GoPlusContractError("access token 响应缺少 access_token")
            expires_in = int(result.get("expires_in", 7200))
            self._token_expires_at = time.time() + expires_in * 0.8
            return self._access_token

    async def evm_token_security(
        self, chain_id: int, contract_addresses: list[str]
    ) -> dict[str, EvmSecurity]:
        await self._bucket.acquire()
        body = await self._get(
            f"/api/v1/token_security/{chain_id}",
            {"contract_addresses": ",".join(contract_addresses)},
        )
        result = body.get("result")
        if not isinstance(result, dict):
            raise GoPlusContractError("EVM 响应缺少 result 对象")
        out: dict[str, EvmSecurity] = {}
        for address in contract_addresses:
            key = address.lower()
            raw = result.get(key)
            if raw is None:
                raise GoPlusContractError(f"EVM 响应缺少地址条目: {address}")
            out[address] = parse_evm(address, raw)
        return out

    async def solana_token_security(
        self, contract_addresses: list[str]
    ) -> dict[str, SolSecurity]:
        await self._bucket.acquire()
        body = await self._get(
            "/api/v1/solana/token_security",
            {"contract_addresses": ",".join(contract_addresses)},
        )
        result = body.get("result")
        if not isinstance(result, dict):
            raise GoPlusContractError("Solana 响应缺少 result 对象")
        out: dict[str, SolSecurity] = {}
        for address in contract_addresses:
            raw = result.get(address)
            if raw is None:
                raise GoPlusContractError(f"Solana 响应缺少地址条目: {address}")
            out[address] = parse_solana(address, raw)
        return out

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        headers = await self._auth_headers()
        try:
            response = await self._client.get(path, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise GoPlusTimeout(f"{path} 超时") from exc
        if response.status_code == 429:
            raise GoPlusRateLimited(f"{path} 限流 (429)")
        if response.status_code >= 500:
            raise GoPlusApiError(f"{path} 服务端错误 {response.status_code}")
        if response.status_code != 200:
            raise GoPlusApiError(f"{path} 状态码 {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise GoPlusContractError(f"{path} 响应非 JSON") from exc
        code = body.get("code")
        if code == 4029:
            raise GoPlusRateLimited(f"{path} 业务限流 (4029)")
        if code != 1:
            raise GoPlusContractError(
                f"{path} 业务失败 code={body.get('code')} message={body.get('message')}"
            )
        return body
