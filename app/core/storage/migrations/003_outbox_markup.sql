-- 003_outbox_markup.sql：outbox 内联键盘（一次性确认 nonce 按钮，总控文档第 8.3 节）

ALTER TABLE telegram_outbox ADD COLUMN markup TEXT;
