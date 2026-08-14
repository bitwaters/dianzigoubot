"""CoinGecko client 契约测试：以录制 raw 响应回放验证归一化解析。"""
import json
from pathlib import Path

import httpx
import pytest

from app.core.clients import coingecko
from app.core.clients.coingecko import (
    CoinGeckoApiError,
    CoinGeckoClient,
    CoinGeckoContractError,
    CoinGeckoRateLimited,
    CoinGeckoTimeout,
)

RAW = Path(__file__).parent / "fixtures" / "vendor_raw"

SAMPLE = None


def _sample():
    global SAMPLE
    if SAMPLE is None:
        mf = json.loads((RAW / "megafilter_solana.json").read_text())
        included = {i["id"]: i for i in mf.get("included", [])}
        for item in mf.get("data", []):
            rel = item.get("relationships", {})
            base_id = (rel.get("base_token") or {}).get("data", {}).get("id")
            base = included.get(base_id, {}).get("attributes", {})
            if item["attributes"].get("address") and base.get("address"):
                SAMPLE = (item["attributes"]["address"], base["address"])
                break
    assert SAMPLE is not None
    return SAMPLE


def _client_for(*files: str) -> CoinGeckoClient:
    handler = {}
    for name in files:
        body = (RAW / f"{name}.json").read_text(encoding="utf-8")
        handler[name] = httpx.Response(200, json=json.loads(body))
    transport = httpx.MockTransport(
        lambda request: handler.get(request.url.path.rsplit("/", 1)[-1])
        or httpx.Response(404, json={})
    )
    return CoinGeckoClient("test-key", transport=transport)


class TestContractParsing:
    def test_key(self):
        client = _client_for("key")
        # key 接口路径固定 /key，MockTransport 按键名匹配不适用
        raw = json.loads((RAW / "key.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        import asyncio

        info = asyncio.run(client.key())
        assert info.plan == "Analyst"
        assert info.rate_limit_request_per_minute == 500
        assert info.current_remaining_monthly_calls is not None

    def test_megafilter(self):
        import asyncio

        raw = json.loads((RAW / "megafilter_solana.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        pools = asyncio.run(client.megafilter({"networks": "solana"}))
        assert len(pools) == 20
        pool = pools[0]
        assert pool.address
        assert pool.base_token_address
        assert pool.quote_token_address
        assert pool.reserve_in_usd is not None
        assert pool.transactions["m5"].buys >= 0

    def test_trending_and_new(self):
        import asyncio

        for name in ("trending_solana", "new_pools_solana"):
            raw = json.loads((RAW / f"{name}.json").read_text())
            transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
            client = CoinGeckoClient("k", transport=transport)
            if name.startswith("trending"):
                pools = asyncio.run(client.trending_pools("solana"))
            else:
                pools = asyncio.run(client.new_pools("solana"))
            assert len(pools) == 20

    def test_pools_multi_volume_fields(self):
        import asyncio

        raw = json.loads((RAW / "pools_multi_raw.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        pool_address = raw["data"][0]["attributes"]["address"]
        pools = asyncio.run(
            client.pools_multi("solana", [pool_address], include_volume_breakdown=True)
        )
        assert len(pools) == 1
        detail = pools[0]
        # 契约测试锁定：include_volume_breakdown=true 时返回买卖/净买入 USD 聚合
        assert "m5" in detail.buy_volume_usd
        assert "m5" in detail.sell_volume_usd
        assert "m5" in detail.net_buy_volume_usd

    def test_tokens_multi(self):
        import asyncio

        raw = json.loads((RAW / "tokens_multi_raw.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        token = raw["data"][0]["attributes"]["address"]
        tokens = asyncio.run(client.tokens_multi("solana", [token]))
        assert tokens[0].address == token

    def test_top_pools_by_token(self):
        import asyncio

        raw = json.loads((RAW / "top_pools_by_token_raw.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        pools = asyncio.run(
            client.top_pools_by_token("solana", "t", include="base_token,quote_token,dex")
        )
        assert len(pools) > 0
        assert pools[0].base_token_address
        assert pools[0].quote_token_address

    def test_token_info(self):
        import asyncio

        raw = json.loads((RAW / "token_info_raw.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        info = asyncio.run(client.token_info("solana", "x"))
        assert info.address
        assert info.gt_score is not None
        assert isinstance(info.is_honeypot, (bool, str)) or info.is_honeypot is None

    def test_top_holders_and_traders(self):
        import asyncio

        for name in ("top_holders_raw", "top_traders_raw"):
            raw = json.loads((RAW / f"{name}.json").read_text())
            transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
            client = CoinGeckoClient("k", transport=transport)
            if "holders" in name:
                holders = asyncio.run(client.top_holders("solana", "x", holders=40))
                assert len(holders.holders) > 0
                assert holders.holders[0].address
            else:
                traders = asyncio.run(client.top_traders("solana", "x", traders=20))
                assert len(traders.traders) > 0
                assert traders.traders[0].address

    def test_pool_trades(self):
        import asyncio

        raw = json.loads((RAW / "pool_trades_raw.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        trades = asyncio.run(client.pool_trades("solana", "p", token="t"))
        assert len(trades) > 0
        assert trades[0].tx_hash
        assert trades[0].kind in ("buy", "sell")

    def test_pool_ohlcv(self):
        import asyncio

        raw = json.loads((RAW / "pool_ohlcv_raw.json").read_text())
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
        client = CoinGeckoClient("k", transport=transport)
        bars = asyncio.run(client.pool_ohlcv("solana", "p", timeframe="minute"))
        assert len(bars) > 0
        assert bars[0].timestamp_ms % 1000 == 0
        assert bars[0].open >= 0


class TestErrors:
    def _client(self, status: int, body) -> CoinGeckoClient:
        transport = httpx.MockTransport(lambda _: httpx.Response(status, json=body))
        return CoinGeckoClient("k", transport=transport)

    def test_rate_limited(self):
        import asyncio

        client = self._client(429, {})
        with pytest.raises(CoinGeckoRateLimited):
            asyncio.run(client.key())

    def test_server_error(self):
        import asyncio

        client = self._client(500, {})
        with pytest.raises(CoinGeckoApiError):
            asyncio.run(client.key())

    def test_non_json_contract_error(self):
        import asyncio

        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"<html>")
        )
        client = CoinGeckoClient("k", transport=transport)
        with pytest.raises(CoinGeckoContractError):
            asyncio.run(client.key())

    def test_bad_ohlcv_structure(self):
        import asyncio

        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"data": {"attributes": {"ohlcv_list": [[1, 2]]}}},
            )
        )
        client = CoinGeckoClient("k", transport=transport)
        with pytest.raises(CoinGeckoContractError):
            asyncio.run(client.pool_ohlcv("solana", "p"))


class TestUsageLedger:
    def test_rest_counting(self):
        ledger = coingecko.UsageLedger()
        ledger.record_rest("pools-megafilter")
        ledger.record_rest("pools-megafilter")
        ledger.record_ws_charged("G3")
        assert ledger.snapshot() == {"rest:pools-megafilter": 2}
        assert ledger.ws_total() == 1
        ledger.clear()
        assert ledger.snapshot() == {}
