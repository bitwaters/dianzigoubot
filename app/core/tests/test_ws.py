"""WebSocket client 测试：协议握手、K 线聚合、退避重连、鉴权失败、准入控制。"""
import asyncio
import json
from decimal import Decimal

import pytest
import websockets

from app.core.clients.coingecko_ws import (
    Candle,
    CoinGeckoWS,
    DailyCreditGate,
    G3CandleBuffer,
    WsAuthError,
)


class FakeServer:
    """模拟 CoinGecko WS 服务端：欢迎、订阅确认、set 确认、推送事件。"""

    def __init__(self, *, events: list[dict], auth_code: int = 3000, close_after_handshake: bool = False):
        self.events = events
        self.auth_code = auth_code
        self.close_after_handshake = close_after_handshake
        self.server = None
        self.port = 0

    async def __aenter__(self):
        async def handler(ws):
            if self.auth_code != 3000:
                await ws.send(json.dumps({"code": self.auth_code, "message": "bad"}))
                await ws.close()
                return
            await ws.send(json.dumps({"code": 3000, "message": "ok"}))
            await ws.send(json.dumps({"type": "welcome", "sid": "s1"}))
            if self.close_after_handshake:
                await ws.close()
                return
            for _ in range(2):  # 两次订阅确认
                await ws.recv()
                identifier = json.loads(json.loads(json.dumps("{}"))) or ""
                await ws.send(json.dumps({"type": "confirm_subscription", "identifier": '{"channel":"x"}'}))
            # set 确认
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    if '"command":"message"' in msg:
                        await ws.send(json.dumps({"code": 2000, "message": "ok"}))
                    break
            except asyncio.TimeoutError:
                pass
            for event in self.events:
                await ws.send(json.dumps(event))
            await asyncio.sleep(0.2)
            await ws.close()

        self.server = await websockets.serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


class TestCandleBuffer:
    def _candle(self, ts, close, final=False):
        return Candle(open_ts_ms=ts, open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(close), volume=Decimal(1), final=final)

    def test_upsert_no_volume_accumulate(self):
        buf = G3CandleBuffer()
        c1 = self._candle(1000, Decimal("1.5"))
        c2 = self._candle(1000, Decimal("1.6"))
        buf.upsert("P", "base", c1)
        buf.upsert("P", "base", c2)
        window = buf.window("P", "base", 1)
        assert window is None  # 未 final
        c1.final = True
        c2.final = True
        buf.upsert("P", "base", c2)
        window = buf.window("P", "base", 1)
        assert window[0].close == Decimal("1.6")

    def test_final_sweep(self):
        buf = G3CandleBuffer()
        buf.upsert("P", "base", self._candle(1000, Decimal("1")))
        buf.upsert("P", "base", self._candle(2000, Decimal("1.2")))
        buf.sweep_final(4000)
        assert buf.window("P", "base", 2) is not None

    def test_gap_rejected(self):
        buf = G3CandleBuffer()
        for ts in (1000, 2000, 4000):  # 缺 3000
            buf.upsert("P", "base", self._candle(ts, Decimal("1")))
        buf.sweep_final(10000)
        assert buf.window("P", "base", 3) is None

    def test_insufficient(self):
        buf = G3CandleBuffer()
        buf.upsert("P", "base", self._candle(1000, Decimal("1")))
        buf.sweep_final(10000)
        assert buf.window("P", "base", 2) is None


class TestProtocol:
    async def test_events_received(self):
        events = [
            {"ch": "G3", "n": "solana", "pa": "P1", "to": "base", "i": "1s",
             "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "t": 1780841100},
            {"c": "G1", "n": "solana", "ta": "T1", "p": 1.23, "t": 1780841100},
        ]
        got = []
        async with FakeServer(events=events) as server:
            client = CoinGeckoWS(
                "k", "solana",
                on_g1_event=lambda e: got.append(("g1", e.token_address, e.price_usd)),
                on_g3_event=lambda e: got.append(("g3", e.pool_address, e.candle)),
            )
            client._url = f"ws://127.0.0.1:{server.port}"
            client.api_key = ""
            client.start()
            for _ in range(200):
                if len(got) >= 2:
                    break
                await asyncio.sleep(0.02)
            await client.stop()
        assert got[0][0] == "g3"
        assert got[0][1] == "P1"
        assert got[1][0] == "g1"
        assert got[1][2] == Decimal("1.23")

    async def test_auth_failure_fatal(self):
        async with FakeServer(events=[], auth_code=4001) as server:
            client = CoinGeckoWS("bad-key", "solana")
            client._url = f"ws://127.0.0.1:{server.port}"
            client.start()
            for _ in range(200):
                if client.state == "FATAL":
                    break
                await asyncio.sleep(0.02)
            await client.stop()
            assert isinstance(client.fatal_error, WsAuthError)


class TestBackoff:
    async def test_backoff_sequence(self):
        from app.core.clients.coingecko_ws import BACKOFF_SEQUENCE

        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        async with FakeServer(events=[], close_after_handshake=True) as server:
            client = CoinGeckoWS("k", "solana", sleep_fn=fake_sleep)
            client._url = f"ws://127.0.0.1:{server.port}"
            client.start()
            for _ in range(500):
                if len(sleeps) >= 5:
                    break
                await asyncio.sleep(0.02)
            await client.stop()
        assert len(sleeps) >= 5
        # 每次失败后 index 递增：sleeps[i] 对应序列第 i 档 ±20% 抖动
        for i, slept in enumerate(sleeps[:5]):
            base = BACKOFF_SEQUENCE[i]
            assert abs(slept - base) <= base * 0.2 + 0.05, f"档位 {i}: {slept} vs {base}"


class TestAdmission:
    def test_g3_cap(self):
        gate = DailyCreditGate(Decimal("999999"))
        client = CoinGeckoWS("k", "solana", credit_gate=gate)
        for i in range(100):
            assert client.add_g3_pool(f"P{i}", "base") is True
        assert client.add_g3_pool("P100", "base") is False

    def test_budget_exhausted(self):
        gate = DailyCreditGate(Decimal("3"))
        client = CoinGeckoWS("k", "solana", credit_gate=gate)
        gate.charge(3)
        assert client.add_g3_pool("P1", "base") is False
