"""插件总线：通知 API 与命令路由（核心文档第 15.3、15.4 节）。

- 通知进入核心 telegram_outbox，投递幂等由核心负责；插件不得持有 bot token。
- 命令：冲突拒绝、管理员校验与确认框架由核心执行（G8 接线）。
"""
from __future__ import annotations

import logging
import uuid

from app.core.models import utc_now_ms

log = logging.getLogger(__name__)

class NotifyAPI:
    """统一通知接口：消息进入核心 outbox，返回 delivery_key。

    目标名（admin/channel）经 resolver 解析为数字 chat_id，投递由
    OutboxSender 完成（未解析目标直接拒绝，避免 outbox 堆积 FAILED）。
    """

    def __init__(self, repo, target_resolver: dict[str, int] | None = None) -> None:
        self._repo = repo
        self._resolver = target_resolver or {}

    async def send(
        self, text: str, *, target: str = "admin", priority: int = 0, markup: str | None = None
    ) -> str:
        chat_id = self._resolver.get(target)
        if chat_id is None:
            raise ValueError(f"未知通知目标: {target}")
        delivery_key = f"n:{uuid.uuid4().hex}"
        now = utc_now_ms()
        await self._repo.insert_outbox(
            delivery_key=delivery_key,
            kind="NOTIFY",
            parent_id=None,
            chat_target=str(chat_id),
            text=text,
            priority=priority,
            markup=markup,
            now_ms=now,
        )
        return delivery_key

    async def status(self, delivery_key: str) -> str | None:
        row = await self._repo.get_outbox(delivery_key)
        return row["status"] if row else None
