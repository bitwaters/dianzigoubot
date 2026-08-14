"""CoinGecko WebSocket client（G1/G3，总控文档第 4.5 节）。

- 每条链一个连接，独立鉴权/订阅/心跳/重连；不因单链断线重建另一条链。
- ActionCable 报文契约：subscribe → confirm_subscription → set_tokens/set_pools。
- 断线按 1/2/4/8/15/30 秒指数退避 + 正负 20% 抖动；鉴权/契约拒绝停止该链重连。
- G3 1 秒 K 线：下一秒事件或 open+2 秒后标记 final；W5/W10 要求严格连续。
- 订阅准入：每频道 100 物理上限 + WS 每日预算。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable, Optional

import websockets

log = logging.getLogger(__name__)

WS_URL = "wss://stream.coingecko.com/v1"

BACKOFF_SEQUENCE = (1, 2, 4, 8, 15, 30)
BACKOFF_JITTER_PCT = 0.2
STABLE_RESET_SECONDS = 60
MAX_SUBSCRIPTIONS_PER_CHANNEL = 100

G1_CHANNEL = "OnchainSimpleTokenPrice"
G3_CHANNEL = "OnchainOHLCV"


class WsAuthError(RuntimeError):
    """鉴权失败或报文契约拒绝：停止该链重连。"""


class WsContractError(RuntimeError):
    """服务端报文与锁定契约不符。"""


@dataclass
class Candle:
    open_ts_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    final: bool = False


class G3CandleBuffer:
    """G3 1 秒 K 线内存缓冲（文档第 4.5 节事件规则）。"""

    def __init__(self, max_keep: int = 20) -> None:
        self._buffers: dict[tuple[str, str], dict[int, Candle]] = {}
        self.max_keep = max_keep

    def upsert(self, pool: str, side: str, candle: Candle) -> Candle:
        key = (pool, side)
        buf = self._buffers.setdefault(key, {})
        existing = buf.get(candle.open_ts_ms)
        if existing is not None:
            # 重复更新执行 UPSERT，不累加 volume
            existing.open = candle.open
            existing.high = candle.high
            existing.low = candle.low
            existing.close = candle.close
            existing.volume = candle.volume
            existing.final = existing.final or candle.final
            return existing
        buf[candle.open_ts_ms] = candle
        if len(buf) > self.max_keep:
            oldest = sorted(buf)[0]
            del buf[oldest]
        return candle

    def mark_final_upto(self, now_ms: int, pool: str, side: str) -> None:
        """open+2 秒后未收到下一根即标记 final（文档第 4.5 节）。"""
        buf = self._buffers.get((pool, side))
        if not buf:
            return
        for ts, candle in buf.items():
            if not candle.final and now_ms >= ts + 2000:
                candle.final = True

    def sweep_final(self, now_ms: int) -> None:
        for key in list(self._buffers):
            self.mark_final_upto(now_ms, *key)

    def window(self, pool: str, side: str, n: int) -> list[Candle] | None:
        """最新 n 根 final K 线；必须严格连续、无缺秒（文档第 4.5 节）。

        返回 None 表示 INCOMPLETE。
        """
        buf = self._buffers.get((pool, side))
        if not buf:
            return None
        final_bars = sorted(
            (c for c in buf.values() if c.final), key=lambda c: c.open_ts_ms
        )
        if len(final_bars) < n:
            return None
        window = final_bars[-n:]
        for prev, cur in zip(window, window[1:]):
            if cur.open_ts_ms - prev.open_ts_ms != 1000:
                return None
        return window

    def final_bars(self, pool: str, side: str) -> list[Candle]:
        """全部 final K 线（按时间升序）；防追高等待续评使用。"""
        buf = self._buffers.get((pool, side))
        if not buf:
            return []
        return sorted(
            (c for c in buf.values() if c.final), key=lambda c: c.open_ts_ms
        )

    def clear(self, pool: str, side: str) -> None:
        self._buffers.pop((pool, side), None)


@dataclass
class G1Event:
    network: str
    token_address: str
    price_usd: Decimal | None = None
    timestamp_ms: int | None = None


@dataclass
class G3Event:
    network: str
    pool_address: str
    side: str
    interval: str
    candle: Candle


class DailyCreditGate:
    """WS 每日预算准入（文档第 4.6 节简化版）；UTC 零点自动重置。"""

    def __init__(self, daily_budget: Decimal) -> None:
        self.daily_budget = daily_budget
        self.used = 0
        self._day = _utc_day_ms()

    @property
    def available(self) -> bool:
        self._rollover()
        return self.used < self.daily_budget

    def charge(self, count: int = 1) -> None:
        self._rollover()
        self.used += count

    def restore(self, used: int) -> None:
        self._rollover()
        self.used = max(self.used, used)

    def _rollover(self) -> None:
        day = _utc_day_ms()
        if day != self._day:
            self._day = day
            self.used = 0


def _utc_day_ms() -> int:
    """当日 UTC 零点（毫秒）。"""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    start = datetime.datetime(now.year, now.month, now.day, tzinfo=datetime.timezone.utc)
    return int(start.timestamp() * 1000)


class CoinGeckoWS:
    def __init__(
        self,
        api_key: str,
        network: str,
        *,
        on_g1_event: Callable[[G1Event], Awaitable[None]] | None = None,
        on_g3_event: Callable[[G3Event], Awaitable[None]] | None = None,
        credit_gate: DailyCreditGate | None = None,
        on_charged: Callable[[str], None] | None = None,
        on_state_change: Callable[[str, str], Awaitable[None]] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.network = network
        self._url = url or WS_URL
        self._on_g1 = on_g1_event
        self._on_g3 = on_g3_event
        self._credit_gate = credit_gate
        self._on_charged = on_charged
        self._on_state_change = on_state_change
        self._sleep = sleep_fn or asyncio.sleep

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._backoff_index = 0
        self._stable_since: float | None = None
        self._stopped = asyncio.Event()
        self._fatal: Exception | None = None

        self._g1_tokens: set[str] = set()
        self._g3_pools: set[tuple[str, str]] = set()  # (pool, side)
        self._pending_sets: list[dict] = []

        self._state = "IDLE"

    # -- 公共接口 -----------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def fatal_error(self) -> Exception | None:
        return self._fatal

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"ws-{self.network}")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def set_g1_tokens(self, tokens: set[str]) -> None:
        """以当前完整目标集合设置 G1（set_tokens）。"""
        self._g1_tokens = set(tokens)
        self._queue_set({"kind": "g1", "tokens": sorted(self._g1_tokens)})

    def set_g3_pools(self, pools: set[tuple[str, str]]) -> None:
        """以当前完整目标集合设置 G3（set_pools）；超过 100 物理上限截断告警。"""
        if len(pools) > MAX_SUBSCRIPTIONS_PER_CHANNEL:
            log.warning(
                "G3 订阅集合 %d 超过物理上限 100，截断",
                len(pools),
            )
            pools = set(sorted(pools)[:MAX_SUBSCRIPTIONS_PER_CHANNEL])
        self._g3_pools = set(pools)
        self._queue_set(
            {"kind": "g3", "pools": sorted(self._g3_pools), "interval": "1s"}
        )

    def add_g3_pool(self, pool: str, side: str) -> bool:
        """增补一个 G3 订阅；超过 100 上限或预算耗尽返回 False。"""
        if len(self._g3_pools) >= MAX_SUBSCRIPTIONS_PER_CHANNEL:
            return False
        if self._credit_gate is not None and not self._credit_gate.available:
            return False
        self._g3_pools.add((pool, side))
        self._queue_set(
            {"kind": "g3", "pools": sorted(self._g3_pools), "interval": "1s"}
        )
        return True

    def remove_g3_pool(self, pool: str, side: str) -> None:
        self._g3_pools.discard((pool, side))
        self._queue_set(
            {"kind": "g3", "pools": sorted(self._g3_pools), "interval": "1s"}
        )

    def _queue_set(self, payload: dict) -> None:
        self._pending_sets.append(payload)

    # -- 连接循环 -----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except (WsAuthError, WsContractError) as exc:
                self._fatal = exc
                self._set_state("FATAL")
                log.error("WS %s 致命错误，停止重连: %s", self.network, exc)
                return
            except Exception as exc:
                if self._stopped.is_set():
                    return
                self._set_state("RECONNECTING")
                log.warning("WS %s 断开: %s", self.network, exc)
            if self._stopped.is_set():
                return
            await self._backoff_sleep()
            self._backoff_index = min(
                self._backoff_index + 1, len(BACKOFF_SEQUENCE) - 1
            )

    async def _backoff_sleep(self) -> None:
        base = BACKOFF_SEQUENCE[self._backoff_index]
        jitter = base * BACKOFF_JITTER_PCT * (random.random() * 2 - 1)
        await self._sleep(max(base + jitter, 0.1))

    async def _connect_and_serve(self) -> None:
        uri = f"{self._url}?x_cg_pro_api_key={self.api_key}" if self.api_key else self._url
        async with websockets.connect(uri, open_timeout=15) as ws:
            self._ws = ws
            await self._expect_welcome(ws)
            await self._subscribe_channel(ws, G1_CHANNEL)
            await self._subscribe_channel(ws, G3_CHANNEL)
            self._reset_backoff()
            await self._replay_pending_sets(ws)
            self._set_state("CONNECTED")
            await self._serve(ws)

    async def _expect_welcome(self, ws) -> None:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if not isinstance(first, dict):
            raise WsContractError("欢迎报文非对象")
        if first.get("code") == 4001:
            raise WsAuthError("API Key 无效")
        if first.get("code") != 3000:
            raise WsContractError(f"连接被拒绝: {first}")
        second = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if not isinstance(second, dict) or second.get("type") != "welcome":
            raise WsContractError(f"缺少 welcome: {second}")

    async def _subscribe_channel(self, ws, channel: str) -> None:
        await ws.send(
            json.dumps(
                {"command": "subscribe", "identifier": json.dumps({"channel": channel})}
            )
        )
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if not isinstance(message, dict):
            raise WsContractError("订阅确认报文非对象")
        if message.get("type") != "confirm_subscription":
            if message.get("code") == 4001:
                raise WsAuthError("API Key 无效")
            raise WsContractError(f"{channel} 订阅确认失败: {message}")

    async def _replay_pending_sets(self, ws) -> None:
        while self._pending_sets:
            payload = self._pending_sets.pop(0)
            if payload["kind"] == "g1":
                data = json.dumps(
                    {
                        "network_id:token_addresses": [
                            f"{self.network}:{t}" for t in payload["tokens"]
                        ],
                        "action": "set_tokens",
                    }
                )
                identifier = G1_CHANNEL
                await ws.send(
                    json.dumps(
                        {
                            "command": "message",
                            "identifier": json.dumps({"channel": identifier}),
                            "data": data,
                        }
                    )
                )
                await self._await_set_ack(ws)
            else:
                # G3：按 side 分两条 set_pools 消息，避免重连后 quote 侧被翻转为 base
                for side in ("base", "quote"):
                    side_pools = [p for p, s in payload["pools"] if s == side]
                    if not side_pools:
                        continue
                    data = json.dumps(
                        {
                            "network_id:pool_addresses": [
                                f"{self.network}:{p}" for p in side_pools
                            ],
                            "interval": payload["interval"],
                            "token": side,
                            "action": "set_pools",
                        }
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "command": "message",
                                "identifier": json.dumps({"channel": G3_CHANNEL}),
                                "data": data,
                            }
                        )
                    )
                    await self._await_set_ack(ws)

    async def _await_set_ack(self, ws) -> None:
        try:
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except asyncio.TimeoutError:
            return
        if isinstance(ack, dict) and ack.get("code") not in (2000,):
            log.warning("WS %s set 确认异常: %s", self.network, ack)

    async def _serve(self, ws) -> None:
        while not self._stopped.is_set():
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                continue
            except (websockets.ConnectionClosed, OSError) as exc:
                raise exc
            if self._stable_since is None:
                self._stable_since = asyncio.get_event_loop().time()
            try:
                data = json.loads(message)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "ping":
                continue  # websockets 库已在协议层回复 pong
            await self._route(data)

    async def _route(self, data: dict) -> None:
        # 契约锁定：G1/G2 事件频道键为 "c"，G3 为 "ch"
        channel = data.get("ch") or data.get("c")
        if channel == "G1":
            event = G1Event(
                network=data.get("n", self.network),
                token_address=data.get("ta", ""),
                price_usd=_dec(data.get("p")),
                timestamp_ms=data.get("t"),
            )
            await _dispatch(self._on_g1, event)
            self._charge("G1")
        elif channel == "G3":
            open_ts = data.get("t")
            if open_ts is None:
                return
            candle = Candle(
                open_ts_ms=int(open_ts) * 1000,
                open=_dec(data.get("o")) or Decimal(0),
                high=_dec(data.get("h")) or Decimal(0),
                low=_dec(data.get("l")) or Decimal(0),
                close=_dec(data.get("c")) or Decimal(0),
                volume=_dec(data.get("v")) or Decimal(0),
            )
            event = G3Event(
                network=data.get("n", self.network),
                pool_address=data.get("pa", ""),
                side=data.get("to", "base"),
                interval=data.get("i", "1s"),
                candle=candle,
            )
            await _dispatch(self._on_g3, event)
            self._charge("G3")
        elif channel == "G2":
            self._charge("G2")

    def _charge(self, channel: str) -> None:
        if self._credit_gate is not None:
            self._credit_gate.charge()
        if self._on_charged is not None:
            self._on_charged(channel)

    def _reset_backoff(self) -> None:
        self._backoff_index = 0
        self._stable_since = None

    def _set_state(self, state: str) -> None:
        if self._state != state:
            self._state = state
            log.info("WS %s 状态: %s", self.network, state)
            if self._on_state_change is not None:
                asyncio.create_task(self._on_state_change(self.network, state))


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def _dispatch(fn, *args) -> None:
    """兼容同步/异步回调分发。"""
    if fn is None:
        return
    result = fn(*args)
    if asyncio.iscoroutine(result):
        await result
