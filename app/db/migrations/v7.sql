-- v7：删除 categories 表与相关校验。
-- 「分类体系规则」从未接入分类管线（pipeline 的 {{SCOPE}} 恒为 None，
-- apply 只读 tags.album/archive），是与标签相册绑定重复的死功能。
-- 表本身无消费者，直接 DROP。v1.sql 保留建表语句（历史迁移不可改），
-- 新库会先建后删，最终 schema 一致。

DROP TABLE IF EXISTS categories;
