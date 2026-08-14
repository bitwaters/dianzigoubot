"""插件总线：命令路由（核心文档第 15.4 节）。

- 冲突拒绝（核心命令或先加载插件已注册）。
- 管理员校验、update 去重与确认框架由核心执行（G8 接线）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CORE_COMMANDS = {
    "/status",
    "/sol",
    "/bsc",
    "/watchlist",
    "/signals",
    "/budget",
    "/strategy",
    "/telegram",
    "/pause",
    "/resume",
}


class CommandConflictError(RuntimeError):
    pass


class CommandRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, dict] = {}

    def register(
        self,
        plugin: str,
        name: str,
        handler: Callable[..., Awaitable[None]],
        *,
        requires_confirmation: bool = False,
    ) -> None:
        if name in CORE_COMMANDS:
            raise CommandConflictError(f"命令与核心命令冲突: {name}")
        if name in self._handlers:
            raise CommandConflictError(
                f"命令已被 {self._handlers[name]['plugin']} 注册: {name}"
            )
        if not name.startswith("/"):
            raise ValueError("命令必须以 / 开头")
        self._handlers[name] = {
            "plugin": plugin,
            "handler": handler,
            "requires_confirmation": requires_confirmation,
        }

    def unregister_plugin(self, plugin: str) -> None:
        for name in [n for n, h in self._handlers.items() if h["plugin"] == plugin]:
            del self._handlers[name]

    def get(self, name: str) -> dict | None:
        return self._handlers.get(name)

    def list_plugin_commands(self, plugin: str) -> list[str]:
        return [n for n, h in self._handlers.items() if h["plugin"] == plugin]
