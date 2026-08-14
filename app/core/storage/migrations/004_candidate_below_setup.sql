-- 004_candidate_below_setup.sql：重新武装计时持久化（总控文档第 4.3 节）

ALTER TABLE candidates ADD COLUMN below_setup_since INTEGER;
