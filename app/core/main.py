"""信号引擎入口：装配与运行（总控文档第 3.2、11.1 节）。

启动恢复必须先于发现和新信号：加载未终态 outbox、结果追踪计划、
今日 WS 计数与订阅、插件注册；恢复完成后才启动发现与订阅。
"""
from __future__ import annotations

import asyncio
import logging
import os

from app.core.bus.events import EventBus
from app.core.bus.loader import load_plugins
from app.core.bus.notify import NotifyAPI
from app.core.bus.commands import CommandRegistry
from app.core.clients.coingecko import CoinGeckoClient
from app.core.clients.coingecko_ws import (
    CoinGeckoWS,
    DailyCreditGate,
    G3CandleBuffer,
)
from app.core.clients.goplus import GoPlusClient
from app.core.config import (
    Settings,
    load_plugins_config,
    load_strategy_file,
    setup_logging,
)
from app.core.services.admin import AdminService
from app.core.services.discovery import CandidatePipeline, DiscoveryTask, TemplateScheduler
from app.core.services.maintenance import CapacityManager, RetentionCleaner
from app.core.services.outcome_tracking import OutcomeTracker
from app.core.services.strategy import SignalPipeline, StrategyActivationService
from app.core.services.usage import RestConcurrencyGate, UsageService
from app.core.storage.database import Database
from app.core.storage.repository import Repository
from app.core.clients.telegram import UpdatePoller, OutboxSender

log = logging.getLogger(__name__)


async def _noop(*args, **kwargs):
    return None


async def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.secrets)

    db = Database(settings.db_path)
    await db.open()
    repo = Repository(db.conn)

    # 1. 恢复：策略 ACTIVE 快照
    activation = StrategyActivationService(repo)
    active = await activation.current_active()
    if active is None:
        strategy = load_strategy_file(settings.strategy_path)
        await activation.activate(strategy, admin_id=settings.telegram_admin_id)
        active = await activation.current_active()
        log.info("基准策略已激活: %s", active["revision"])
    strategy = load_strategy_file(settings.strategy_path)  # 运行以 DB ACTIVE 为准
    strategy_hash = active["strategy_hash"]
    strategy_revision = active["revision"]
    log.info("运行策略: %s (%s)", strategy_revision, strategy_hash[:8])

    # 2. 组件装配
    cg = CoinGeckoClient(settings.coingecko_api_key)
    gp = GoPlusClient()
    rest_gate = RestConcurrencyGate(
        strategy.solana.collection.rest_concurrency_max,
        reserved_for_critical=1,
    )
    ws_gates = {
        chain: DailyCreditGate(
            getattr(strategy, chain).collection.websocket.daily_credit_budget
        )
        for chain in ("solana", "bsc")
    }
    candle_buffers = {"solana": G3CandleBuffer(), "bsc": G3CandleBuffer()}
    ws_clients: dict[str, CoinGeckoWS] = {}

    def _g3_handler(chain):
        async def handler(event):
            candle_buffers[chain].upsert(event.pool_address, event.side, event.candle)
            candle_buffers[chain].sweep_final(_now_ms())

        return handler

    # G1 顶部池映射缓存（文档第 4.4 节：G1 事件触发映射解析）
    top_pool_cache: dict[str, dict] = {}
    top_pool_ttl_ms = 300_000

    def _g1_handler(chain):
        async def handler(event):
            token = event.token_address
            entry = top_pool_cache.get(token)
            now = _now_ms()
            if entry is not None and now - entry["at"] < top_pool_ttl_ms:
                return
            try:
                pools = await cg.top_pools_by_token(
                    chain, token, include="base_token,quote_token,dex"
                )
                top_pool_cache[token] = {"at": now, "pools": pools}
                log.info("G1 顶部池映射已刷新 %s %s (%d 池)", chain, token[:10], len(pools))
            except Exception as exc:
                log.warning("顶部池映射刷新失败 %s: %s", token[:10], exc)

        return handler

    for chain in ("solana", "bsc"):
        ws_clients[chain] = CoinGeckoWS(
            settings.coingecko_api_key,
            chain,
            credit_gate=ws_gates[chain],
            on_g1_event=_g1_handler(chain),
            on_g3_event=_g3_handler(chain),
        )

    bus = EventBus(repo, dispatch_timeout_seconds=settings.dispatch_timeout_seconds)
    notify = NotifyAPI(repo)
    commands = CommandRegistry()
    admin = AdminService(repo)

    usage = UsageService(
        key_provider=cg.key,
        rest_ledger=cg.usage,
        credit_gates=ws_gates,
        alert_threshold=int(os.environ.get("CREDIT_ALERT_THRESHOLD", "100000")),
    )
    admin.register("/budget", lambda args: _budget_lines(usage))

    outcome = OutcomeTracker(repo, cg, rest_gate)
    cleaner = RetentionCleaner(repo)
    capacity = CapacityManager(settings.db_path.parent)

    # 3. 恢复顺序：事件补发 → 今日 WS 计数 → 插件 → 订阅（发现最后启动）
    bus.start()
    await bus.resend_unpublished()

    day_start = _day_start_ms()
    ws_used_today = await repo.sum_ws_charged_since(day_start)
    for gate in ws_gates.values():
        gate.restore(ws_used_today)
    if ws_used_today:
        log.info("WS 今日计费恢复: %d", ws_used_today)

    plugins_config = load_plugins_config(settings.plugins_config_path)
    plugin_contexts = await load_plugins(
        enabled=plugins_config.enabled,
        events=bus,
        notify=notify,
        commands=commands,
        repo=repo,
    )
    log.info("插件加载完成: %d 个", len(plugin_contexts))

    pipelines: dict[str, SignalPipeline] = {}
    for chain in ("solana", "bsc"):
        chain_strategy = getattr(strategy, chain)
        scheduler = TemplateScheduler(chain, chain_strategy.collection, repo)
        discovery = DiscoveryTask(chain, chain_strategy, cg, scheduler)
        pipeline = SignalPipeline(
            chain=chain,
            strategy=chain_strategy,
            repo=repo,
            cg=cg,
            gp=gp,
            ws=ws_clients[chain],
            candle_buffer=candle_buffers[chain],
            pipeline=CandidatePipeline(chain, chain_strategy, cg, gp, repo),
            bus=bus,
            admin=admin,
            admin_chat_id=settings.telegram_admin_id,
            active_snapshot=(strategy_revision, strategy_hash),
        )
        discovery.on_pool = lambda pool, label, tpl, p=pipeline: _dispatch(p, pool, label)
        pipelines[chain] = pipeline
        ws_clients[chain].start()
        discovery_tasks[chain] = discovery

    # 4. 维护任务：过期作废 + api_usage 持久化 + 容量与清理
    async def maintenance_loop():
        while True:
            try:
                now = _now_ms()
                # 信号过期 → 作废并发布 signal_invalidated（文档第 7.2 节）
                for row in await repo.list_expired_signals(now):
                    await repo.invalidate_signal(row["signal_id"], "EXPIRED", invalidated_at=now)
                    await bus.publish_signal_invalidated(row["signal_id"])
                # 用量账本周期性落库（api_usage 表，文档第 9.3 节）
                for interface, count in cg.usage.snapshot().items():
                    await repo.record_api_usage(
                        "REST", interface.replace("rest:", ""), attempts=count
                    )
                ws_total = cg.usage.ws_total()
                if ws_total > 0:
                    await repo.record_api_usage("WS", "all", charged_responses=ws_total)
                cg.usage.clear()

                state = capacity.state()
                if state == "STOP_SIGNALS":
                    admin.paused = True
                    log.warning("容量 90%%：停止新信号")
                elif state == "STOP_BACKFILL":
                    log.warning("容量 80%%：停止历史回补")
                await cleaner.run(now)
                await cleaner.cleanup_outbox_terminal(now)
                await repo.conn.commit()
            except Exception:
                log.exception("维护循环异常")
            await asyncio.sleep(300)

    maintenance_task = asyncio.create_task(maintenance_loop())

    # 5. 发现循环
    async def discovery_loop():
        while True:
            for chain in ("solana", "bsc"):
                if admin.paused:
                    continue
                try:
                    await discovery_tasks[chain].run()
                except Exception:
                    log.exception("发现循环 %s 异常", chain)
            await asyncio.sleep(5)

    discovery_runner = asyncio.create_task(discovery_loop())
    await outcome.start()

    # 6. Telegram（需要真实 bot token；无 token 时跳过并告警）
    if settings.telegram_bot_token:
        try:
            from telegram import Bot

            bot = Bot(token=settings.telegram_bot_token)

            async def fetch_updates(offset, timeout):
                updates = await bot.get_updates(offset=offset, timeout=timeout)
                return [u.to_dict() for u in updates]

            async def send_msg(chat_id, text):
                message = await bot.send_message(chat_id=chat_id, text=text)
                return str(message.message_id)

            poller = UpdatePoller(repo, fetch_updates)
            poller.on_update = lambda update: _handle_update(
                update, admin, commands, notify, settings.telegram_admin_id
            )
            sender = OutboxSender(repo, send_msg)
            await poller.start()
            await sender.start()
            log.info("Telegram 已连接")
        except Exception as exc:
            log.error("Telegram 启动失败（信号推送停用）: %s", exc)
    else:
        log.warning("未配置 TELEGRAM_BOT_TOKEN，Telegram 停用")

    # 7. 等待终止信号（SIGINT/SIGTERM 优雅退出）
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        discovery_runner.cancel()
        maintenance_task.cancel()
        for ws in ws_clients.values():
            await ws.stop()
        await outcome.stop()
        await bus.stop()
        await cg.close()
        await gp.close()
        await db.close()


discovery_tasks: dict[str, DiscoveryTask] = {}


async def _dispatch(pipeline: SignalPipeline, pool, label) -> None:
    try:
        await pipeline.handle_pool(pool, label)
    except Exception:
        log.exception("候选处理异常")


async def _budget_lines(usage: UsageService) -> str:
    return "\n".join(await usage.budget_lines())


async def _handle_update(
    update: dict,
    admin: AdminService,
    commands: CommandRegistry,
    notify: NotifyAPI,
    admin_id: int,
) -> None:
    message = update.get("message") or {}
    from_user = message.get("from") or {}
    if int(from_user.get("id", 0)) != admin_id:
        return
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split(maxsplit=1)
    command = parts[0].split("@")[0]
    args = parts[1] if len(parts) > 1 else ""
    plugin_cmd = commands.get(command)
    if plugin_cmd is not None:
        try:
            await plugin_cmd["handler"](message)
            return
        except Exception:
            log.exception("插件命令处理异常: %s", command)
            return
    reply = await admin.handle(command, args)
    if reply:
        await notify.send(reply, target="admin")


def _now_ms() -> int:
    from app.core.models import utc_now_ms

    return utc_now_ms()


def _day_start_ms() -> int:
    """当日 UTC 零点（毫秒）。"""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    start = datetime.datetime(now.year, now.month, now.day, tzinfo=datetime.timezone.utc)
    return int(start.timestamp() * 1000)


if __name__ == "__main__":
    asyncio.run(main())
