# -*- coding: utf-8 -*-
"""MCP 接入点工具调用超时单测（本地不连服务器）。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_mcp_tool_timeout.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_mcp_tool_timeout.py

覆盖：
  (a) future 永不完成 → 到点返回含 tool_timeout 的 JSON 字符串，耗时≈超时值，
      且日志里有带工具名与秒数的 WARNING；
  (b) 配置 mcp_tool_call_timeout_sec: 2 经执行器生效（不是写死的默认值）；
  (c) 正常完成不受影响，返回原文本、不产生超时结果；
  (d) 写操作（及判断不了的）超时 → executed="unknown" + 核对话术；
  (e) 只读操作超时 → executed=False + 原查询话术。
"""
import asyncio
import json
import os
import sys
import time


# ---------------------------------------------------------------------------
# 运行环境自举（三个单测文件里逐字相同）
#   - 兼容 cwd：仓库根 / main/xiaozhi-server / 容器内 /opt/xiaozhi-esp32-server
#   - 兼容两种跑法：pytest（有 __file__）与容器 stdin（无 __file__）
#   - 本地开发环境可能缺 av / opuslib_next 等重依赖，打桩；容器内有真实模块，桩不生效
# 注意：stdin 跑法不会加载 conftest.py，所以这段必须内联在每个文件里。
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

from core.providers.tools.mcp_endpoint import mcp_endpoint_handler as H  # noqa: E402
from core.providers.tools.mcp_endpoint.mcp_endpoint_executor import (  # noqa: E402
    MCPEndpointExecutor,
)
from plugins_func.register import Action  # noqa: E402
from loguru import logger as loguru_logger  # noqa: E402

# import 完成后（模块内 setup_logging 已跑）再挂捕获 sink
captured = []
loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")

TOOL = "query_stock"


class FakeClient:
    """只实现 call_mcp_endpoint_tool 用到的那几个方法。"""

    def __init__(self, reply=None):
        self.reply = reply
        self.name_mapping = {}
        self.sent = []
        self.cleaned = []
        self._future = None

    async def is_ready(self):
        return True

    def has_tool(self, name):
        return name == TOOL

    def get_tool_original_name(self, name):
        return name

    async def get_next_id(self):
        return 42

    async def register_call_result_future(self, call_id, future):
        self._future = future

    async def send_message(self, message):
        self.sent.append(message)
        # reply=None 表示后端永远不回，future 挂着直到超时
        if self.reply is not None:
            self._future.set_result(self.reply)

    async def cleanup_call_result(self, call_id):
        self.cleaned.append(call_id)


class FakeConn:
    def __init__(self, config, client):
        self.config = config
        self.mcp_endpoint_client = client


def _run(coro):
    return asyncio.run(coro)


# ── (a) 超时返回可播报结果 ──

def test_a_timeout_returns_speakable_result():
    captured.clear()
    client = FakeClient(reply=None)

    async def go():
        t0 = time.monotonic()
        result = await H.call_mcp_endpoint_tool(client, TOOL, "{}", timeout=1)
        return result, time.monotonic() - t0

    result, elapsed = _run(go())

    assert isinstance(result, str), type(result)
    payload = json.loads(result)
    assert payload["error"] == "tool_timeout", payload
    assert payload["ok"] is False and payload["executed"] is False
    assert payload["say"] == "查询超时，请稍后再试"
    assert payload["say_kind"] == "tell"
    # 耗时≈超时值：不早退、也不叠加
    assert 0.9 <= elapsed < 2.0, elapsed
    # 清理了挂起的 future，避免泄漏
    assert client.cleaned == [42], client.cleaned
    # WARNING 带工具名与秒数
    warns = [m for m in captured if TOOL in m and "超时" in m]
    assert warns, captured
    assert "1秒" in warns[0], warns[0]


# ── (b) 配置生效 ──

def test_b_config_timeout_applies():
    client = FakeClient(reply=None)
    conn = FakeConn({"mcp_tool_call_timeout_sec": 2}, client)

    async def go():
        t0 = time.monotonic()
        resp = await MCPEndpointExecutor(conn).execute(conn, TOOL, {})
        return resp, time.monotonic() - t0

    resp, elapsed = _run(go())
    assert 1.9 <= elapsed < 3.0, elapsed
    assert resp.action == Action.REQLLM, resp.action
    assert json.loads(resp.result)["error"] == "tool_timeout", resp.result


def test_b2_default_is_8_seconds():
    assert H.DEFAULT_MCP_TOOL_CALL_TIMEOUT == 8
    # 未配置时执行器取默认值，函数签名默认值也是同一个常量
    import inspect

    sig = inspect.signature(H.call_mcp_endpoint_tool)
    assert sig.parameters["timeout"].default == 8


def test_b3_config_yaml_ships_the_key():
    """仓库里的 config.yaml 必须带上这个键（它是这个默认值的唯一文档）。

    只在有 ``__file__`` 时检查：容器 stdin 跑法拿不到仓库路径，而镜像里的
    ``config.yaml`` 是现场部署变体（与仓库版本本就不同，thin 镜像不覆盖它），
    拿它来断言等于拿现场配置卡仓库的默认值。运行期不依赖这个键——
    没配就用 DEFAULT_MCP_TOOL_CALL_TIMEOUT。
    """
    # stdin 跑法下 __file__ 是 "<stdin>"（不是「没有 __file__」，踩过），
    # 只有真正指向本文件的 .py 路径才能推出仓库根。
    here = globals().get("__file__", "")
    if not here.endswith(".py") or not os.path.isfile(here):
        print("skip test_b3: stdin 跑法没有仓库路径")
        return

    import yaml

    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(here))), "config.yaml"
    )
    if not os.path.isfile(cfg_path):
        print("skip test_b3: 找不到仓库 config.yaml")
        return
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    assert cfg.get("mcp_tool_call_timeout_sec") == 8, cfg.get("mcp_tool_call_timeout_sec")


# ── (c) 正常完成不受影响 ──

def test_c_normal_call_unaffected():
    captured.clear()
    reply = {"content": [{"type": "text", "text": "指环刀当前库存137件"}]}
    client = FakeClient(reply=reply)

    async def go():
        t0 = time.monotonic()
        result = await H.call_mcp_endpoint_tool(client, TOOL, "{}", timeout=8)
        return result, time.monotonic() - t0

    result, elapsed = _run(go())
    assert result == "指环刀当前库存137件", result
    assert elapsed < 1.0, elapsed
    assert client.cleaned == [], client.cleaned
    assert not [m for m in captured if "tool_timeout" in m]


def test_c2_normal_call_through_executor():
    client = FakeClient(reply={"content": [{"type": "text", "text": "库存137件"}]})
    conn = FakeConn({}, client)
    resp = _run(MCPEndpointExecutor(conn).execute(conn, TOOL, {}))
    assert resp.action == Action.REQLLM, resp.action
    assert resp.result == "库存137件", resp.result



# ── (d/e) 超时的 executed 语义按工具名分类 ──

class AnyToolClient(FakeClient):
    """has_tool 对任何名字都返回 True，用来跑不同工具名的超时分支。"""

    def has_tool(self, name):
        return True


def test_d_write_tool_timeout_is_unknown():
    """写操作超时：远端可能已经执行，不能告诉用户「没执行」。"""
    captured.clear()
    for name in ("stock_in", "stock_out", "move_item", "adjust_qty", "wms_unknown_op"):
        client = AnyToolClient(reply=None)
        result = _run(H.call_mcp_endpoint_tool(client, name, "{}", timeout=1))
        payload = json.loads(result)
        assert payload["executed"] == "unknown", (name, payload)
        assert payload["ok"] is False, (name, payload)
        assert payload["error"] == "tool_timeout", (name, payload)
        assert payload["notice"] == "执行状态未知", (name, payload)
        assert payload["say"] == (
            "操作超时，执行结果未知，请先到系统里核对库存，再决定是否重试"
        ), (name, payload)
        assert payload["say_kind"] == "tell", (name, payload)
    warns = [m for m in captured if "stock_in" in m]
    assert warns, captured
    assert "写操作" in warns[0], warns[0]
    print("(d) 写操作/无法判断的工具超时 → executed=unknown  ✓")


def test_e_read_tool_timeout_stays_false():
    """只读操作超时：重试无副作用，executed=False 是安全的说法。"""
    captured.clear()
    for name in ("query_stock", "search_item", "resolve_sku", "get_location"):
        client = AnyToolClient(reply=None)
        result = _run(H.call_mcp_endpoint_tool(client, name, "{}", timeout=1))
        payload = json.loads(result)
        assert payload["executed"] is False, (name, payload)
        assert payload["say"] == "查询超时，请稍后再试", (name, payload)
        assert "notice" not in payload, (name, payload)
    warns = [m for m in captured if "query_stock" in m]
    assert warns, captured
    assert "只读操作" in warns[0], warns[0]
    print("(e) 只读工具超时 → executed=False，话术不变  ✓")


if __name__ == "__main__":
    test_a_timeout_returns_speakable_result()
    test_b_config_timeout_applies()
    test_b2_default_is_8_seconds()
    test_b3_config_yaml_ships_the_key()
    test_c_normal_call_unaffected()
    test_c2_normal_call_through_executor()
    test_d_write_tool_timeout_is_unknown()
    test_e_read_tool_timeout_stays_false()
    print("\n全部通过 ✓")
