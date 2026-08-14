#!/usr/bin/env python3
"""CoinGecko/GoPlus 能力探测脚本（总控文档第 3.1 节元数据规则）。

- 记录实际 Python 版本、脚本版本和 UTC 执行时间。
- 输出报告 JSON 至 tools/reports/，并为契约测试录制 fixture。
- 不定义应用运行时。
"""
from __future__ import annotations

import asyncio
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.clients.coingecko import CoinGeckoClient  # noqa: E402
from app.core.clients.goplus import GoPlusClient  # noqa: E402
from app.core.config import load_env_file  # noqa: E402

REPORTS_DIR = ROOT / "tools" / "reports"
FIXTURES_DIR = ROOT / "app" / "core" / "tests" / "fixtures" / "vendor"

SOLANA = "solana"
BSC = "bsc"
CHAIN_ID_BSC = 56


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def probe() -> dict:
    load_env_file()
    import os

    api_key = os.environ["COINGECKO_API_KEY"]
    report: dict = {"entries": {}}
    fixtures: dict[str, dict] = {}

    async def record(name: str, fn):
        try:
            result = await fn()
            if isinstance(result, tuple) and len(result) > 1:
                report["entries"][name] = {"ok": True, "summary": result[0]}
                fixtures[name] = result[1]
            else:
                report["entries"][name] = {"ok": True, "summary": result}
        except Exception as exc:  # 探测失败不中断
            report["entries"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async with CoinGeckoClient(api_key) as cg:
        await record("key", lambda: _key_probe(cg))
        await record("megafilter_solana", lambda: _megafilter_probe(cg, SOLANA))
        await record("trending_solana", lambda: _trending_probe(cg, SOLANA))
        await record("new_pools_solana", lambda: _new_pools_probe(cg, SOLANA))

        sample = await _sample(cg)
        if sample:
            pool_address, token_address = sample
            await record("pools_multi", lambda: _pools_multi_probe(cg, pool_address))
            await record("tokens_multi", lambda: _tokens_multi_probe(cg, token_address))
            await record("token_info", lambda: _token_info_probe(cg, token_address))
            await record("top_holders", lambda: _top_holders_probe(cg, token_address))
            await record("top_traders", lambda: _top_traders_probe(cg, token_address))
            await record("pool_trades", lambda: _pool_trades_probe(cg, pool_address, token_address))
            await record("pool_ohlcv", lambda: _pool_ohlcv_probe(cg, pool_address, token_address))
            await record("g2_onchaintrade", lambda: _g2_probe(pool_address))
            await record("g1_g3_live", lambda: _g1_g3_probe(pool_address, token_address))
        else:
            report["entries"]["sample"] = {"ok": False, "error": "无可用采样池"}

    async with GoPlusClient() as gp:
        await record("goplus_solana", lambda: _goplus_solana_probe(gp, token_address if sample else None))
        await record("goplus_bsc", lambda: _goplus_bsc_probe(gp))

    return report, fixtures


async def _key_probe(cg):
    info = await cg.key()
    return (
        {
            "plan": info.plan,
            "remaining": info.current_remaining_monthly_calls,
            "rpm": info.rate_limit_request_per_minute,
        },
        info.model_dump(mode="json"),
    )


async def _megafilter_probe(cg, network: str):
    pools = await cg.megafilter(
        {"networks": network, "sort": "m5_trending",
         "include": "base_token,quote_token,dex,network"}, pages=1
    )
    return {"count": len(pools)}, [p.model_dump(mode="json") for p in pools]


async def _trending_probe(cg, network: str):
    pools = await cg.trending_pools(
            network,
            duration="5m",
            include="base_token,quote_token,dex",
        )
    return {"count": len(pools)}, [p.model_dump(mode="json") for p in pools]


async def _new_pools_probe(cg, network: str):
    pools = await cg.new_pools(network, include="base_token,quote_token,dex")
    return {"count": len(pools)}, [p.model_dump(mode="json") for p in pools]


async def _sample(cg) -> tuple[str, str] | None:
    pools = await cg.megafilter(
        {"networks": SOLANA, "sort": "m5_trending",
         "include": "base_token,quote_token,dex,network"}, pages=1
    )
    for pool in pools:
        if pool.base_token_address and pool.address:
            return pool.address, pool.base_token_address
    return None


async def _pools_multi_probe(cg, pool_address: str):
    pools = await cg.pools_multi(SOLANA, [pool_address])
    return {"count": len(pools)}, [p.model_dump(mode="json") for p in pools]


async def _tokens_multi_probe(cg, token_address: str):
    tokens = await cg.tokens_multi(SOLANA, [token_address])
    return {"count": len(tokens)}, [t.model_dump(mode="json") for t in tokens]


async def _token_info_probe(cg, token_address: str):
    info = await cg.token_info(SOLANA, token_address)
    return {"gt_score": str(info.gt_score)}, info.model_dump(mode="json")


async def _top_holders_probe(cg, token_address: str):
    holders = await cg.top_holders(SOLANA, token_address, holders=40)
    return {"count": len(holders.holders)}, holders.model_dump(mode="json")


async def _top_traders_probe(cg, token_address: str):
    traders = await cg.top_traders(SOLANA, token_address, traders=20)
    return {"count": len(traders.traders)}, traders.model_dump(mode="json")


async def _pool_trades_probe(cg, pool_address: str, token_address: str):
    trades = await cg.pool_trades(SOLANA, pool_address, token=token_address)
    return {"count": len(trades)}, [t.model_dump(mode="json") for t in trades]


async def _pool_ohlcv_probe(cg, pool_address: str, token_address: str):
    bars = await cg.pool_ohlcv(
        SOLANA,
        pool_address,
        timeframe="minute",
        aggregate=1,
        limit=100,
        token=token_address,
        include_empty_intervals=False,
    )
    return {"count": len(bars)}, [b.model_dump(mode="json") for b in bars]


async def _g2_probe(pool_address: str):
    """G2 逐条计费契约探测：短订阅 10 秒，统计消息数。

    契约要点（实测锁定）：data 内 JSON key 为字面量 "network_id:pool_addresses"，
    值为 "network:pool" 字符串；消息字段为短键（c/n/pa/tx/ty/to/toq/vo/pc/pu/t）。
    """
    import os

    import websockets

    uri = f"wss://stream.coingecko.com/v1?x_cg_pro_api_key={os.environ['COINGECKO_API_KEY']}"
    received = 0
    try:
        async with websockets.connect(uri, open_timeout=15) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)  # code 3000
            await asyncio.wait_for(ws.recv(), timeout=10)  # welcome
            await ws.send(
                json.dumps(
                    {"command": "subscribe", "identifier": '{"channel":"OnchainTrade"}'}
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=10)  # confirm_subscription
            await ws.send(
                json.dumps(
                    {
                        "command": "message",
                        "identifier": '{"channel":"OnchainTrade"}',
                        "data": json.dumps(
                            {
                                "network_id:pool_addresses": [
                                    f"{SOLANA}:{pool_address}"
                                ],
                                "action": "set_pools",
                            }
                        ),
                    }
                )
            )
            deadline = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < deadline:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                try:
                    data = json.loads(message)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("c") == "G2":
                    received += 1
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "messages": received}
    return {"ok": True, "messages_in_10s": received}


async def _g1_g3_probe(pool_address: str, token_address: str):
    """G1/G3 契约探测：使用生产 CoinGeckoWS client 实测 15 秒。"""
    import os

    from app.core.clients.coingecko_ws import CoinGeckoWS

    events = {"g1": 0, "g3": 0}

    async def on_g1(event):
        events["g1"] += 1

    async def on_g3(event):
        events["g3"] += 1

    client = CoinGeckoWS(
        os.environ["COINGECKO_API_KEY"], SOLANA, on_g1_event=on_g1, on_g3_event=on_g3
    )
    client.set_g1_tokens({token_address})
    client.set_g3_pools({(pool_address, "base")})
    client.start()
    await asyncio.sleep(15)
    await client.stop()
    return (
        {"g1_events": events["g1"], "g3_events": events["g3"], "state": client.state},
        None,
    )


async def _goplus_solana_probe(gp, token_address: str | None):
    address = token_address or "So11111111111111111111111111111111111111112"
    results = await gp.solana_token_security([address])
    return {"count": len(results)}, {
        a: s.model_dump(mode="json") for a, s in results.items()
    }


async def _goplus_bsc_probe(gp):
    address = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"  # WBNB
    results = await gp.evm_token_security(CHAIN_ID_BSC, [address])
    return {"count": len(results)}, {
        a: s.model_dump(mode="json") for a, s in results.items()
    }


def main() -> None:
    report, fixtures = asyncio.run(probe())
    payload = {
        "script": "tools/probe_contracts.py",
        "script_version": SCRIPT_VERSION,
        "python_version": platform.python_version(),
        "executed_at_utc": _now_utc(),
        "report": report,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"probe-{_now_utc().replace(':', '').replace('-', '')}.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in fixtures.items():
        (FIXTURES_DIR / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    print(f"报告: {report_path}")
    print(f"fixture: {len(fixtures)} 个 → {FIXTURES_DIR}")
    for name, entry in report["entries"].items():
        print(f"  {name}: {entry}")


if __name__ == "__main__":
    main()
