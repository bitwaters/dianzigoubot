"""信号引擎入口：装配与运行（总控文档第 3.2、11.1 节）。

启动恢复必须先于发现和新信号：加载未终态 outbox、结果追踪计划、
今日 WS 计数与订阅、插件注册；恢复完成后才启动发现与订阅。
"""
from __future__ import annotations

import asyncio
import json
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
    StrategyFile,
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
from app.core.clients.telegram import ConfirmationManager, UpdatePoller, OutboxSender

log = logging.getLogger(__name__)


async def _noop(*args, **kwargs):
    return None


async def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.secrets)

    db = Database(settings.db_path)
    await db.open()
    repo = Repository(db.conn)

    # 1. 恢复：策略 ACTIVE 快照（文档第 6.1 节：运行只加载 SQLite 快照，
    #    编辑 strategy.yaml 不热加载；生效需 /strategy activate）
    activation = StrategyActivationService(repo)
    active = await activation.current_active()
    if active is None:
        strategy = load_strategy_file(settings.strategy_path)
        await activation.activate(strategy, admin_id=settings.telegram_admin_id)
        active = await activation.current_active()
        log.info("基准策略已激活: %s", active["revision"])
    strategy = StrategyFile.model_validate(json.loads(active["canonical"]))
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

    # G1 顶部池映射缓存（文档第 4.4 节：G1 事件触发映射解析；键含 chain）
    top_pool_cache: dict[tuple[str, str], dict] = {}
    top_pool_ttl_ms = 300_000

    def _g1_handler(chain):
        async def handler(event):
            key = (chain, event.token_address)
            entry = top_pool_cache.get(key)
            now = _now_ms()
            if entry is not None and now - entry["at"] < top_pool_ttl_ms:
                return
            if entry is not None and entry.get("retry_after") and now < entry["retry_after"]:
                return  # 刷新失败退避，避免每事件重试消耗额度
            try:
                pools = await cg.top_pools_by_token(
                    chain, event.token_address, include="base_token,quote_token,dex"
                )
                top_pool_cache[key] = {"at": now, "pools": pools}
                log.info(
                    "G1 顶部池映射已刷新 %s %s (%d 池)",
                    chain, event.token_address[:10], len(pools),
                )
            except Exception as exc:
                top_pool_cache[key] = {
                    "at": entry["at"] if entry else now,
                    "pools": entry["pools"] if entry else [],
                    "retry_after": now + 60_000,
                }
                log.warning("顶部池映射刷新失败 %s: %s", event.token_address[:10], exc)

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
    notify = NotifyAPI(
        repo,
        target_resolver={
            "admin": settings.telegram_admin_id,
            "channel": settings.telegram_signal_chat_id,
        },
    )
    commands = CommandRegistry()
    admin = AdminService(repo)
    # 插件熔断告警经通知 API 送达管理员
    bus.on_notify = lambda payload: notify.send(
        payload["text"], target="admin", priority=payload.get("priority", 0)
    )

    usage = UsageService(
        key_provider=cg.key,
        rest_ledger=cg.usage,
        credit_gates=ws_gates,
        alert_threshold=int(os.environ.get("CREDIT_ALERT_THRESHOLD", "100000")),
    )
    core = CoreActions(repo, activation, settings, cg, settings.strategy_path)
    admin.register("/budget", lambda args: _budget_lines(usage))
    admin.register("/strategy", core.strategy_command)
    admin.register("/telegram", core.telegram_command)

    async def status_provider() -> str:
        """/status 补充运行活动数据（候选/信号/投递/WS 状态/管线阶段计数）。"""
        lines = []
        for table in ("candidates", "signals"):
            cursor = await repo.conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            lines.append(f"{table}: {int(row[0])}")
        cursor = await repo.conn.execute(
            "SELECT COUNT(*) FROM telegram_outbox WHERE status IN ('PENDING','SENDING','FAILED')"
        )
        row = await cursor.fetchone()
        lines.append(f"outbox 未终态: {int(row[0])}")
        for chain in ("solana", "bsc"):
            lines.append(f"ws[{chain}]: {ws_clients[chain].state}")
        for chain in ("solana", "bsc"):
            stages = pipelines[chain].stages
            lines.append(
                f"pipeline[{chain}]: "
                + " ".join(f"{k}={v}" for k, v in stages.items())
            )
        return "\n".join(lines)

    admin.set_status_provider(status_provider)

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
            signal_chat_id=settings.telegram_signal_chat_id,
            active_snapshot=(strategy_revision, strategy_hash),
            top_pool_lookup=lambda c, t: [
                p
                for p in (top_pool_cache.get((c, t)) or {}).get("pools", [])
            ],
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
                # 用量账本周期性落库（api_usage 表，文档第 9.3 节）；
                # 先落库 commit 成功后再 clear，避免崩溃丢计数
                for interface, count in cg.usage.snapshot().items():
                    await repo.record_api_usage(
                        "REST", interface.replace("rest:", ""), attempts=count
                    )
                ws_total = cg.usage.ws_total()
                if ws_total > 0:
                    await repo.record_api_usage("WS", "all", charged_responses=ws_total)
                await repo.conn.commit()
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

    # 5. 发现循环（含 G1 订阅集合维护与候选过期清理）
    async def discovery_loop():
        while True:
            for chain in ("solana", "bsc"):
                if admin.paused:
                    continue
                try:
                    await discovery_tasks[chain].run()
                    # G1 订阅集合 = 活跃候选代币（文档第 4.5 节 WATCHING 突发订阅）
                    tokens = await repo.list_active_candidate_tokens(chain, limit=100)
                    ws_clients[chain].set_g1_tokens(set(tokens))
                except Exception:
                    log.exception("发现循环 %s 异常", chain)
            try:
                await repo.expire_candidates(_now_ms())
            except Exception:
                log.exception("候选过期清理异常")
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

            async def send_msg(chat_id, text, reply_markup=None):
                kwargs = {}
                if reply_markup:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                    # PTB 要求 inline_keyboard 为"序列的序列"：单行放置全部按钮
                    kwargs["reply_markup"] = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(**btn) for btn in reply_markup]]
                    )
                message = await bot.send_message(
                    chat_id=chat_id, text=text, **kwargs
                )
                return str(message.message_id)

            poller = UpdatePoller(repo, fetch_updates)
            confirmations = ConfirmationManager(repo)
            poller.on_update = lambda update: _handle_update(
                update,
                admin,
                commands,
                notify,
                confirmations,
                core,
                settings.telegram_admin_id,
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
    confirmations: "ConfirmationManager",
    core: "CoreActions",
    admin_id: int,
) -> None:
    # 确认回调：confirm:<nonce> / cancel:<nonce>（文档第 8.3 节）
    callback = update.get("callback_query")
    if callback is not None:
        message = callback.get("message") or {}
        chat_id = int(message.get("chat", {}).get("id", 0))
        from_user = callback.get("from") or {}
        user_id = int(from_user.get("id", 0))
        data = (callback.get("data") or "").strip()
        if data.startswith("confirm:"):
            payload = await confirmations.try_consume(
                data[len("confirm:"):], admin_id=user_id, chat_id=chat_id
            )
            if payload is None:
                await notify.send("确认无效、已过期或已被使用", target="admin")
                return
            if payload.get("action"):
                await core.execute(payload, notify)
            else:
                await _execute_plugin_command(commands, payload, notify)
        elif data.startswith("cancel:"):
            nonce = data[len("cancel:"):]
            if await confirmations.try_consume(nonce, admin_id=user_id, chat_id=chat_id) is not None:
                await notify.send("已取消", target="admin")
        return

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
    chat_id = int(message.get("chat", {}).get("id", 0))

    # 核心高风险命令二次确认（文档第 8.2、8.3 节）
    core_confirm = core.confirm_payload(command, args)
    if core_confirm is not None:
        nonce = await confirmations.create(
            admin_id=admin_id,
            chat_id=chat_id,
            update_id=int(update["update_id"]),
            command=command,
            args=args,
            payload=core_confirm,
        )
        markup = json.dumps(
            [
                {"text": "确认", "callback_data": f"confirm:{nonce}"},
                {"text": "取消", "callback_data": f"cancel:{nonce}"},
            ]
        )
        await notify.send(
            f"⚠️ 高风险命令 {command} {args} 需二次确认（60 秒内有效）",
            target="admin",
            markup=markup,
        )
        return

    plugin_cmd = commands.get(command)
    if plugin_cmd is not None:
        if plugin_cmd["requires_confirmation"]:
            nonce = await confirmations.create(
                admin_id=admin_id,
                chat_id=chat_id,
                update_id=int(update["update_id"]),
                command=command,
                args=args,
                payload={"command": command, "args": args, "message": message},
            )
            markup = json.dumps(
                [
                    {"text": "确认", "callback_data": f"confirm:{nonce}"},
                    {"text": "取消", "callback_data": f"cancel:{nonce}"},
                ]
            )
            await notify.send(
                f"⚠️ 高风险命令 {command} 需二次确认（60 秒内有效）",
                target="admin",
                markup=markup,
            )
            return
        try:
            await plugin_cmd["handler"](message)
            return
        except Exception:
            log.exception("插件命令处理异常: %s", command)
            return
    reply = await admin.handle(command, args)
    if reply:
        await notify.send(reply, target="admin")


class CoreActions:
    """核心高风险命令与策略管理命令（文档第 8.2、10.6 节）。"""

    def __init__(self, repo, activation, settings, cg, strategy_path) -> None:
        self._repo = repo
        self._activation = activation
        self._settings = settings
        self._cg = cg
        self._strategy_path = strategy_path

    def confirm_payload(self, command: str, args: str) -> dict | None:
        if command == "/strategy" and args.split(maxsplit=1)[0] == "activate":
            return {"action": "strategy_activate", "args": args}
        if command == "/strategy" and args.split(maxsplit=1)[0] == "rollback":
            return {"action": "strategy_rollback", "args": args}
        if command == "/telegram" and args.startswith("retry "):
            return {"action": "telegram_retry", "args": args}
        return None

    async def execute(self, payload: dict, notify) -> None:
        action = payload.get("action")
        if action == "strategy_activate":
            await notify.send(await self._do_activate(payload.get("args", "")), target="admin")
        elif action == "strategy_rollback":
            await notify.send(await self._do_rollback(payload.get("args", "")), target="admin")
        elif action == "telegram_retry":
            await notify.send(await self._do_telegram_retry(payload.get("args", "")), target="admin")
        else:
            await notify.send(f"未知核心动作: {action}", target="admin")

    async def _do_activate(self, args: str) -> str:
        from app.core.config import load_strategy_file

        try:
            candidate = load_strategy_file(self._strategy_path)
        except Exception as exc:
            return f"strategy.yaml 校验失败: {exc}"
        try:
            aid = await self._activation.activate(
                candidate, admin_id=self._settings.telegram_admin_id
            )
        except Exception as exc:
            return f"激活失败: {exc}"
        return f"已激活 {candidate.revision}（activation_id={aid[:8]}）"

    async def _do_rollback(self, args: str) -> str:
        revision = args.split(maxsplit=1)[1].strip() if len(args.split(maxsplit=1)) > 1 else ""
        if not revision:
            return "用法: /strategy rollback <revision>"
        try:
            aid = await self._activation.rollback(
                revision, admin_id=self._settings.telegram_admin_id
            )
        except Exception as exc:
            return f"回退失败: {exc}"
        return f"已回退到 {revision}（activation_id={aid[:8]}）"

    async def _do_telegram_retry(self, args: str) -> str:
        key = args.split(maxsplit=1)[1].strip() if len(args.split(maxsplit=1)) > 1 else ""
        if not key:
            return "用法: /telegram retry <delivery_key>"
        row = await self._repo.get_outbox(key)
        if row is None:
            return f"delivery_key 不存在: {key}"
        if row["status"] == "SENT":
            return f"该消息已投递成功，无需补发: {key}"
        await self._repo.requeue_outbox(key)
        return f"已重新入队: {key}"

    async def strategy_command(self, args: str) -> str:
        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else ""
        sub_args = parts[1] if len(parts) > 1 else ""
        if sub == "status":
            active = await self._activation.current_active()
            if active is None:
                return "无 ACTIVE 策略"
            return (
                f"ACTIVE: {active['revision']}\n"
                f"hash: {active['strategy_hash']}\n"
                f"parent: {active['parent_revision'] or '-'}\n"
                f"reason: {active['change_reason']}"
            )
        if sub == "validate-collection":
            from app.core.config import load_strategy_file
            from app.core.services.discovery import collection_dry_run

            try:
                candidate = load_strategy_file(self._strategy_path)
            except Exception as exc:
                return f"strategy.yaml 校验失败: {exc}"
            lines = ["collection dry-run:"]
            for chain in ("solana", "bsc"):
                report = await collection_dry_run(
                    chain, getattr(candidate, chain), self._cg
                )
                for tpl_id, entry in report["templates"].items():
                    if entry["ok"]:
                        lines.append(
                            f"  {chain}/{tpl_id}: {entry['count']} 池 "
                            f"({entry['latency_seconds']}s)"
                        )
                    else:
                        lines.append(f"  {chain}/{tpl_id}: ERROR {entry['error']}")
            return "\n".join(lines)
        if sub == "backtest":
            return await self._backtest_summary()
        if sub in ("activate", "rollback"):
            return f"/strategy {sub} 需要二次确认，请直接发送命令（将弹出确认按钮）"
        return (
            "用法:\n"
            "/strategy status\n"
            "/strategy backtest\n"
            "/strategy validate-collection\n"
            "/strategy activate\n"
            "/strategy rollback <revision>"
        )

    async def _backtest_summary(self) -> str:
        from app.core.services.strategy import StrategyActivationService

        active = await self._activation.current_active()
        cursor = await self._repo.conn.execute("SELECT COUNT(*) FROM signals")
        row = await cursor.fetchone()
        signal_count = int(row[0])
        cursor = await self._repo.conn.execute(
            "SELECT COUNT(*) FROM outcome_snapshots"
        )
        row = await cursor.fetchone()
        outcome_count = int(row[0])
        cursor = await self._repo.conn.execute(
            "SELECT COUNT(*) FROM market_bars WHERE timeframe = '1m'"
        )
        row = await cursor.fetchone()
        bars_count = int(row[0])
        lines = [
            "回测数据覆盖:",
            f"  信号数: {signal_count}",
            f"  结果标签数: {outcome_count}",
            f"  1m 行情条数: {bars_count}",
        ]
        if active is not None:
            lines.append(f"  ACTIVE: {active['revision']}")
        if signal_count == 0:
            lines.append("暂无历史信号；回测需要决策快照积累，请先运行积累数据。")
        return "\n".join(lines)

    async def telegram_command(self, args: str) -> str:
        if args.startswith("retry "):
            return "/telegram retry 需要二次确认，请直接发送命令（将弹出确认按钮）"
        return "用法: /telegram retry <delivery_key>"


async def _execute_plugin_command(
    commands: CommandRegistry, payload: dict, notify: NotifyAPI
) -> None:
    """确认通过后执行插件命令（文档第 8.3 节：确认消费与业务在同一事务语义内）。"""
    plugin_cmd = commands.get(payload.get("command", ""))
    if plugin_cmd is None:
        await notify.send("命令已失效（插件可能被卸载）", target="admin")
        return
    try:
        await plugin_cmd["handler"](payload.get("message") or {})
    except Exception:
        log.exception("插件命令执行异常: %s", payload.get("command"))
        await notify.send("命令执行失败，详见日志", target="admin")


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
