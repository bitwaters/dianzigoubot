"""维护服务：容量熔断与数据保留清理（总控文档第 9.4、9.5、11.1 节）。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.core.models import utc_now_ms

log = logging.getLogger(__name__)

CAPACITY_WARN_PCT = 70
CAPACITY_STOP_BACKFILL_PCT = 80
CAPACITY_STOP_SIGNALS_PCT = 90

RETENTION = {
    "market_bars": 72 * 3600,          # 普通运行期 1m 行情 72 小时
    "security_checks": 30 * 24 * 3600,  # 安全快照 30 天
    "signals": 180 * 24 * 3600,         # 信号 180 天
    "telegram_updates": 90 * 24 * 3600,  # update 明细 90 天
    "api_usage": 30 * 24 * 3600,         # 明细 30 天（汇总保留 1 年，v1 直接汇总）
    "discovery_snapshots": 30 * 24 * 3600,
    "candidates": 30 * 24 * 3600,
}

TIME_COLUMN = {
    "market_bars": "candle_timestamp",
    "telegram_updates": "processed_at",
}

OUTBOX_TERMINAL_RETENTION = 90 * 24 * 3600  # 普通通知确定终态 90 天


class CapacityManager:
    """数据卷水位：70% 告警、80% 停止回补、90% 停止新信号。"""

    def __init__(self, data_dir: Path | str, quota_gb: float | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.quota_gb = quota_gb  # 挂载卷配额；None 时使用磁盘剩余空间推算

    def usage_pct(self) -> float:
        if not self.data_dir.exists():
            return 0.0
        if self.quota_gb is not None:
            total = self.quota_gb * 1024**3
            used = sum(
                f.stat().st_size for f in self.data_dir.rglob("*") if f.is_file()
            )
            return used / total * 100 if total else 0.0
        usage = shutil.disk_usage(self.data_dir)
        return usage.used / usage.total * 100

    def state(self) -> str:
        pct = self.usage_pct()
        if pct >= CAPACITY_STOP_SIGNALS_PCT:
            return "STOP_SIGNALS"
        if pct >= CAPACITY_STOP_BACKFILL_PCT:
            return "STOP_BACKFILL"
        if pct >= CAPACITY_WARN_PCT:
            return "WARN"
        return "OK"


class RetentionCleaner:
    """按第 9.4 节周期清理；保护最新 offset、未终态 outbox 与未过期确认。"""

    def __init__(self, repo) -> None:
        self._repo = repo

    async def run(self, now_ms: int) -> dict[str, int]:
        cleaned: dict[str, int] = {}
        for table, seconds in RETENTION.items():
            cutoff = now_ms - seconds * 1000
            column = TIME_COLUMN.get(table, "created_at")
            try:
                cursor = await self._repo.conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)
                )
                cleaned[table] = cursor.rowcount
            except Exception as exc:
                log.warning("清理 %s 失败: %s", table, exc)
        await self._repo.conn.commit()
        return cleaned

    async def cleanup_outbox_terminal(self, now_ms: int) -> int:
        """outbox 确定终态清理：SENT/FAILED 超过 90 天，且不触碰 PENDING/SENDING/DELIVERY_UNKNOWN。"""
        cutoff = now_ms - OUTBOX_TERMINAL_RETENTION * 1000
        cursor = await self._repo.conn.execute(
            """DELETE FROM telegram_outbox
               WHERE status IN ('SENT', 'FAILED') AND updated_at < ?""",
            (cutoff,),
        )
        await self._repo.conn.commit()
        return cursor.rowcount
