-- v5（UX 改进批次）：
-- 1. asset_records 补记图片文件名（列表页展示文件名替代超长 assetId；
--    历史记录该列为 NULL，前端回落显示 assetId）。
-- 2. max_tokens 旧默认 400 过小（多模态网关常见下限远高于此），
--    新默认 128000；存量行里仍是旧默认值的同步抬升
--    （用户显式改过的值不受影响，除非恰好等于旧默认 400）。

ALTER TABLE asset_records ADD COLUMN filename TEXT;

UPDATE llm_conn SET max_tokens = 128000 WHERE max_tokens = 400;
