"""管理员命令与信号消息（总控文档第 8.1、8.2 节）。

核心命令集框架；/budget 与 /strategy * 由 G9/G10 通过 register 接入。
所有机器人回复使用中文字段名与状态标签（LABELS 映射），不直接展示内部字段。
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CANDIDATE_STATUS_ZH = {
    "WATCHING": "观察中",
    "SETUP": "预备",
    "SIGNAL": "已发信号",
    "REJECTED": "已拒绝",
    "EXPIRED": "已过期",
}

TELEGRAM_STATUS_ZH = {
    "PENDING": "待发送",
    "SENDING": "发送中",
    "SENT": "已送达",
    "FAILED": "失败",
    "DELIVERY_UNKNOWN": "投递未知",
}

SIGNAL_LEVEL_ZH = {"BUY": "买入", "SELL": "卖出"}

WS_STATE_ZH = {
    "IDLE": "空闲",
    "CONNECTED": "已连接",
    "RECONNECTING": "重连中",
    "FATAL": "故障",
}

PIPELINE_STAGE_ZH = {
    "seen": "进入管线",
    "trusted_quote_fail": "报价未命中",
    "security_fail": "安全拒绝",
    "market_quality_fail": "市场质量不足",
    "g3_incomplete": "K线不完整",
    "score_below_setup": "评分不足",
    "anti_chase_block": "防追高拦截",
    "setup_waiting": "预备确认中",
    "pre_exec_fail": "复检失败",
    "signaled": "已发信号",
}

TEMPLATE_ID_ZH = {
    "sol_m5_trending": "Solana热门榜(30s)",
    "sol_m5_pcp": "Solana涨跌榜(120s)",
    "sol_trending": "Solana趋势池(120s)",
    "sol_new": "Solana新池(180s)",
    "bsc_m5_trending": "BSC热门榜(30s)",
    "bsc_m5_pcp": "BSC涨跌榜(120s)",
    "bsc_trending": "BSC趋势池(120s)",
    "bsc_new": "BSC新池(180s)",
}

REST_INTERFACE_ZH = {
    "pools-megafilter": "池筛选",
    "trending-pools-network": "趋势池",
    "latest-pools-network": "新池",
    "pools-addresses": "池详情",
    "tokens-data-contract-addresses": "代币批量",
    "token-info-contract-address": "代币信息",
    "top-token-holders-token-address": "持仓分布",
    "top-token-traders-token-address": "盈利交易者",
    "pool-trades-contract-address": "逐笔成交",
    "pool-ohlcv-contract-address": "历史K线",
    "top-pools-contract-address": "顶部池",
    "api-usage": "额度查询",
}


def zh_candidate_status(status: str) -> str:
    return CANDIDATE_STATUS_ZH.get(status, status)


def zh_telegram_status(status: str) -> str:
    return TELEGRAM_STATUS_ZH.get(status, status)


def build_signal_message(row, decision: dict | None = None) -> str:
    """买入信号消息（文档第 8.1 节内容清单；中文标签展示）。"""
    security = json.loads(row["security_snapshot"] or "{}")
    decision = decision or json.loads(row["decision_snapshot"] or "{}")
    scoring = decision.get("scoring") or {}
    lines = [
        f"🔔 买入信号 [{row['chain'].upper()}]",
        f"等级: {SIGNAL_LEVEL_ZH.get(row['signal_level'], row['signal_level'])}｜"
        f"总分: {row['total_score']}",
        f"代币: {row['token_address']}",
        f"决定池: {row['pool_address']}",
        f"参考价: {row['reference_price']}",
        f"策略: {row['strategy_revision']}（{row['strategy_hash'][:8]}）",
        f"安全复检: {'通过' if security.get('pass') else '未通过'}｜"
        f"可交易: {'是' if security.get('trade_allowed') else '否'}",
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
            return f"信号引擎运行中｜暂停新信号: {'是' if self.paused else '否'}\n{extra}".strip()
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
                    f"[{row['chain']}] 代币 {row['token_address'][:12]}"
                    f"｜状态: {zh_candidate_status(row['status'])}"
                    f"｜池: {row['pool_address'][:10]}"
                )
            return "\n".join(lines)
        if command == "/signals":
            signals = await self._repo.list_signals(limit=10)
            if not signals:
                return "暂无信号"
            lines = []
            for row in signals:
                lines.append(
                    f"[{row['chain']}] {row['token_address'][:12]}"
                    f"｜信号: {SIGNAL_LEVEL_ZH.get(row['signal_level'], row['signal_level'])}"
                    f"｜评分: {row['total_score']}"
                    f"｜投递: {zh_telegram_status(row['telegram_status'])}"
                )
            return "\n".join(lines)
        return f"未知命令: {command}"

    async def _chain_summary(self, chain: str) -> str:
        candidates = await self._repo.list_candidates(chain)
        by_status: dict[str, int] = {}
        for row in candidates:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        status_text = " ".join(
            f"{zh_candidate_status(k)}:{v}" for k, v in sorted(by_status.items())
        )
        return f"{chain} 候选: {len(candidates)} 个｜{status_text}"
