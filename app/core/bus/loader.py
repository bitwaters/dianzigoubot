"""插件加载器与 PluginContext（SDK 规范第 3 章）。

- 目录约定 plugins/<name>/register.py + plugin.yaml；按启用列表顺序加载。
- 加载失败只跳过该插件；无热加载；同步 handler 注册被拒绝。
- 插件存储：命名空间表约定（<plugin_name>_ 前缀）与独立迁移执行。
"""
from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PLUGINS_DIR = Path("plugins")
SDK_VERSION = "1"


@dataclass
class PluginMeta:
    name: str
    version: str
    sdk_version: str
    supported_event_schema_versions: list[str] = field(default_factory=list)


@dataclass
class PluginStorage:
    """插件命名空间存储与独立迁移（SDK 规范第 6 章）。"""

    plugin_name: str
    repo: Any
    migrations_dir: Path

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.repo.conn.execute(sql, params)

    async def commit(self) -> None:
        await self.repo.conn.commit()

    async def apply_migrations(self) -> None:
        if not self.migrations_dir.exists():
            return
        await self.repo.conn.execute(
            """CREATE TABLE IF NOT EXISTS plugin_migrations (
                   plugin TEXT NOT NULL,
                   version INTEGER NOT NULL,
                   name TEXT NOT NULL,
                   applied_at INTEGER NOT NULL,
                   PRIMARY KEY (plugin, version)
               )"""
        )
        cursor = await self.repo.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM plugin_migrations WHERE plugin = ?",
            (self.plugin_name,),
        )
        row = await cursor.fetchone()
        applied_max = int(row[0])
        for file in sorted(self.migrations_dir.glob("*.sql")):
            version = int(file.name.split("_", 1)[0])
            if version <= applied_max:
                continue
            sql = file.read_text(encoding="utf-8")
            await self.repo.conn.execute("BEGIN")
            try:
                await self.repo.conn.executescript(sql)
                await self.repo.conn.execute(
                    "INSERT INTO plugin_migrations (plugin, version, name, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (self.plugin_name, version, file.name, _now_ms()),
                )
                await self.repo.conn.commit()
            except Exception:
                await self.repo.conn.rollback()
                raise
            log.info("插件 %s 迁移已应用: %s", self.plugin_name, file.name)


@dataclass
class PluginLog:
    plugin_name: str

    def _log(self, level: int, msg: str, *args) -> None:
        log.log(level, f"[{self.plugin_name}] {msg}", *args)

    def info(self, msg: str, *args) -> None:
        self._log(logging.INFO, msg, *args)

    def warning(self, msg: str, *args) -> None:
        self._log(logging.WARNING, msg, *args)

    def error(self, msg: str, *args) -> None:
        self._log(logging.ERROR, msg, *args)


@dataclass
class PluginContext:
    """插件可见的核心接口（SDK 规范第 3.2 节）。"""

    name: str
    events: Any
    notify: Any
    commands: Any
    storage: PluginStorage
    log: PluginLog
    config: dict = field(default_factory=dict)


def _load_meta(plugin_dir: Path) -> PluginMeta:
    meta_path = plugin_dir / "plugin.yaml"
    if not meta_path.exists():
        raise ValueError(f"缺少 plugin.yaml: {plugin_dir}")
    raw = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    meta = PluginMeta(
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        sdk_version=str(raw.get("sdk_version", "")),
        supported_event_schema_versions=[
            str(v) for v in raw.get("supported_event_schema_versions", [])
        ],
    )
    if not meta.name:
        raise ValueError(f"plugin.yaml 缺少 name: {plugin_dir}")
    if meta.sdk_version != SDK_VERSION:
        raise ValueError(
            f"插件 {meta.name} SDK 版本 {meta.sdk_version!r} 不受支持（当前 {SDK_VERSION}）"
        )
    if "1" not in meta.supported_event_schema_versions:
        raise ValueError(f"插件 {meta.name} 未声明支持事件 Schema v1")
    if meta.name != plugin_dir.name:
        raise ValueError(
            f"plugin.yaml name={meta.name!r} 与目录 {plugin_dir.name!r} 不一致"
        )
    return meta


def _load_register(plugin_dir: Path, name: str):
    register_path = plugin_dir / "register.py"
    if not register_path.exists():
        raise ValueError(f"缺少 register.py: {plugin_dir}")
    spec = importlib.util.spec_from_file_location(
        f"plugins.{name}.register", register_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    if not hasattr(module, "register"):
        raise ValueError(f"{register_path} 未定义 register(app)")
    return module.register


async def load_plugins(
    *,
    enabled: list[str],
    events: Any,
    notify: Any,
    commands: Any,
    repo: Any,
    base_dir: Path = PLUGINS_DIR,
) -> list[PluginContext]:
    """按启用列表顺序加载插件；单个插件失败只跳过并告警。"""
    loaded: list[PluginContext] = []
    for name in enabled:
        plugin_dir = base_dir / name
        try:
            meta = _load_meta(plugin_dir)
            register_fn = _load_register(plugin_dir, name)
            events.register_plugin(name)
            config = {}
            config_path = plugin_dir / "config.yaml"
            if config_path.exists():
                config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            context = PluginContext(
                name=name,
                events=events,
                notify=notify,
                commands=commands,
                storage=PluginStorage(
                    plugin_name=name,
                    repo=repo,
                    migrations_dir=plugin_dir / "migrations",
                ),
                log=PluginLog(plugin_name=name),
                config=config,
            )
            await register_fn(context)
            await context.storage.apply_migrations()
            loaded.append(context)
            log.info("插件已加载: %s v%s", name, meta.version)
        except Exception as exc:
            events.unregister_plugin(name)
            log.error("插件 %s 加载失败，已跳过: %s", name, exc)
    return loaded


def _now_ms() -> int:
    from app.core.models import utc_now_ms

    return utc_now_ms()
