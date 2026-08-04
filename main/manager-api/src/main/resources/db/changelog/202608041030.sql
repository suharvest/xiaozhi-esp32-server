-- liquibase formatted sql
-- changeset xiaozhi:202608041030-1
-- Seeed 自有供应商：OpenVoiceStream ASR/TTS + EdgeLLM，注册进智控台使其可配置。
--
-- 本文件取代了原先游离的 main/manager-api/seeed-providers.sql —— 那份不是
-- liquibase changeset，客户全新部署后智控台里根本看不到这几个供应商，需要人工
-- 进数据库执行。转成 changeset 后随迁移自动建好。
--
-- config_json 里的 `type` = 服务端 provider 实现的文件名
-- （openvoicestream / openvoicestream_tts / openai），create_instance 据此加载
-- core/providers/{asr,tts}/{type}.py。
--
-- 地址一律用 127.0.0.1 占位，交付后在智控台里改成实际的设备地址。
-- 不要在这里写任何具体环境的 IP。
--
-- DELETE + INSERT，可重复执行。

-- ── 供应商定义（驱动 manager-web 的配置表单）──
DELETE FROM `ai_model_provider` WHERE id IN
  ('SYSTEM_ASR_OpenVoiceStream', 'SYSTEM_TTS_OpenVoiceStream');
INSERT INTO `ai_model_provider`
  (id, model_type, provider_code, name, fields, sort, creator, create_date, updater, update_date) VALUES
('SYSTEM_ASR_OpenVoiceStream', 'ASR', 'openvoicestream', 'OpenVoiceStream流式ASR(本地)',
 '[{"key":"type","type":"string","label":"类型"},{"key":"ws_url","type":"string","label":"WS地址"},{"key":"api_key","type":"password","label":"API Key(OVS_API_KEYS,可留空)"},{"key":"sample_rate","type":"number","label":"采样率"},{"key":"language","type":"string","label":"语言"},{"key":"final_timeout","type":"number","label":"结束超时"},{"key":"fallback_to_partial","type":"boolean","label":"回退部分结果"},{"key":"allow_backend_endpoint","type":"boolean","label":"允许后端端点"}]',
 100, 1, NOW(), 1, NOW()),
('SYSTEM_TTS_OpenVoiceStream', 'TTS', 'openvoicestream_tts', 'OpenVoiceStream流式TTS(本地)',
 '[{"key":"type","type":"string","label":"类型"},{"key":"base_url","type":"string","label":"基础URL"},{"key":"api_key","type":"password","label":"API Key(OVS_API_KEYS,可留空)"},{"key":"speaker_id","type":"number","label":"音色ID"},{"key":"sid","type":"number","label":"音色ID(旧协议,留空)"},{"key":"speed","type":"number","label":"语速"},{"key":"timeout","type":"number","label":"超时(秒)"}]',
 100, 1, NOW(), 1, NOW());

-- ── 模型配置（服务端读 config_json，`type` 决定用哪个实现）──
DELETE FROM `ai_model_config` WHERE id IN
  ('ASR_OpenVoiceStream', 'TTS_OpenVoiceStream', 'LLM_EdgeLLM');
INSERT INTO `ai_model_config`
  (id, model_type, model_code, model_name, is_default, is_enabled, config_json, sort, creator, create_date, updater, update_date) VALUES
('ASR_OpenVoiceStream', 'ASR', 'OpenVoiceStream', 'OpenVoiceStream流式ASR(本地)', 0, 1,
 '{"type":"openvoicestream","ws_url":"ws://127.0.0.1:8621/asr/stream","api_key":"","sample_rate":16000,"language":"auto","final_timeout":5.0,"fallback_to_partial":true,"allow_backend_endpoint":true}',
 100, 1, NOW(), 1, NOW()),
-- 注意：不要给 sid 填 0。服务端 _to_optional_int(0) 返回 0 而非 None，而 payload
-- 优先级是 speaker_embedding_b64 > speaker_id > sid，填了 0 会永远压住 speaker_id，
-- 导致智控台里选的音色不生效。留空即可。
('TTS_OpenVoiceStream', 'TTS', 'OpenVoiceStream', 'OpenVoiceStream流式TTS(本地)', 0, 1,
 '{"type":"openvoicestream_tts","base_url":"http://127.0.0.1:8621","api_key":"","speed":1.0,"timeout":30}',
 100, 1, NOW(), 1, NOW()),
('LLM_EdgeLLM', 'LLM', 'EdgeLLM', 'EdgeLLM (本地)', 0, 1,
 '{"type":"openai","base_url":"http://127.0.0.1:8000/v1","model_name":"Qwen/Qwen3-4B-AWQ","api_key":"EMPTY","temperature":0.7,"max_tokens":2048}',
 100, 1, NOW(), 1, NOW());
