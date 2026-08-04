-- liquibase formatted sql
-- changeset xiaozhi:202608041530-1
-- 把本地模型（OpenVoiceStream ASR/TTS、EdgeLLM）排到各自列表最前面。
--
-- 背景：列表分页每页 10 条，排序是 ORDER BY is_enabled DESC, sort ASC
-- （ModelConfigServiceImpl.java:112）。上游预置模型的 sort 从 1 递增排到 14，
-- 我们原先给的 sort=100 把本地模型挤到了第二页 —— 客户装完打开「大语言模型」，
-- 第一屏全是智谱/豆包/DeepSeek/Gemini 这些用不上的云端模型，唯一该用的
-- EdgeLLM 反而看不见。ASR/TTS 同理。
--
-- 改成 0：上游 sort 从 1 开始，0 不与任何既有值冲突，且天然排最前。
-- 供应商列表（ai_model_provider）同样按 sort 升序（ModelProviderServiceImpl.java:98），
-- 一并调整。
--
-- 本 changeset 只调排序，不改启用状态 —— 云端模型是否禁用属于交付预设，另议。

UPDATE `ai_model_provider` SET sort = 0
  WHERE id IN ('SYSTEM_ASR_OpenVoiceStream', 'SYSTEM_TTS_OpenVoiceStream');

UPDATE `ai_model_config` SET sort = 0
  WHERE id IN ('ASR_OpenVoiceStream', 'TTS_OpenVoiceStream', 'LLM_EdgeLLM');
