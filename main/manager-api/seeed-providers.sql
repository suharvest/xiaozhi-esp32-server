-- Seeed custom providers for the xiaozhi manager console (智控台).
-- Registers OpenVoiceStream ASR/TTS + EdgeLLM so they're configurable in the
-- manager UI, mirroring the local data/.config.yaml. Idempotent (DELETE+INSERT).
-- NOT a liquibase changelog (kept separate to avoid upstream merge conflicts);
-- apply manually after a fresh manager deploy, or load into the running DB.
--
-- Note: config_json carries `type` = the server-side provider impl filename
-- (openvoicestream / openvoicestream_tts / openai), which create_instance uses
-- to load core/providers/{asr,tts}/{type}.py. Endpoints point at orin-nx.

-- ── Provider definitions (drive the manager-web config form) ──
DELETE FROM `ai_model_provider` WHERE id IN
  ('SYSTEM_ASR_OpenVoiceStream', 'SYSTEM_TTS_OpenVoiceStream');
INSERT INTO `ai_model_provider`
  (id, model_type, provider_code, name, fields, sort, creator, create_date, updater, update_date) VALUES
('SYSTEM_ASR_OpenVoiceStream', 'ASR', 'openvoicestream', 'OpenVoiceStream流式ASR(本地)',
 '[{"key":"type","type":"string","label":"类型"},{"key":"ws_url","type":"string","label":"WS地址"},{"key":"sample_rate","type":"number","label":"采样率"},{"key":"language","type":"string","label":"语言"},{"key":"final_timeout","type":"number","label":"结束超时"},{"key":"partial_results","type":"boolean","label":"部分结果"},{"key":"fallback_to_partial","type":"boolean","label":"回退部分结果"},{"key":"allow_backend_endpoint","type":"boolean","label":"允许后端端点"}]',
 100, 1, NOW(), 1, NOW()),
('SYSTEM_TTS_OpenVoiceStream', 'TTS', 'openvoicestream_tts', 'OpenVoiceStream流式TTS(本地)',
 '[{"key":"type","type":"string","label":"类型"},{"key":"base_url","type":"string","label":"基础URL"},{"key":"sid","type":"number","label":"声音ID"},{"key":"speed","type":"number","label":"语速"},{"key":"timeout","type":"number","label":"超时(秒)"}]',
 100, 1, NOW(), 1, NOW());

-- ── Model configs (server reads config_json; `type` selects impl) ──
DELETE FROM `ai_model_config` WHERE id IN
  ('ASR_OpenVoiceStream', 'TTS_OpenVoiceStream', 'LLM_EdgeLLM');
INSERT INTO `ai_model_config`
  (id, model_type, model_code, model_name, is_default, is_enabled, config_json, sort, creator, create_date, updater, update_date) VALUES
('ASR_OpenVoiceStream', 'ASR', 'OpenVoiceStream', 'OpenVoiceStream流式ASR(本地)', 0, 1,
 '{"type":"openvoicestream","ws_url":"ws://100.82.225.102:8621/asr/stream","sample_rate":16000,"language":"auto","final_timeout":5.0,"partial_results":false,"fallback_to_partial":true,"allow_backend_endpoint":true}',
 100, 1, NOW(), 1, NOW()),
('TTS_OpenVoiceStream', 'TTS', 'OpenVoiceStream', 'OpenVoiceStream流式TTS(本地)', 0, 1,
 '{"type":"openvoicestream_tts","base_url":"http://100.82.225.102:8621","sid":0,"speed":1.0,"timeout":30}',
 100, 1, NOW(), 1, NOW()),
('LLM_EdgeLLM', 'LLM', 'EdgeLLM', 'EdgeLLM Qwen3-4B (orin-nx)', 0, 1,
 '{"type":"openai","base_url":"http://100.82.225.102:8000/v1","model_name":"Qwen/Qwen3-4B-AWQ","api_key":"EMPTY","temperature":0.7,"max_tokens":2048}',
 100, 1, NOW(), 1, NOW());
