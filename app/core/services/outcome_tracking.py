"""信号后结果标签追踪（总控文档第 4.4.2 节）。

- 信号发出后 15m/1h/6h/24h 定点用 Multiple Pools 查询决定池；
- 不占 G1/G3 订阅；遵循 REST 并发上限；失败补洞一次，连续失败记数据缺口。
- spike 已锁定：simple token_price 不含流动性，结果追踪固定使用 Multiple Pools。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from app.core.clients.coingecko import CoinGeckoClient
from app.core.models import utc_now_ms
from app.core.storage.repository import Repository

log = logging.getLogger(__name__)

HORIZONS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
}


class OutcomeTracker:
    def __init__(
        self,
        repo: Repository,
        cg: CoinGeckoClient,
        rest_gate,
        *,
        poll_interval_seconds: int = 30,
    ) -> None:
        self._repo = repo
        self._cg = cg
        self._rest_gate = rest_gate
        self.poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="outcome-tracker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_due(self, now_ms: int) -> int:
        """采集全部到期结果标签；返回本次处理数。"""
        signals = await self._repo.list_due_outcomes(now_ms, limit=500)
        done = 0
        for row in signals:
            created = int(row["created_at"])
            for horizon, seconds in HORIZONS.items():
                if created + seconds * 1000 > now_ms:
                    continue
                if await self._repo.get_outcome_snapshot(row["signal_id"], horizon):
                    continue
                await self._capture(row, horizon)
                done += 1
        return done

    async def _capture(self, row, horizon: str) -> None:
        for attempt in range(2):  # 失败补洞一次
            try:
                async with self._rest_gate.acquire(critical=False):
                    details = await self._cg.pools_multi(
                        row["chain"], [row["pool_address"]], include_volume_breakdown=False
                    )
                if not details:
                    raise RuntimeError("决定池无返回")
                detail = details[0]
                price = str(detail.base_token_price_usd) if detail.base_token_price_usd is not None else None
                await self._repo.insert_outcome_snapshot(
                    signal_id=row["signal_id"],
                    chain=row["chain"],
                    token_address=row["token_address"],
                    pool_address=row["pool_address"],
                    horizon=horizon,
                    price=price,
                    price_high=None,
                    price_low=None,
                    liquidity_usd=(
                        str(detail.reserve_in_usd) if detail.reserve_in_usd is not None else None
                    ),
                    pool_removed=False,
                    completeness="COMPLETE",
                )
                return
            except Exception as exc:
                if attempt == 0:
                    log.warning(
                        "结果标签补洞重试 %s %s: %s", row["signal_id"], horizon, exc
                    )
                    continue
                await self._repo.insert_outcome_snapshot(
                    signal_id=row["signal_id"],
                    chain=row["chain"],
                    token_address=row["token_address"],
                    pool_address=row["pool_address"],
                    horizon=horizon,
                    price=None,
                    price_high=None,
                    price_low=None,
                    liquidity_usd=None,
                    pool_removed=False,
                    completeness="DATA_GAP",
                )
                log.warning("结果标签数据缺口 %s %s: %s", row["signal_id"], horizon, exc)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due(utc_now_ms())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("结果追踪循环异常")
            await asyncio.sleep(self.poll_interval_seconds)
