# -*- coding: utf-8 -*-
"""console（manager-api）模式下的本地权威配置键。

`read_config_from_api: true` 时 `config_loader.get_config_from_api_async()` 整份
配置都来自 Java manager-api 的数据库，`data/.config.yaml` 里的顶层键除了
`server` / `manager-api` / `prompt_template` 一律被丢掉。manager 的表里没有我们
自己加的键，所以要在拉完配置后把它们合并回来。

允许合并的键写在 `LOCAL_AUTHORITATIVE_KEYS` 里 —— 这是白名单，不是"整份覆盖"：
把 ASR/TTS/LLM 之类 manager 已经在管的段落放进来会让网页上的改动失效。
"""

# 白名单：只列 manager 不认识、必须由本地文件说了算的顶层键。
# 仍然不含 ASR/TTS/LLM/selected_module —— 那几段由 manager 管着，放进来会让
# 网页上的改动失效。
LOCAL_AUTHORITATIVE_KEYS = (
    "server_plugins_exclude",
    # 工具调用
    "mcp_tool_call_timeout_sec",
    # ASR 监听窗口与空音频判定
    "asr_listen_timeout_quiet_sec",
    "asr_listen_timeout_max_sec",
    "asr_empty_min_frames",
    # LLM 前缀预热
    "llm_prefix_warmup_enabled",
    "llm_prefix_warmup_debounce_sec",
)


def apply_local_overrides(config_data, local_config):
    """把本地权威键从 `data/.config.yaml` 合并进 manager 拉来的配置（就地修改）。"""
    if not isinstance(config_data, dict) or not isinstance(local_config, dict):
        return config_data
    for key in LOCAL_AUTHORITATIVE_KEYS:
        if key in local_config:
            config_data[key] = local_config[key]
    return config_data
