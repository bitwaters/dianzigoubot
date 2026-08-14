-- 001_init.sql：核心表全集（总控文档第 9.3 节）
-- 时间字段统一为 UTC 毫秒整数；金额/比例/分数统一为十进制字符串。

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    name TEXT,
    symbol TEXT,
    decimals INTEGER,
    coingecko_coin_id TEXT,
    first_seen_at INTEGER NOT NULL,
    UNIQUE (chain, token_address)
);

CREATE TABLE IF NOT EXISTS pools (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    dex TEXT,
    quote_address TEXT,
    created_at INTEGER,
    first_seen_at INTEGER NOT NULL,
    UNIQUE (chain, pool_address)
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'WATCHING',
    status_reason TEXT,
    trade_allowed INTEGER NOT NULL DEFAULT 0,
    labels TEXT NOT NULL DEFAULT '[]',
    main_label TEXT,
    signal_generation INTEGER NOT NULL DEFAULT 0,
    setup_started_at INTEGER,
    total_score TEXT,
    strategy_revision TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER,
    UNIQUE (chain, token_address)
);

CREATE TABLE IF NOT EXISTS market_bars (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    ohlcv_kind TEXT NOT NULL,
    entity_address TEXT NOT NULL,
    token_side TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_timestamp INTEGER NOT NULL,
    open TEXT,
    high TEXT,
    low TEXT,
    close TEXT,
    volume TEXT,
    synthetic INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chain, ohlcv_kind, entity_address, token_side, timeframe, candle_timestamp)
);

CREATE TABLE IF NOT EXISTS security_checks (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT,
    check_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    source_time INTEGER,
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_security_token
    ON security_checks (chain, token_address, check_kind, created_at);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    signal_level TEXT NOT NULL,
    total_score TEXT,
    reference_price TEXT,
    signal_generation INTEGER NOT NULL,
    strategy_revision TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    security_snapshot TEXT NOT NULL DEFAULT '{}',
    telegram_status TEXT NOT NULL DEFAULT 'PENDING',
    event_published INTEGER NOT NULL DEFAULT 0,
    decision_snapshot TEXT NOT NULL DEFAULT '{}',
    expires_at INTEGER NOT NULL,
    invalidated_at INTEGER,
    invalidated_reason TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (chain, token_address, strategy_revision, signal_generation)
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at INTEGER NOT NULL,
    result TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS telegram_offset (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    committed_update_id INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO telegram_offset (id, committed_update_id) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY,
    delivery_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    parent_id TEXT,
    chat_target TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    telegram_message_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_status
    ON telegram_outbox (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS telegram_confirmations (
    id INTEGER PRIMARY KEY,
    nonce TEXT NOT NULL UNIQUE,
    admin_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    command_hash TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    expires_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    consumed_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_snapshots (
    id INTEGER PRIMARY KEY,
    revision TEXT NOT NULL UNIQUE,
    parent_revision TEXT,
    strategy_hash TEXT NOT NULL,
    canonical TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    activated_at INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_strategy
    ON strategy_snapshots (is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS strategy_activations (
    id INTEGER PRIMARY KEY,
    activation_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    before_revision TEXT,
    after_revision TEXT,
    before_hash TEXT,
    after_hash TEXT,
    admin_id INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    interface TEXT NOT NULL,
    chain TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    charged_responses INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_usage_day
    ON api_usage (interface, created_at);

CREATE TABLE IF NOT EXISTS discovery_snapshots (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    source_template TEXT NOT NULL,
    source_label TEXT,
    features TEXT NOT NULL DEFAULT '{}',
    completeness TEXT NOT NULL DEFAULT '{}',
    requested_at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_discovery_token
    ON discovery_snapshots (chain, token_address, created_at);

CREATE TABLE IF NOT EXISTS wallet_observations (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    buy_price_usd TEXT NOT NULL,
    buy_volume_usd TEXT NOT NULL,
    buy_timestamp INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (chain, wallet_address, token_address, pool_address, buy_timestamp)
);

CREATE TABLE IF NOT EXISTS wallet_outcome_labels (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES wallet_observations(id),
    strategy_hash TEXT NOT NULL,
    label TEXT NOT NULL,
    trigger_time INTEGER,
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE (observation_id, strategy_hash)
);

CREATE TABLE IF NOT EXISTS wallet_reputation (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    computed_at INTEGER NOT NULL,
    observed_tokens INTEGER NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    failure INTEGER NOT NULL DEFAULT 0,
    neutral INTEGER NOT NULL DEFAULT 0,
    rug_exposed INTEGER NOT NULL DEFAULT 0,
    coverage_pct TEXT,
    is_local_smart INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chain, wallet_address, strategy_hash, computed_at)
);

CREATE TABLE IF NOT EXISTS outcome_snapshots (
    id INTEGER PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    horizon TEXT NOT NULL,
    price TEXT,
    price_high TEXT,
    price_low TEXT,
    liquidity_usd TEXT,
    pool_removed INTEGER NOT NULL DEFAULT 0,
    completeness TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    UNIQUE (signal_id, horizon)
);

CREATE TABLE IF NOT EXISTS historical_ranges (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    ohlcv_kind TEXT NOT NULL,
    entity_address TEXT NOT NULL,
    token_side TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    backfilled_at INTEGER,
    last_backtest_at INTEGER,
    UNIQUE (chain, ohlcv_kind, entity_address, token_side, timeframe, range_start, range_end)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    strategy_revision TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    dataset_hash TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
