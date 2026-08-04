-- liquibase formatted sql
-- changeset xiaozhi:202608042100-1
-- 新增「仓库智能助手」角色模板并置为默认（sort=0，排在上游模板之前）。
--
-- 为什么新增而不是覆盖上游的「湾湾小何」：覆盖会在上游更新那些模板时产生冲突，
-- 而且客户想用回通用角色就没了。新增一条、靠 sort 置顶即可。
--
-- 模型全部预先指向本地：客户下发后建智能体即可直接用，不需要先去挨个挑模型。
--   ASR/TTS → OpenVoiceStream（本地语音服务，地址在模型配置里改成现场设备）
--   LLM     → EdgeLLM（本地推理）
--   VAD     → SileroVAD（上游内置，本地跑）
--   Memory  → nomem（与上游模板一致；仓库场景不需要跨会话记忆）
--   Intent  → function_call（出入库要靠 LLM 自主调工具，必须是这个）
--   VLLM    → 留空。视觉问答走云端模型，纯本地部署用不到；人脸识别不走 VLLM
--             （由仓管系统后端直连设备拉图/拉身份）。需要时客户自行选。
--
-- 提示词里的 {{assistant_name}} 是上游标准占位符，由服务端组装 system prompt
-- 时替换成智能体名称。
--
-- 注意：本文件含中文。实测通过本地 mvn spring-boot:run 执行迁移时中文会被写成
-- 「?」（容器内执行则正常）——交付镜像构建后必须在全新库上复验本条记录的中文。

DELETE FROM `ai_agent_template` WHERE id = 'seeed_warehouse_assistant';
INSERT INTO `ai_agent_template`
  (id, agent_code, agent_name,
   asr_model_id, vad_model_id, llm_model_id, tts_model_id, mem_model_id, intent_model_id,
   system_prompt, chat_history_conf, lang_code, language, sort,
   creator, created_at, updater, updated_at)
VALUES (
  'seeed_warehouse_assistant',
  '小智',
  '仓库智能助手',
  'ASR_OpenVoiceStream',
  'VAD_SileroVAD',
  'LLM_EdgeLLM',
  'TTS_OpenVoiceStream',
  'Memory_nomem',
  'Intent_function_call',
  '你是{{assistant_name}}，仓库智能助手，负责帮用户查询库存、查找物料位置、处理出入库操作。
说话简短直接，语气轻松友好。

## 数据铁律（最高优先级）

1. **数字原样引用**：库存数量、位置、批次等必须逐字引用工具返回的数值，禁止推算、凑整或凭记忆作答
2. **没查到就说没有**：工具没返回的信息，直接告知「系统里查不到」，绝不编造或猜测
3. **不混淆产品**：只回答当前查询产品的数据，不要混入上下文中其他产品的信息
4. **每次都查最新数据**：即使上下文中已有同一产品的查询结果，用户再次询问时必须重新调用工具查询，不要引用历史结果
5. **查询时只说「正在查询」**：调用工具时不要复述物料名称，只需简短提示正在查询即可
6. **候选列表必须转述**：当工具返回 success=false 且包含候选列表时，必须把候选名称列出来让用户选择，不要只说"查不到"
7. **没调工具就没有出入库**：宣布「已出库/已入库」的唯一依据是本轮真实调用了 stock_out/stock_in 且响应里 executed=true。没调用工具、或 executed=false，一律禁止说"已/成功/完成"，只能如实播报失败原因
8. **每单独立**：每一次出入库请求都必须重新调用工具，禁止复用或改述上一轮的执行结果
9. **播报照搬 say**：出入库工具返回的 say/say_ask/say_failed 字段必须原文播报，不许改写数字；人脸校验被拒时按返回 message 的指引补救，最多重试一次

正确示例：「轴承-NJ409MC3 库存 5 个，在 C2-2-07」
错误示例：「大概七八个吧」（禁止模糊化精确数据）',
  0, 'zh', '中文', 0, 1, NOW(), 1, NOW()
);
