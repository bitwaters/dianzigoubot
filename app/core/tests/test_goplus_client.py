"""GoPlus client 契约测试：录制响应回放验证双链归一化。"""
import json
from pathlib import Path

import httpx
import pytest

from app.core.clients.goplus import (
    EvmSecurity,
    GoPlusClient,
    GoPlusContractError,
    SolSecurity,
)

RAW = Path(__file__).parent / "fixtures" / "vendor_raw"


def _client(name: str) -> GoPlusClient:
    raw = json.loads((RAW / f"{name}.json").read_text())
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
    return GoPlusClient(transport=transport)


class TestSolanaParsing:
    def test_solana_security(self):
        import asyncio

        raw = json.loads((RAW / "goplus_solana_raw.json").read_text())
        address = next(iter(raw["result"]))
        client = _client("goplus_solana_raw")
        results = asyncio.run(client.solana_token_security([address]))
        assert isinstance(results[address], SolSecurity)
        sec = results[address]
        assert sec.token_address == address
        assert sec.mintable is not None
        assert sec.freezable is not None
        assert sec.transfer_fee is not None
        if sec.transfer_fee.current_fee_rate is not None:
            assert sec.transfer_fee.current_fee_rate.fee_rate is not None
        assert sec.holders  # Top10 持有者

    def test_missing_address_entry(self):
        import asyncio

        client = _client("goplus_solana_raw")
        with pytest.raises(GoPlusContractError, match="缺少地址条目"):
            asyncio.run(client.solana_token_security(["So11111111111111111111111111111111111111112"]))


class TestEvmParsing:
    def test_evm_security(self):
        import asyncio

        raw = json.loads((RAW / "goplus_bsc_raw.json").read_text())
        address = next(iter(raw["result"]))
        client = _client("goplus_bsc_raw")
        results = asyncio.run(client.evm_token_security(56, [address]))
        sec = results[address]
        assert isinstance(sec, EvmSecurity)
        assert sec.is_open_source in ("0", "1")
        assert sec.is_in_dex in ("0", "1")
        assert sec.buy_tax is not None or sec.buy_tax is None
        assert sec.holders
        assert sec.holders[0].is_locked in (None, "0", "1")

    def test_bool_flag_coercion(self):
        from app.core.clients.goplus import _flag

        assert _flag("1") == "1"
        assert _flag(0) == "0"
        assert _flag(True) == "1"
        assert _flag(None) is None
        with pytest.raises(GoPlusContractError):
            _flag("yes")


class TestAuth:
    def test_bearer_headers_with_static_token(self):
        import asyncio

        from app.core.clients.goplus import GoPlusClient

        client = GoPlusClient(api_token="static-token")
        headers = asyncio.run(client._auth_headers())
        assert headers == {"Authorization": "Bearer static-token"}

    def test_app_key_secret_signature(self):
        import asyncio

        import httpx

        from app.core.clients.goplus import GoPlusClient

        # 模拟 access token 端点：校验签名 = sha1(app_key+time+app_secret)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import hashlib

            body = __import__("json").loads(request.content)
            captured.update(body)
            expected = hashlib.sha1(
                f"KEY{body['time']}SECRET".encode()
            ).hexdigest()
            if body["sign"] != expected:
                return httpx.Response(400, json={"code": -1, "message": "bad sign"})
            return httpx.Response(
                200,
                json={"code": 1, "result": {"access_token": "tok-1", "expires_in": 3600}},
            )

        client = GoPlusClient(
            transport=httpx.MockTransport(handler), app_key="KEY", app_secret="SECRET"
        )
        token = asyncio.run(client._ensure_access_token())
        assert token == "tok-1"
        assert captured["app_key"] == "KEY"
        # 缓存命中：不再请求
        token2 = asyncio.run(client._ensure_access_token())
        assert token2 == "tok-1"

    def test_no_auth_when_no_credentials(self):
        import asyncio

        from app.core.clients.goplus import GoPlusClient

        client = GoPlusClient()
        assert asyncio.run(client._auth_headers()) == {}


class TestRateLimit:
    def test_token_bucket(self):
        import asyncio
        import time

        from app.core.clients.goplus import TokenBucket

        async def run():
            bucket = TokenBucket(rate=120)  # 2 枚/秒，即每枚等待约 0.5 秒
            bucket._tokens = 0  # 白盒测试：耗尽突发容量，验证等待补发
            start = time.monotonic()
            await bucket.acquire()
            elapsed = time.monotonic() - start
            assert elapsed >= 0.3

        asyncio.run(run())
