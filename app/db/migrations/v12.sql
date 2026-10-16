-- v12：tags 表增加 write_rating / write_description 列。
-- 语义：该标签命中时是否把评分 / 描述写进 Immich。原先这两项由
-- settings.enabled_effects 里的 rating / description 全局控制，现在下放到标签维度，
-- 与 archive（是否归档）、skip_immich（是否写 Immich）同一套「标签即开关」的模型。
-- DEFAULT 1：升级后既有标签的行为不变（照旧写评分/描述），要停就逐个标签取消勾选。

ALTER TABLE tags ADD COLUMN write_rating INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tags ADD COLUMN write_description INTEGER NOT NULL DEFAULT 1;
