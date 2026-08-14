"""Repository：核心表 SQL 集中层（总控文档第 12.1 节：SQL 集中在 Repository 层）。"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from app.core.models import utc_now_ms

CANDIDATE_STATUSES = ("WATCHING", "SETUP", "SIGNAL", "REJECTED", "EXPIRED")


class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> aiosqlite.Connection:
        return self._conn

    # -- tokens / pools -----------------------------------------------------

    async def upsert_token(
        self,
        chain: str,
        token_address: str,
        *,
        name: str | None = None,
        symbol: str | None = None,
        decimals: int | None = None,
        coingecko_coin_id: str | None = None,
    ) -> int:
        now = utc_now_ms()
        cursor = await self._conn.execute(
            """INSERT INTO tokens (chain, token_address, name, symbol, decimals,
                                   coingecko_coin_id, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chain, token_address) DO UPDATE SET
                   name = COALESCE(excluded.name, tokens.name),
                   symbol = COALESCE(excluded.symbol, tokens.symbol),
                   decimals = COALESCE(excluded.decimals, tokens.decimals),
                   coingecko_coin_id = COALESCE(excluded.coingecko_coin_id, tokens.coingecko_coin_id)
               RETURNING id""",
            (chain, token_address, name, symbol, decimals, coingecko_coin_id, now),
        )
        row = await cursor.fetchone()
        return int(row["id"])

    async def upsert_pool(
        self,
        chain: str,
        pool_address: str,
        token_id: int,
        *,
        dex: str | None = None,
        quote_address: str | None = None,
        created_at: int | None = None,
    ) -> int:
        now = utc_now_ms()
        cursor = await self._conn.execute(
            """INSERT INTO pools (chain, pool_address, token_id, dex, quote_address,
                                  created_at, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chain, pool_address) DO UPDATE SET
                   token_id = excluded.token_id,
                   dex = COALESCE(excluded.dex, pools.dex),
                   quote_address = COALESCE(excluded.quote_address, pools.quote_address)
               RETURNING id""",
            (chain, pool_address, token_id, dex, quote_address, created_at, now),
        )
        row = await cursor.fetchone()
        return int(row["id"])

    async def get_token_id(self, chain: str, token_address: str) -> int | None:
        cursor = await self._conn.execute(
            "SELECT id FROM tokens WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        )
        row = await cursor.fetchone()
        return int(row["id"]) if row else None

    # -- candidates ---------------------------------------------------------

    async def upsert_candidate(
        self,
        chain: str,
        token_address: str,
        pool_address: str,
        *,
        status: str = "WATCHING",
        status_reason: str | None = None,
        trade_allowed: bool = False,
        labels: list[str] | None = None,
        main_label: str | None = None,
        signal_generation: int = 0,
        setup_started_at: int | None = None,
        total_score: str | None = None,
        strategy_revision: str | None = None,
        expires_at: int | None = None,
    ) -> None:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"非法候选状态: {status}")
        now = utc_now_ms()
        await self._conn.execute(
            """INSERT INTO candidates (chain, token_address, pool_address, status,
                                       status_reason, trade_allowed, labels, main_label,
                                       signal_generation, setup_started_at, total_score,
                                       strategy_revision, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chain, token_address) DO UPDATE SET
                   pool_address = excluded.pool_address,
                   status = excluded.status,
                   status_reason = excluded.status_reason,
                   trade_allowed = excluded.trade_allowed,
                   labels = excluded.labels,
                   main_label = excluded.main_label,
                   signal_generation = excluded.signal_generation,
                   setup_started_at = excluded.setup_started_at,
                   total_score = excluded.total_score,
                   strategy_revision = excluded.strategy_revision,
                   updated_at = excluded.updated_at,
                   expires_at = excluded.expires_at""",
            (
                chain,
                token_address,
                pool_address,
                status,
                status_reason,
                int(trade_allowed),
                json.dumps(labels or []),
                main_label,
                signal_generation,
                setup_started_at,
                total_score,
                strategy_revision,
                now,
                now,
                expires_at,
            ),
        )

    async def get_candidate(
        self, chain: str, token_address: str
    ) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            "SELECT * FROM candidates WHERE chain = ? AND token_address = ?",
            (chain, token_address),
        )
        return await cursor.fetchone()

    async def list_candidates(self, chain: str) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM candidates WHERE chain = ? ORDER BY updated_at DESC",
            (chain,),
        )
        return list(await cursor.fetchall())

    async def list_candidates_limit(self, limit: int = 20) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM candidates ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return list(await cursor.fetchall())

    async def list_signals(self, limit: int = 10) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return list(await cursor.fetchall())

    # -- security_checks ----------------------------------------------------

    async def insert_security_check(
        self,
        chain: str,
        token_address: str,
        check_kind: str,
        status: str,
        *,
        pool_address: str | None = None,
        details: dict[str, Any] | None = None,
        source_time: int | None = None,
        expires_at: int | None = None,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO security_checks (chain, token_address, pool_address,
                                           check_kind, status, details, source_time,
                                           created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chain,
                token_address,
                pool_address,
                check_kind,
                status,
                json.dumps(details or {}),
                source_time,
                utc_now_ms(),
                expires_at,
            ),
        )

    async def latest_security_check(
        self, chain: str, token_address: str, check_kind: str
    ) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            """SELECT * FROM security_checks
               WHERE chain = ? AND token_address = ? AND check_kind = ?
               ORDER BY created_at DESC LIMIT 1""",
            (chain, token_address, check_kind),
        )
        return await cursor.fetchone()

    # -- signals ------------------------------------------------------------

    async def insert_signal(
        self,
        *,
        signal_id: str,
        chain: str,
        token_address: str,
        pool_address: str,
        signal_level: str,
        total_score: str,
        reference_price: str,
        signal_generation: int,
        strategy_revision: str,
        strategy_hash: str,
        security_snapshot: dict[str, Any],
        decision_snapshot: dict[str, Any],
        expires_at: int,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO signals (signal_id, chain, token_address, pool_address,
                                    signal_level, total_score, reference_price,
                                    signal_generation, strategy_revision, strategy_hash,
                                    security_snapshot, telegram_status, event_published,
                                    decision_snapshot, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)""",
            (
                signal_id,
                chain,
                token_address,
                pool_address,
                signal_level,
                total_score,
                reference_price,
                signal_generation,
                strategy_revision,
                strategy_hash,
                json.dumps(security_snapshot),
                json.dumps(decision_snapshot),
                expires_at,
                utc_now_ms(),
            ),
        )

    async def get_signal(self, signal_id: str) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
        )
        return await cursor.fetchone()

    async def mark_signal_event_published(self, signal_id: str) -> None:
        await self._conn.execute(
            "UPDATE signals SET event_published = 1 WHERE signal_id = ?",
            (signal_id,),
        )

    async def list_unpublished_signals(self, limit: int = 100) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM signals WHERE event_published = 0 ORDER BY created_at LIMIT ?",
            (limit,),
        )
        return list(await cursor.fetchall())

    async def invalidate_signal(
        self, signal_id: str, reason: str, invalidated_at: int | None = None
    ) -> None:
        await self._conn.execute(
            "UPDATE signals SET invalidated_at = ?, invalidated_reason = ? "
            "WHERE signal_id = ? AND invalidated_at IS NULL",
            (invalidated_at or utc_now_ms(), reason, signal_id),
        )

    async def upsert_schedule(self, chain: str, template_id: str, next_due: int) -> None:
        await self._conn.execute(
            """INSERT INTO discovery_schedule (chain, template_id, next_due_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chain, template_id) DO UPDATE SET next_due_at = excluded.next_due_at""",
            (chain, template_id, next_due),
        )

    async def get_next_due(self, chain: str, template_id: str) -> int | None:
        cursor = await self._conn.execute(
            "SELECT next_due_at FROM discovery_schedule WHERE chain = ? AND template_id = ?",
            (chain, template_id),
        )
        row = await cursor.fetchone()
        return int(row["next_due_at"]) if row else None

    async def reset_schedule(self, chain: str) -> None:
        await self._conn.execute(
            "DELETE FROM discovery_schedule WHERE chain = ?", (chain,)
        )

    # -- telegram_outbox（核心统一投递，总控文档第 8.3 节） -----------------

    async def insert_outbox(
        self,
        *,
        delivery_key: str,
        kind: str,
        parent_id: str | None,
        chat_target: str,
        text: str,
        priority: int = 0,
        now_ms: int | None = None,
    ) -> None:
        now = now_ms or utc_now_ms()
        await self._conn.execute(
            """INSERT INTO telegram_outbox (delivery_key, kind, parent_id,
                                            chat_target, text, status,
                                            retry_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)""",
            (delivery_key, kind, parent_id, chat_target, text, now, now),
        )

    async def get_outbox(self, delivery_key: str) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            "SELECT * FROM telegram_outbox WHERE delivery_key = ?", (delivery_key,)
        )
        return await cursor.fetchone()

    async def list_outbox_pending(self, limit: int = 50) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(
            """SELECT * FROM telegram_outbox
               WHERE status IN ('PENDING', 'SENDING')
               ORDER BY created_at LIMIT ?""",
            (limit,),
        )
        return list(await cursor.fetchall())

    async def requeue_failed_outbox(self, now_ms: int) -> int:
        """明确失败且到达退避时间的消息重新入队（同一 delivery_key 补发）。"""
        cursor = await self._conn.execute(
            """UPDATE telegram_outbox SET status = 'PENDING', updated_at = ?
               WHERE status = 'FAILED' AND next_attempt_at IS NOT NULL
                 AND next_attempt_at <= ?""",
            (now_ms, now_ms),
        )
        return cursor.rowcount

    async def update_outbox_status(
        self,
        delivery_key: str,
        status: str,
        *,
        telegram_message_id: str | None = None,
        retry_count: int | None = None,
        next_attempt_at: int | None = None,
    ) -> None:
        fields = ["status = ?"]
        params: list[Any] = [status]
        if telegram_message_id is not None:
            fields.append("telegram_message_id = ?")
            params.append(telegram_message_id)
        if retry_count is not None:
            fields.append("retry_count = ?")
            params.append(retry_count)
        if next_attempt_at is not None:
            fields.append("next_attempt_at = ?")
            params.append(next_attempt_at)
        fields.append("updated_at = ?")
        params.append(utc_now_ms())
        params.append(delivery_key)
        await self._conn.execute(
            f"UPDATE telegram_outbox SET {', '.join(fields)} WHERE delivery_key = ?",
            tuple(params),
        )

    async def mark_sending_recovery(self) -> int:
        """恢复：遗留 SENDING 无法证明已发送 → DELIVERY_UNKNOWN。"""
        cursor = await self._conn.execute(
            """UPDATE telegram_outbox SET status = 'DELIVERY_UNKNOWN',
                                        updated_at = ?
               WHERE status = 'SENDING' RETURNING delivery_key""",
            (utc_now_ms(),),
        )
        rows = await cursor.fetchall()
        return len(rows)

    # -- telegram_updates 与 offset -----------------------------------------

    async def get_committed_offset(self) -> int:
        cursor = await self._conn.execute(
            "SELECT committed_update_id FROM telegram_offset WHERE id = 1"
        )
        row = await cursor.fetchone()
        return int(row["committed_update_id"]) if row else 0

    async def set_committed_offset(self, update_id: int) -> None:
        cursor = await self._conn.execute(
            """UPDATE telegram_offset SET committed_update_id = ?
               WHERE id = 1 AND committed_update_id < ? RETURNING committed_update_id""",
            (update_id, update_id),
        )
        await cursor.fetchone()

    async def insert_update(self, update_id: int, result: dict) -> bool:
        """update 去重：已存在返回 False。"""
        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO telegram_updates (update_id, processed_at, result)
               VALUES (?, ?, ?)""",
            (update_id, utc_now_ms(), json.dumps(result, ensure_ascii=False)),
        )
        return cursor.rowcount > 0

    async def get_update(self, update_id: int) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            "SELECT * FROM telegram_updates WHERE update_id = ?", (update_id,)
        )
        return await cursor.fetchone()

    # -- telegram_confirmations（一次性确认 nonce） ---------------------------

    async def insert_confirmation(
        self,
        *,
        nonce: str,
        admin_id: int,
        chat_id: int,
        update_id: int,
        command_hash: str,
        payload: dict,
        expires_at: int,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO telegram_confirmations
               (nonce, admin_id, chat_id, update_id, command_hash, payload,
                expires_at, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
            (
                nonce,
                admin_id,
                chat_id,
                update_id,
                command_hash,
                json.dumps(payload, ensure_ascii=False),
                expires_at,
                utc_now_ms(),
            ),
        )

    async def get_confirmation(self, nonce: str) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            "SELECT * FROM telegram_confirmations WHERE nonce = ?", (nonce,)
        )
        return await cursor.fetchone()

    async def consume_confirmation(self, nonce: str, status: str) -> bool:
        """一次性消费：仅 PENDING 且未过期可消费；同一事务内推进业务。"""
        cursor = await self._conn.execute(
            """UPDATE telegram_confirmations SET status = ?, consumed_at = ?
               WHERE nonce = ? AND status = 'PENDING' AND expires_at >= ?""",
            (status, utc_now_ms(), nonce, utc_now_ms()),
        )
        return cursor.rowcount > 0

    # -- outcome_snapshots（结果标签，文档第 4.4.2 节） ----------------------

    async def insert_outcome_snapshot(
        self,
        *,
        signal_id: str,
        chain: str,
        token_address: str,
        pool_address: str,
        horizon: str,
        price: str | None,
        price_high: str | None,
        price_low: str | None,
        liquidity_usd: str | None,
        pool_removed: bool,
        completeness: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO outcome_snapshots
               (signal_id, chain, token_address, pool_address, horizon,
                price, price_high, price_low, liquidity_usd, pool_removed,
                completeness, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(signal_id, horizon) DO UPDATE SET
                   price = excluded.price,
                   price_high = excluded.price_high,
                   price_low = excluded.price_low,
                   liquidity_usd = excluded.liquidity_usd,
                   pool_removed = excluded.pool_removed,
                   completeness = excluded.completeness,
                   captured_at = excluded.captured_at""",
            (
                signal_id,
                chain,
                token_address,
                pool_address,
                horizon,
                price,
                price_high,
                price_low,
                liquidity_usd,
                int(pool_removed),
                completeness,
                utc_now_ms(),
            ),
        )

    async def get_outcome_snapshot(self, signal_id: str, horizon: str) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(
            "SELECT * FROM outcome_snapshots WHERE signal_id = ? AND horizon = ?",
            (signal_id, horizon),
        )
        return await cursor.fetchone()

    async def list_due_outcomes(self, now_ms: int, limit: int = 100) -> list[aiosqlite.Row]:
        """到期未采集的结果标签计划：信号发出时间 + horizon 已过且无对应快照。"""
        cursor = await self._conn.execute(
            """SELECT s.signal_id, s.chain, s.token_address, s.pool_address,
                      s.created_at, o.horizon
               FROM signals s
               LEFT JOIN outcome_snapshots o ON o.signal_id = s.signal_id
               WHERE o.horizon IS NULL
               ORDER BY s.created_at LIMIT ?""",
            (limit,),
        )
        return list(await cursor.fetchall())

    # -- api_usage ----------------------------------------------------------

    async def record_api_usage(
        self,
        kind: str,
        interface: str,
        *,
        chain: str | None = None,
        attempts: int = 1,
        charged_responses: int = 0,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO api_usage (kind, interface, chain, attempts,
                                      charged_responses, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, interface, chain, attempts, charged_responses, utc_now_ms()),
        )
