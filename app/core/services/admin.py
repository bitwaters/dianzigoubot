"""管理员命令与信号消息（总控文档第 8.1、8.2 节）。

核心命令集框架；/budget 与 /strategy * 由 G9/G10 通过 register 接入。
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


def build_signal_message(row, decision: dict | None = None) -> str:
    """买入信号消息（文档第 8.1 节内容清单）。"""
    security = json.loads(row["security_snapshot"] or "{}")
    decision = decision or json.loads(row["decision_snapshot"] or "{}")
    scoring = decision.get("scoring") or {}
    lines = [
        f"🔔 买入信号 [{row['chain'].upper()}]",
        f"等级: {row['signal_level']}｜总分: {row['total_score']}",
        f"代币: {row['token_address']}",
        f"决定池: {row['pool_address']}",
        f"参考价: {row['reference_price']}",
        f"策略: {row['strategy_revision']} ({row['strategy_hash'][:8]})",
        f"安全复检: {'PASS' if security.get('pass') else 'FAIL'}｜可交易: {security.get('trade_allowed')}",
    ]
    return "\n".join(lines)


class AdminService:
    """核心命令处理：/status /sol /bsc /watchlist /signals /pause /resume。"""

    def __init__(self, repo) -> None:
        self._repo = repo
        self.paused = False
        self._extra: dict[str, Callable[[str], Awaitable[str]]] = {}
        self._status_provider: Callable[[], Awaitable[str]] | None = None

    def register(self, prefix: str, handler: Callable[[str], Awaitable[str]]) -> None:
        self._extra[prefix] = handler

    def set_status_provider(self, provider: Callable[[], Awaitable[str]]) -> None:
        self._status_provider = provider

    async def handle(self, command: str, args: str) -> str:
        for prefix, handler in self._extra.items():
            if command == prefix or command.startswith(prefix + " "):
                return await handler(args)
        if command == "/status":
            extra = ""
            if self._status_provider is not None:
                extra = await self._status_provider()
            return f"信号引擎运行中｜暂停新信号: {self.paused}\n{extra}".strip()
        if command == "/pause":
            self.paused = True
            return "已暂停新信号生成"
        if command == "/resume":
            self.paused = False
            return "已恢复新信号生成"
        if command == "/sol":
            return await self._chain_summary("solana")
        if command == "/bsc":
            return await self._chain_summary("bsc")
        if command == "/watchlist":
            candidates = await self._repo.list_candidates_limit(20)
            if not candidates:
                return "候选列表为空"
            lines = []
            for row in candidates:
                lines.append(
                    f"{row['chain']} {row['token_address'][:12]} "
                    f"{row['status']} 池:{row['pool_address'][:10]}"
                )
            return "\n".join(lines)
        if command == "/signals":
            signals = await self._repo.list_signals(limit=10)
            if not signals:
                return "暂无信号"
            lines = []
            for row in signals:
                lines.append(
                    f"{row['chain']} {row['token_address'][:12]} "
                    f"{row['signal_level']} {row['total_score']} "
                    f"投递:{row['telegram_status']}"
                )
            return "\n".join(lines)
        return f"未知命令: {command}"

    async def _chain_summary(self, chain: str) -> str:
        candidates = await self._repo.list_candidates(chain)
        by_status: dict[str, int] = {}
        for row in candidates:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        return (
            f"{chain} 候选: {len(candidates)} 个｜"
            + " ".join(f"{k}:{v}" for k, v in sorted(by_status.items()))
        )
