#!/usr/bin/env python3
"""GoPlus 有鉴权限速探测（服务器出口 IP 实测）。

梯度测试 30 → 60 → 90 → 120 次/分钟，每档 2 分钟，统计 4029 计数。
首个出现 4029 的档位停止；推荐限速 = 上一个干净档位 × 80%。
用法（服务器容器内）: python tools/probe_goplus_rate.py
"""
import asyncio
import hashlib
import json
import os
import sys
import time

import httpx

BASE = "https://api.gopluslabs.io"
SOL_TOKEN = "So11111111111111111111111111111111111111112"
BSC_TOKEN = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
STEPS = (30, 60, 90, 120)
STEP_SECONDS = 120


async def get_access_token(client: httpx.AsyncClient) -> str:
    key = os.environ.get("GOPLUS_APP_KEY", "")
    secret = os.environ.get("GOPLUS_APP_SECRET", "")
    if not key or not secret:
        print("缺少 GOPLUS_APP_KEY/GOPLUS_APP_SECRET，按无鉴权探测")
        return ""
    ts = int(time.time())
    sign = hashlib.sha1(f"{key}{ts}{secret}".encode()).hexdigest()
    resp = await client.post(
        "/api/v1/token", json={"app_key": key, "sign": sign, "time": ts}
    )
    body = resp.json()
    if body.get("code") != 1:
        print("access token 获取失败:", body)
        return ""
    token = body["result"]["access_token"]
    print("access token 获取成功，expires_in:", body["result"].get("expires_in"))
    return token


async def probe_step(client: httpx.AsyncClient, token: str, rate: int, endpoint: str, params: dict) -> tuple[int, int]:
    """按 rate/min 的均匀间隔发请求，持续 STEP_SECONDS 秒；返回 (总请求, 4029数)。"""
    interval = 60.0 / rate
    sent = 0
    limited = 0
    deadline = time.monotonic() + STEP_SECONDS
    while time.monotonic() < deadline:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = await client.get(endpoint, params=params, headers=headers, timeout=15)
            body = resp.json()
            if body.get("code") == 4029:
                limited += 1
        except Exception as exc:
            print("  请求异常:", exc)
            limited += 1
        sent += 1
        await asyncio.sleep(interval)
    return sent, limited


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE) as client:
        token = await get_access_token(client)
        if token:
            print("== 探测：Solana 端点（有鉴权）==")
            solana_params = {"contract_addresses": SOL_TOKEN}
        else:
            print("== 探测：Solana 端点（无鉴权）==")
            solana_params = {"contract_addresses": SOL_TOKEN}

        last_clean = None
        for rate in STEPS:
            sent, limited = await probe_step(
                client, token, rate, "/api/v1/solana/token_security", solana_params
            )
            print(f"  {rate}/min: 发送 {sent}，4029 {limited}")
            if limited > 0:
                print(f"  → {rate}/min 触发限流，停止")
                break
            last_clean = rate

        if last_clean is None:
            print("结论: 30/min 也触发限流，保持 30/min 或更低")
            return
        recommended = int(last_clean * 0.8)
        print(f"结论: 干净上限 {last_clean}/min，推荐客户端限速 {recommended}/min")
        print(f"配置: GOPLUS_RATE_PER_MINUTE={recommended}")

        # EVM 端点快速验证（推荐速率下 30 次请求）
        if token:
            print("== 验证：BSC 端点（推荐速率下 30 次请求）==")
            evm_params = {"contract_addresses": BSC_TOKEN}
            limited = 0
            for _ in range(30):
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    resp = await client.get(
                        "/api/v1/token_security/56", params=evm_params,
                        headers=headers, timeout=15,
                    )
                    if resp.json().get("code") == 4029:
                        limited += 1
                except Exception:
                    limited += 1
                await asyncio.sleep(60.0 / recommended)
            print(f"  BSC 4029: {limited}/30")


if __name__ == "__main__":
    asyncio.run(main())
