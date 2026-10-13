-- v3（LLM 多模态开关）之后的 v4：asset_records 记录 LLM 请求参数。
-- 图片详情页原本只展示 raw_response；补记发送给 LLM 的输入
-- （model / max_tokens / messages，含 prompt 文本与是否带图），
-- base64 图片数据不落库（体积大且可随时从 immich 重取），只留
-- data-url 前缀 + 长度摘要。
ALTER TABLE asset_records ADD COLUMN llm_request TEXT;
