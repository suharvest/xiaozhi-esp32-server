-- liquibase formatted sql
-- changeset xiaozhi:202608041930-1
-- EdgeLLM 不再写死 model_name，改由探测自动填。
--
-- 起因：changeset 202608041030 里写的是 Qwen/Qwen3-4B-AWQ，而实测 orin-nx 上
-- /v1/models 报的是 Qwen/Qwen3.5-4B —— 版本不同、也没有 AWQ 后缀。客户设备上跑
-- 什么模型是他们的事，在这里写死任何一个具体值都必然对不上，而且对不上时的表现是
-- 「对话时报 model not found」，离配置错误的现场很远。
--
-- 留空 + 由 POST /models/probe 的 edgellm_models 探测回填。按回填规则（设计文档
-- §19）：只有一个候选时直接填入，无需用户选择——EdgeLLM 是单模型服务，正好命中。
--
-- 同时把 base_url 也退回 127.0.0.1 占位（202608041030 已经是占位值，这里保持），
-- 交付后由现场探测填真实地址。
--
-- 注意：不能给 model_name 加 remote-select 类型。表单字段是按 configJson.type 找
-- 供应商的（ModelEditDialog.vue:236），EdgeLLM 的 type 是 "openai"，与豆包 /
-- DeepSeek / 通义千问 / LM Studio 共用上游的 SYSTEM_LLM_openai 定义——改它会让那些
-- 云端模型的模型名也去打某台设备的 /v1/models。要做成下拉需要先给 EdgeLLM 拆出
-- 独立的 provider，另议。

UPDATE `ai_model_config`
SET config_json = JSON_SET(config_json, '$.model_name', '')
WHERE id = 'LLM_EdgeLLM'
  AND JSON_VALID(config_json);
