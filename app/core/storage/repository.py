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
