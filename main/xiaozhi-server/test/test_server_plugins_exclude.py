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
  (c) 不配置该键 → 行为与原来一致（get_lunar 仍在）。
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


if __name__ == "__main__":
    test_a_excludes_get_lunar()
    test_b_cannot_exclude_exit_intent()
    test_c_default_keeps_everything()
    print("\n全部通过 ✓")
