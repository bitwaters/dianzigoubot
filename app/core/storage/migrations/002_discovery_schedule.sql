-- 002_discovery_schedule.sql：查询模板调度状态持久化（总控文档第 4.2、9.3 节）

CREATE TABLE IF NOT EXISTS discovery_schedule (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    template_id TEXT NOT NULL,
    next_due_at INTEGER NOT NULL,
    UNIQUE (chain, template_id)
);
