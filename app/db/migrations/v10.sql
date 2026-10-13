-- v10：删除 llm_conn.concurrency（死配置）。
-- 优化批次把 worker 池固定为 5（spec：分类并发 = worker 数），不再读取
-- llm_conn.concurrency；该列仍被配置页输入框写入却无任何消费者，误导用户。
-- 直接 DROP COLUMN（SQLite 3.35+ 支持；本项目随附 3.46）。列无索引/约束引用，
-- 可原地删除，无需 v7/v9 式的表重建。
ALTER TABLE llm_conn DROP COLUMN concurrency;
