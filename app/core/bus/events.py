"""插件总线：事件发布与订阅（核心文档第 15 章、SDK 规范第 2 章）。

- 事件 Schema v1；事件与信号落库同事务（signal 表驱动，至少一次投递）。
- 重启后对未发布过的信号补发事件；插件以 event_id 幂等。
- 分发超时（默认 5 秒）：超时或异常计一次熔断计数，不阻塞其他插件。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.core.models import utc_now_ms

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

EventType = str
EventHandler = Callable[[dict], Awaitable[None]]


@dataclass
class PluginState:
    name: str
    handlers: dict[EventType, list[EventHandler]] = field(default_factory=dict)
    supported_schema_versions: set[str] = field(default_factory=lambda: {"1"})
    consecutive_failures: int = 0
    tripped: bool = False


def build_signal_created_event(row) -> dict:
    """按核心文档第 15.2 节 Schema v1 构造 signal_created 载荷。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"sc:{row['signal_id']}",
        "signal_id": row["signal_id"],
        "chain": row["chain"],
        "token_address": row["token_address"],
        "pool_address": row["pool_address"],
        "signal_level": row["signal_level"],
        "total_score": row["total_score"],
        "reference_price": row["reference_price"],
        "security_snapshot": row["security_snapshot"],
        "expires_at": str(row["expires_at"]),
        "strategy_revision": row["strategy_revision"],
        "strategy_hash": row["strategy_hash"],
        "telegram_status": row["telegram_status"],
        "created_at": str(row["created_at"]),
    }


def build_signal_invalidated_event(row) -> dict:
    """按核心文档第 15.2 节 Schema v1 构造 signal_invalidated 载荷。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "signal_id": row["signal_id"],
        "chain": row["chain"],
        "token_address": row["token_address"],
        "pool_address": row["pool_address"],
        "reason": row["invalidated_reason"] or "EXPIRED",
        "invalidated_at": str(row["invalidated_at"] or utc_now_ms()),
        "strategy_revision": row["strategy_revision"],
    }


class EventBus:
    def __init__(
        self,
        repo,
        *,
        dispatch_timeout_seconds: float = 5.0,
        failure_threshold: int = 10,
    ) -> None:
        self._repo = repo
        self.dispatch_timeout_seconds = dispatch_timeout_seconds
        self.failure_threshold = failure_threshold
        self._plugins: dict[str, PluginState] = {}
        self._queue: asyncio.Queue[tuple[EventType, dict]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self.on_notify: Callable[[dict], Awaitable[None]] | None = None

    # -- 插件注册 -----------------------------------------------------------

    def register_plugin(
        self, name: str, supported_schema_versions: list[str] | None = None
    ) -> PluginState:
        if name in self._plugins:
            raise ValueError(f"插件重复注册: {name}")
        state = PluginState(
            name=name,
            supported_schema_versions=set(supported_schema_versions or ["1"]),
        )
        self._plugins[name] = state
        return state

    def unregister_plugin(self, name: str) -> None:
        self._plugins.pop(name, None)

    def subscribe(self, plugin: str, event_type: EventType, handler: EventHandler) -> None:
        state = self._plugins.get(plugin)
        if state is None:
            raise ValueError(f"未知插件: {plugin}")
        if not asyncio.iscoroutinefunction(handler):
            raise ValueError("事件处理函数必须为 async 函数（禁止阻塞式注册）")
        state.handlers.setdefault(event_type, []).append(handler)

    # -- 发布 ---------------------------------------------------------------

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker(), name="event-bus")

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def publish_signal_created(self, signal_id: str) -> bool:
        """信号已落库后调用：发布事件并标记 event_published（同进程幂等）。"""
        row = await self._repo.get_signal(signal_id)
        if row is None:
            log.error("发布失败：信号不存在 %s", signal_id)
            return False
        event = build_signal_created_event(row)
        await self._queue.put(("signal_created", event))
        await self._repo.mark_signal_event_published(signal_id)
        return True

    async def publish_signal_invalidated(self, signal_id: str) -> bool:
        row = await self._repo.get_signal(signal_id)
        if row is None or not row["invalidated_at"]:
            return False
        event = build_signal_invalidated_event(row)
        await self._queue.put(("signal_invalidated", event))
        return True

    async def resend_unpublished(self) -> int:
        """重启恢复：对未发布过的信号补发事件（至少一次投递）。"""
        rows = await self._repo.list_unpublished_signals(limit=500)
        for row in rows:
            event = build_signal_created_event(row)
            await self._queue.put(("signal_created", event))
            await self._repo.mark_signal_event_published(row["signal_id"])
        return len(rows)

    # -- 分发 ---------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            event_type, event = await self._queue.get()
            for state in self._plugins.values():
                if state.tripped:
                    continue
                # 事件 Schema 版本校验：插件未声明支持的版本不得透传
                if event.get("schema_version") not in state.supported_schema_versions:
                    log.warning(
                        "插件 %s 不支持事件版本 %s，跳过 %s",
                        state.name,
                        event.get("schema_version"),
                        event_type,
                    )
                    continue
                for handler in state.handlers.get(event_type, []):
                    await self._dispatch(state, handler, event)
                    if state.tripped:
                        break

    async def _dispatch(self, state: PluginState, handler: EventHandler, event: dict) -> None:
        try:
            await asyncio.wait_for(
                handler(event), timeout=self.dispatch_timeout_seconds
            )
            state.consecutive_failures = 0
        except asyncio.TimeoutError:
            state.consecutive_failures += 1
            log.warning("插件 %s 处理超时（第 %d 次）", state.name, state.consecutive_failures)
            self._maybe_trip(state)
        except Exception:
            state.consecutive_failures += 1
            log.exception("插件 %s 处理异常（第 %d 次）", state.name, state.consecutive_failures)
            self._maybe_trip(state)

    def _maybe_trip(self, state: PluginState) -> None:
        if state.consecutive_failures >= self.failure_threshold and not state.tripped:
            state.tripped = True
            log.error("插件 %s 熔断：停止分发", state.name)
            if self.on_notify is not None:
                asyncio.create_task(
                    self.on_notify(
                        {"text": f"⚠️ 插件 {state.name} 连续失败已熔断", "priority": 0}
                    )
                )
