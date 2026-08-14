"""用量控制（总控文档第 4.6 节简化版）。

- REST 粗控：全局并发上限，关键事务优先。
- WS 每日预算由 clients/coingecko_ws.DailyCreditGate 承担。
- /budget 只显示 /key 实际字段与本地计数，不写死套餐额度。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)


class RestConcurrencyGate:
    """REST 全局并发上限；关键事务占用保留槽位。"""

    def __init__(self, total: int, reserved_for_critical: int = 1) -> None:
        assert total > reserved_for_critical >= 0
        self.total = total
        self.reserved = reserved_for_critical
        self._normal = asyncio.Semaphore(total - reserved_for_critical)
        self._critical = asyncio.Semaphore(reserved_for_critical)

    @asynccontextmanager
    async def acquire(self, critical: bool = False):
        sem = self._critical if critical else self._normal
        async with sem:
            yield

    @property
    def normal_slots(self) -> int:
        return self.total - self.reserved


class UsageService:
    """用量告警与 /budget 展示数据。"""

    def __init__(
        self,
        *,
        key_provider,
        rest_ledger,
        credit_gates: dict[str, Any] | None = None,
        alert_threshold: int | None = None,
    ) -> None:
        self._key_provider = key_provider  # async () -> KeyInfo | None
        self._rest_ledger = rest_ledger  # UsageLedger
        self._credit_gates = credit_gates or {}
        self.alert_threshold = alert_threshold

    async def budget_lines(self) -> list[str]:
        from app.core.services.admin import REST_INTERFACE_ZH

        info = None
        try:
            info = await self._key_provider()
        except Exception as exc:
            return [f"额度查询失败: {exc}"]
        lines = []
        if info is not None:
            lines.append(
                f"套餐: {info.plan}｜剩余额度: {info.current_remaining_monthly_calls:,}｜"
                f"每分钟限速: {info.rate_limit_request_per_minute}"
            )
            if (
                self.alert_threshold is not None
                and info.current_remaining_monthly_calls is not None
                and info.current_remaining_monthly_calls < self.alert_threshold
            ):
                lines.append(f"⚠️ 剩余额度低于告警阈值 {self.alert_threshold:,}")
        rest = self._rest_ledger.snapshot()
        if rest:
            parts = []
            for key, value in sorted(rest.items()):
                interface = key.replace("rest:", "")
                label = REST_INTERFACE_ZH.get(interface, interface)
                parts.append(f"{label} {value} 次")
            lines.append("REST 调用: " + "，".join(parts))
        for name, gate in self._credit_gates.items():
            lines.append(f"实时行情[{name}] 今日用量: {gate.used}/{gate.daily_budget}")
        return lines or ["无额度数据"]
