-- liquibase formatted sql
-- changeset xiaozhi:202608042000-1
-- OVS TTS 的音色字段改成远程下拉：点开即从设备实时拉取音色列表。
--
-- 之前声明的是 type:"number"，渲染成纯文本框，用户得自己知道音色 id 是几。
-- 而音色随 TTS 模型变（Kokoro 53 个、Qwen3 是 0/2301/2302、CustomVoice 是四位数、
-- matcha 只有一个 0），靠人记不现实。
--
-- remote-select 由 DynamicField.vue 渲染，选项来自 POST /models/probe 的
-- ovs_tts_speakers 探测，地址取自同一份配置里的 base_url（dependsOn）。
--
-- 之所以只改 OVS TTS 而不动 LLM 的 model_name：表单字段按 configJson.type 找供应商
-- （ModelEditDialog.vue:236），OVS TTS 的 type 是 openvoicestream_tts，对应
-- SYSTEM_TTS_OpenVoiceStream 这个我们自有的 provider，改它只影响我们自己；而
-- EdgeLLM 的 type 是 openai，与豆包/DeepSeek 等共用上游定义，不能碰。
--
-- sid 保持 number（旧协议，留空即可），不给它做下拉——避免用户在两个音色字段之间
-- 犹豫。速度/超时等保持原样。

UPDATE `ai_model_provider`
SET fields = '[{"key":"type","type":"string","label":"类型"},{"key":"base_url","type":"url","label":"基础URL"},{"key":"api_key","type":"password","label":"API Key(OVS_API_KEYS,可留空)"},{"key":"speaker_id","type":"remote-select","label":"音色","placeholder":"点开从设备拉取","optionsFrom":{"probe":"ovs_tts_speakers","dependsOn":"base_url","labelKey":"label","valueKey":"id"}},{"key":"sid","type":"number","label":"音色ID(旧协议,留空)"},{"key":"speed","type":"number","label":"语速"},{"key":"timeout","type":"number","label":"超时(秒)"}]'
WHERE id = 'SYSTEM_TTS_OpenVoiceStream';

-- ASR 的地址字段一并升级成 url 类型（带格式提示），其余保持不变。
UPDATE `ai_model_provider`
SET fields = '[{"key":"type","type":"string","label":"类型"},{"key":"ws_url","type":"url","label":"WS地址"},{"key":"api_key","type":"password","label":"API Key(OVS_API_KEYS,可留空)"},{"key":"sample_rate","type":"number","label":"采样率"},{"key":"language","type":"string","label":"语言"},{"key":"final_timeout","type":"number","label":"结束超时"},{"key":"fallback_to_partial","type":"boolean","label":"回退部分结果"},{"key":"allow_backend_endpoint","type":"boolean","label":"允许后端端点"}]'
WHERE id = 'SYSTEM_ASR_OpenVoiceStream';
