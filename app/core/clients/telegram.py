"""Telegram client：Long Polling、outbox 投递与一次性确认（总控文档第 8 章）。

- update_id 持久化去重：业务事务提交后才推进 offset（第 8.3 节）。
- outbox：delivery_key 幂等；PENDING 重启补发；SENDING 恢复转 DELIVERY_UNKNOWN；
  过期买入信号补发标记"已过期，仅通知"。
- 确认 nonce：128-bit 随机、60 秒过期、绑定参数、一次性消费。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
from typing import Any, Awaitable, Callable

from app.core.models import utc_now_ms

log = logging.getLogger(__name__)

CONFIRMATION_TTL_SECONDS = 60


class UpdatePoller:
    """Long Polling：get_updates 轮询 + 持久化去重 + 事务后推进 offset。"""

    def __init__(
        self,
        repo,
        fetch_fn: Callable[[int, int], Awaitable[list[dict]]],
        *,
        poll_interval: float = 0.5,
        timeout: int = 20,
    ) -> None:
        self._repo = repo
        self._fetch = fetch_fn  # async (offset, timeout) -> [update dict]
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.on_update: Callable[[dict], Awaitable[None]] | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        offset = await self._repo.get_committed_offset()
        self._task = asyncio.create_task(self._loop(offset), name="telegram-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, offset: int) -> None:
        while True:
            try:
                updates = await self._fetch(offset, self.timeout)
                for update in updates:
                    update_id = int(update["update_id"])
                    new = await self._repo.insert_update(update_id, {"handled": "pending"})
                    if not new:
                        continue  # 重复 update：只返回已保存结果，不重复执行
                    result: dict[str, Any] = {"handled": "ok"}
                    try:
                        if self.on_update is not None:
                            await self.on_update(update)
                    except Exception as exc:
                        result = {"handled": "error", "error": str(exc)}
                        log.warning(
                            "update 处理异常 %s: %s | update=%s",
                            update_id,
                            exc,
                            str(update)[:300],
                        )
                    finally:
                        await self._repo.update_update_result(update_id, result)
                        # 业务事务提交后推进 offset
                        await self._repo.set_committed_offset(update_id + 1)
                        offset = update_id + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram 轮询异常: %s", exc)
            await asyncio.sleep(self.poll_interval)


class OutboxSender:
    """outbox 投递状态机（第 8.3 节）。"""

    def __init__(
        self,
        repo,
        send_fn,
        *,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        self._repo = repo
        # async (chat_id, text, reply_markup: dict | None) -> message_id
        self._send = send_fn
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        # 恢复：遗留 SENDING 无法证明已发送 → DELIVERY_UNKNOWN，不得自动补发
        recovered = await self._repo.mark_sending_recovery()
        if recovered:
            log.warning("恢复：%d 条 SENDING 转 DELIVERY_UNKNOWN", recovered)
        self._task = asyncio.create_task(self._loop(), name="telegram-outbox")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._repo.requeue_failed_outbox(utc_now_ms())
                rows = await self._repo.list_outbox_pending(limit=50)
                now = utc_now_ms()
                for row in rows:
                    if row["next_attempt_at"] and int(row["next_attempt_at"]) > now:
                        continue
                    await self._deliver(row)
                if not rows:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox 投递循环异常")

    async def _deliver(self, row) -> None:
        delivery_key = row["delivery_key"]
        chat_id = int(row["chat_target"]) if row["chat_target"].lstrip("-").isdigit() else None
        text = row["text"]
        if chat_id is None:
            # 目标为频道名等非数字目标时由管理员会话投递（G8 简化：跳过并告警）
            await self._repo.update_outbox_status(delivery_key, "FAILED")
            log.warning("outbox 目标无法解析: %s", row["chat_target"])
            return
        if row["kind"] == "BUY_SIGNAL" and row["parent_id"]:
            signal = await self._repo.get_signal(row["parent_id"])
            if signal and int(signal["expires_at"]) < utc_now_ms():
                text = f"⚠️ [已过期，仅通知]\n{text}"
        markup = None
        if row["markup"]:
            try:
                import json

                markup = json.loads(row["markup"])
            except Exception:
                log.warning("outbox markup 解析失败 %s", delivery_key)
        await self._repo.update_outbox_status(delivery_key, "SENDING")
        try:
            message_id = await self._send(chat_id, text, markup)
            await self._repo.update_outbox_status(
                delivery_key, "SENT", telegram_message_id=message_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry_count = int(row["retry_count"]) + 1
            backoff = min(
                self.retry_base_seconds * (2 ** (retry_count - 1)),
                self.retry_max_seconds,
            )
            log.warning("outbox 投递失败 %s: %s", delivery_key, exc)
            await self._repo.update_outbox_status(
                delivery_key,
                "FAILED",
                retry_count=retry_count,
                next_attempt_at=utc_now_ms() + int(backoff * 1000),
            )


class ConfirmationManager:
    """一次性确认 nonce（第 8.3 节）。"""

    def __init__(self, repo) -> None:
        self._repo = repo

    def new_nonce(self) -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")

    def command_hash(self, command: str, args: str) -> str:
        return hashlib.sha256(f"{command}|{args}".encode()).hexdigest()[:16]

    async def create(
        self,
        *,
        admin_id: int,
        chat_id: int,
        update_id: int,
        command: str,
        args: str,
        payload: dict,
        ttl_seconds: int = CONFIRMATION_TTL_SECONDS,
    ) -> str:
        nonce = self.new_nonce()
        await self._repo.insert_confirmation(
            nonce=nonce,
            admin_id=admin_id,
            chat_id=chat_id,
            update_id=update_id,
            command_hash=self.command_hash(command, args),
            payload=payload,
            expires_at=utc_now_ms() + ttl_seconds * 1000,
        )
        return nonce

    async def try_consume(
        self, nonce: str, *, admin_id: int, chat_id: int
    ) -> dict | None:
        """校验绑定并一次性消费；成功返回 payload，失败返回 None。"""
        row = await self._repo.get_confirmation(nonce)
        if row is None:
            return None
        if row["status"] != "PENDING":
            return None
        if int(row["admin_id"]) != admin_id or int(row["chat_id"]) != chat_id:
            return None
        if int(row["expires_at"]) < utc_now_ms():
            await self._repo.consume_confirmation(nonce, "EXPIRED")
            return None
        if not await self._repo.consume_confirmation(nonce, "CONFIRMED"):
            return None
        import json

        return json.loads(row["payload"] or "{}")
