# -*- coding: utf-8 -*-
"""server_plugins_exclude 单测（本地不连服务器）。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_server_plugins_exclude.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_server_plugins_exclude.py

覆盖三例：
  (a) exclude=[get_lunar] → 工具列表不含 get_lunar，仍含 handle_exit_intent；
  (b) exclude 含 handle_exit_intent → 被忽略（仍在列表里）并打 WARNING；
  (c) 不配置该键 → 行为与原来一致（get_lunar 仍在）；
  (d) console（manager-api）配置 + 本地 .config.yaml 含本地权威键 →
      合并后这些键对运行期可见，且 manager 管的 ASR/TTS/LLM 段不被本地覆盖。
"""
import os
import sys


# ---------------------------------------------------------------------------
# 运行环境自举（各单测文件里逐字相同）
# ---------------------------------------------------------------------------
def _bootstrap():
    import importlib
    from types import SimpleNamespace

    roots = []
    if "__file__" in globals():
        roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cwd = os.getcwd()
    roots += [cwd, os.path.join(cwd, "main", "xiaozhi-server")]
    for root in reversed(roots):
        if os.path.isdir(os.path.join(root, "core")) and root not in sys.path:
            sys.path.insert(0, root)
    for mod in ("av", "opuslib_next"):
        try:
            importlib.import_module(mod)
        except ImportError:
            sys.modules[mod] = SimpleNamespace()


_bootstrap()

from plugins_func.loadplugins import auto_import_modules  # noqa: E402
from core.providers.tools.server_plugins.plugin_executor import (  # noqa: E402
    ServerPluginExecutor,
)
from plugins_func.register import all_function_registry  # noqa: E402
from config.local_overrides import (  # noqa: E402
    LOCAL_AUTHORITATIVE_KEYS,
    apply_local_overrides,
)

auto_import_modules("plugins_func.functions")  # 注册所有服务端插件
from loguru import logger as loguru_logger  # noqa: E402

captured = []
loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")


class FakeConn:
    def __init__(self, config):
        self.config = config


def make_executor(exclude=None):
    config = {
        "selected_module": {"Intent": "function_call"},
        "Intent": {"function_call": {"functions": []}},
    }
    if exclude is not None:
        config["server_plugins_exclude"] = exclude
    return ServerPluginExecutor(FakeConn(config))


def test_a_excludes_get_lunar():
    assert "get_lunar" in all_function_registry, "前提：get_lunar 已注册"
    tools = make_executor(["get_lunar"]).get_tools()
    assert "get_lunar" not in tools, sorted(tools)
    assert "handle_exit_intent" in tools, sorted(tools)
    print("(a) exclude=[get_lunar] → 列表里没有 get_lunar  ✓")


def test_b_cannot_exclude_exit_intent():
    captured.clear()
    tools = make_executor(["handle_exit_intent"]).get_tools()
    assert "handle_exit_intent" in tools, "退出意图不允许被排除"
    assert any("handle_exit_intent" in m for m in captured), captured
    print("(b) 排除 handle_exit_intent 被忽略并打 WARNING  ✓")


def test_c_default_keeps_everything():
    tools = make_executor().get_tools()
    assert "get_lunar" in tools, sorted(tools)
    assert "handle_exit_intent" in tools, sorted(tools)
    print("(c) 不配置该键 → 行为与原来一致  ✓")



def test_d_local_authoritative_keys_merge():
    """console 模式：manager 拉来的配置里没有这些键，本地文件说了算。"""
    local_only = {
        "server_plugins_exclude": ["get_lunar"],
        "mcp_tool_call_timeout_sec": 5,
        "asr_listen_timeout_quiet_sec": 2.5,
        "asr_listen_timeout_max_sec": 45,
        "asr_empty_min_frames": 12,
        "llm_prefix_warmup_enabled": False,
        "llm_prefix_warmup_debounce_sec": 0.8,
    }
    for key in local_only:
        assert key in LOCAL_AUTHORITATIVE_KEYS, f"{key} 不在白名单里"

    from_console = {
        "selected_module": {"LLM": "EdgeLLM"},
        "LLM": {"EdgeLLM": {"base_url": "http://console/v1"}},
        "ASR": {"OVA": {"base_url": "http://console/asr"}},
        "TTS": {"OVS": {"base_url": "http://console/tts"}},
    }
    local = dict(local_only)
    # manager 管的段即使写在本地文件里也不该被合并进去
    local["LLM"] = {"EdgeLLM": {"base_url": "http://local/v1"}}
    local["ASR"] = {"OVA": {"base_url": "http://local/asr"}}

    merged = apply_local_overrides(from_console, local)

    for key, value in local_only.items():
        assert merged.get(key) == value, (key, merged.get(key))
    assert merged["LLM"]["EdgeLLM"]["base_url"] == "http://console/v1", merged["LLM"]
    assert merged["ASR"]["OVA"]["base_url"] == "http://console/asr", merged["ASR"]
    for seg in ("ASR", "TTS", "LLM", "selected_module"):
        assert seg not in LOCAL_AUTHORITATIVE_KEYS, seg
    print("(d) 本地权威键合并可见，ASR/TTS/LLM 仍由 console 说了算  ✓")


if __name__ == "__main__":
    test_a_excludes_get_lunar()
    test_b_cannot_exclude_exit_intent()
    test_c_default_keeps_everything()
    test_d_local_authoritative_keys_merge()
    print("\n全部通过 ✓")
