-- liquibase formatted sql
-- changeset xiaozhi:202608041830-1
-- 默认开启 MCP 接入点入口（角色配置 →「编辑功能」弹窗里那块）。
--
-- 开关存在 sys_params 的 system-web.menu 这个 JSON 里，features.mcpAccessPoint.enabled
-- 上游默认是 false，前端 featureManager 据此隐藏整块 UI（FunctionDialog.vue:109
-- 的 v-if="featureStatus.mcpAccessPoint"）。
--
-- 我们的方案要靠 MCP 接入点把仓管系统的出入库工具挂到每个 agent 上，这是必备功能，
-- 不该让客户先去翻参数管理里的一段 JSON 才能看见入口。
--
-- 用 JSON_SET 定点改这一个布尔值，不整体覆盖 param_value —— 上游以后往这段 JSON 里
-- 加新特性时，整体覆盖会把它们一起抹掉。
--
-- 注意：开启的只是**入口可见**。接入点地址仍需在「参数管理 → server.mcp_endpoint」
-- 里配置（且该参数有硬校验：不能含 localhost/127.0.0.1，URL 里必须含 key），
-- 未配置时前端拿到的地址为空。

UPDATE `sys_params`
SET param_value = JSON_SET(param_value, '$.features.mcpAccessPoint.enabled', TRUE)
WHERE param_code = 'system-web.menu'
  AND JSON_VALID(param_value)
  AND JSON_EXTRACT(param_value, '$.features.mcpAccessPoint.enabled') IS NOT NULL;
