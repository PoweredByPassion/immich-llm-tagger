-- v8：tags 表增加 skip_immich 列（忽略标签）。
-- 语义：该标签照常参与 LLM 识别与 asset_records 记录，
-- 但 apply 阶段不在 Immich 打标、不进相册、不改归档，仅摘队列标签。
-- 典型场景：normal_photo 等常规图不污染图库。

ALTER TABLE tags ADD COLUMN skip_immich INTEGER NOT NULL DEFAULT 0;
