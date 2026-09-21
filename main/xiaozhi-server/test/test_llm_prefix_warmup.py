# -*- coding: utf-8 -*-
"""LLM 前缀预热单测（本地不连服务器）。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_llm_prefix_warmup.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_llm_prefix_warmup.py

覆盖：
  (a) 预热构造的 messages 只含静态 system + few-shot + 动态 system，
      不含 dialogue 里的真实用户/助手消息；调用后 dialogue 长度不变、
      TTS 队列为空；请求带 max_tokens=1；
  (b) 1.5s 去抖窗口内连续 3 次工具变化只真正预热 1 次；
  (c) llm_prefix_warmup_enabled=false 时既不调度也不发请求；
  (d) 真实对话进行中（_llm_chat_active>0）跳过预热；
  (e) depth 0 与 depth>0 的 functions 前缀指纹相同，且与预热用的相同
      （direct_answer 所有深度都注入，工具列表跨轮逐字稳定）；
  (f) 前缀变化日志带 added=/removed= 工具名差集；
  (g) 锁忙而跳过的那次会被补一次（pending → 再调度一次）；
  (h) stop_event 已置位时直接返回，不发请求。
"""
import asyncio
import os
import queue
import sys
import threading
import time


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

from core import connection as conn_mod  # noqa: E402
from core.connection import ConnectionHandler  # noqa: E402
from core.utils.dialogue import Dialogue, Message  # noqa: E402
from core.providers.llm.telemetry import compute_prefix_md5  # noqa: E402
from config.logger import setup_logging  # noqa: E402
from loguru import logger as loguru_logger  # noqa: E402

# import 完成后（模块内 setup_logging 已跑）再挂捕获 sink
captured = []
loguru_logger.add(lambda m: captured.append(str(m)), level="INFO")

REAL_USER_TEXT = "10-8四通还有多少"
REAL_ASSISTANT_TEXT = "还有 12 个。"
FEWSHOT_USER_TEXT = "给我讲个故事吧"


class FakeLLM:
    def __init__(self):
        self.calls = []

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        self.calls.append(
            {"dialogue": list(dialogue), "functions": functions, "kwargs": dict(kwargs)}
        )
        yield ("", None)

    def response(self, session_id, dialogue, **kwargs):
        self.calls.append({"dialogue": list(dialogue), "functions": None, "kwargs": dict(kwargs)})
        yield ""


class FakeToolHandler:
    def __init__(self, tools):
        self._tools = tools

    def get_functions(self):
        return list(self._tools)


class FakeTTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()


TOOLS = [
    {"type": "function", "function": {"name": "query_stock", "description": "查库存"}},
]


def make_conn(config=None, llm=None):
    """绕过 __init__ 构造最小可用的 ConnectionHandler。"""
    conn = object.__new__(ConnectionHandler)
    conn.config = {"llm_prefix_warmup_debounce_sec": 1.5} if config is None else config
    conn.logger = setup_logging()
    conn.session_id = "test-session"
    conn.dialogue = Dialogue()
    conn.dialogue.dialogue.append(
        Message(role="system", content="你是小智。<context>现在 {{current_time}}</context>")
    )
    # few-shot（is_temporary）——属于前缀，预热必须带上
    conn.dialogue.put(Message(role="user", content=FEWSHOT_USER_TEXT, is_temporary=True))
    conn.dialogue.put(Message(role="assistant", content="好呀~", is_temporary=True))
    # 真实历史——预热必须不带
    conn.dialogue.put(Message(role="user", content=REAL_USER_TEXT))
    conn.dialogue.put(Message(role="assistant", content=REAL_ASSISTANT_TEXT))
    conn.tts = FakeTTS()
    conn.stop_event = threading.Event()
    conn.intent_type = "function_call"
    conn.func_handler = FakeToolHandler(TOOLS)
    conn.llm = llm if llm is not None else FakeLLM()
    conn.loop = None
    conn._llm_prefix_warmup_task = None
    conn._llm_prefix_warmup_lock = threading.Lock()
    conn._llm_prefix_warmup_pending = False
    conn._llm_chat_active = 0
    conn._llm_prefix_last_md5 = None
    conn._llm_prefix_last_tools = None
    conn._llm_prefix_last_tool_names = None
    return conn


def test_a_warmup_messages_exclude_real_history():
    conn = make_conn()
    before = len(conn.dialogue.dialogue)

    conn._warm_llm_prefix("connection_ready")

    assert len(conn.llm.calls) == 1, conn.llm.calls
    call = conn.llm.calls[0]
    texts = "".join(str(m.get("content") or "") for m in call["dialogue"])
    assert REAL_USER_TEXT not in texts, call["dialogue"]
    assert REAL_ASSISTANT_TEXT not in texts, call["dialogue"]
    assert FEWSHOT_USER_TEXT in texts, "few-shot 属于前缀，必须带上"
    assert call["dialogue"][0]["role"] == "system"
    assert call["dialogue"][-1] == {"role": "user", "content": "。"}
    assert call["kwargs"].get("max_tokens") == 1, call["kwargs"]
    # direct_answer 虚拟工具与 chat() 一致地拼在末尾
    names = [f["function"]["name"] for f in call["functions"]]
    assert names == ["query_stock", "direct_answer"], names

    assert len(conn.dialogue.dialogue) == before, "预热不得写入对话历史"
    assert conn.tts.tts_text_queue.empty(), "预热不得进 TTS"
    print("(a) 预热 messages 不含真实历史、不写 dialogue、不进 TTS  ✓")


def test_b_debounce_collapses_three_changes():
    conn = make_conn()
    fired = []
    conn._warm_llm_prefix = lambda reason: fired.append((reason, time.monotonic()))

    async def main():
        conn.loop = asyncio.get_running_loop()
        for _ in range(3):
            conn._schedule_llm_prefix_warmup("tools_changed")
            await asyncio.sleep(0.3)  # 三次变化都落在 1.5s 去抖窗口内
        await asyncio.sleep(2.0)

    started = time.monotonic()
    asyncio.run(main())
    assert len(fired) == 1, fired
    # 最后一次变化在 t=0.6s，去抖 1.5s → 真正发出不早于 t=2.1s
    assert fired[0][1] - started >= 0.6 + 1.5 - 0.05, fired
    print("(b) 1.5s 内 3 次工具变化只预热 1 次  ✓")


def test_c_disabled_does_nothing():
    conn = make_conn(config={"llm_prefix_warmup_enabled": False})
    fired = []

    async def main():
        conn.loop = asyncio.get_running_loop()
        conn._schedule_llm_prefix_warmup("tools_changed")
        await asyncio.sleep(0.2)

    original = conn._warm_llm_prefix
    conn._warm_llm_prefix = lambda reason: fired.append(reason)
    asyncio.run(main())
    conn._warm_llm_prefix = original

    assert fired == [], fired
    assert conn._llm_prefix_warmup_task is None
    conn._warm_llm_prefix("connection_ready")  # 直接调也要拒绝
    assert conn.llm.calls == [], conn.llm.calls
    print("(c) llm_prefix_warmup_enabled=false 不触发  ✓")


def test_d_skip_while_chat_active():
    conn = make_conn()
    conn._llm_chat_active = 1
    conn._warm_llm_prefix("tools_changed")
    assert conn.llm.calls == [], conn.llm.calls
    conn._llm_chat_active = 0
    conn._warm_llm_prefix("tools_changed")
    assert len(conn.llm.calls) == 1, conn.llm.calls
    print("(d) 真实对话进行中跳过预热  ✓")


def test_e_functions_identical_across_depths():
    """工具列表跨深度逐字一致 —— EdgeLLM 前缀缓存的命中条件。

    现场日志（2026-09-18 11:10）：depth 0 tools=29 ttft=0.55s，工具结果那轮
    tools=28 ttft=2.37s，下一轮又变回 29 —— direct_answer 只在 depth==0 注入，
    每一轮都把前缀打翻。
    """
    conn = make_conn()
    f0 = conn._build_llm_functions(force_final_answer=False)
    f1 = conn._build_llm_functions(force_final_answer=False)  # depth>0 同一构造
    assert f0 == f1, (f0, f1)
    names = [f["function"]["name"] for f in f0]
    assert names == ["query_stock", "direct_answer"], names

    messages = [{"role": "system", "content": "你是小智。"}]
    md5_0 = compute_prefix_md5(messages, f0)
    md5_1 = compute_prefix_md5(messages, f1)
    assert md5_0 == md5_1, (md5_0, md5_1)

    # 预热走的是同一个 _build_llm_functions，指纹必须对得上
    conn._warm_llm_prefix("connection_ready")
    warm_functions = conn.llm.calls[-1]["functions"]
    assert compute_prefix_md5(messages, warm_functions) == md5_0

    # 撞到 MAX_DEPTH 时禁用工具，行为与原来一致
    assert conn._build_llm_functions(force_final_answer=True) is None
    print("(e) depth 0/1 与预热的 functions 指纹一致  ✓")


def test_f_prefix_change_logs_tool_name_diff():
    conn = make_conn()
    messages = [{"role": "system", "content": "你是小智。"}]
    extra = {"type": "function", "function": {"name": "query_price", "description": "查价"}}

    captured.clear()
    conn._note_llm_prefix(messages, conn._build_llm_functions())
    assert not any("LLM prefix changed" in m for m in captured), "首次不该报变化"

    conn.func_handler = FakeToolHandler(TOOLS + [extra])
    conn._note_llm_prefix(messages, conn._build_llm_functions())
    line = [m for m in captured if "LLM prefix changed" in m]
    assert len(line) == 1, captured
    assert "added=['query_price']" in line[0], line[0]
    assert "removed=[]" in line[0], line[0]

    captured.clear()
    conn.func_handler = FakeToolHandler(TOOLS)
    conn._note_llm_prefix(messages, conn._build_llm_functions())
    line = [m for m in captured if "LLM prefix changed" in m]
    assert len(line) == 1, captured
    assert "added=[]" in line[0], line[0]
    assert "removed=['query_price']" in line[0], line[0]
    print("(f) 前缀变化日志带工具名 diff  ✓")



def test_g_skipped_warmup_is_retried():
    """锁忙时跳过的那次带着新工具列表，必须在当前这次跑完后补上。"""
    conn = make_conn()
    scheduled = []
    conn._schedule_llm_prefix_warmup = lambda reason: scheduled.append(reason)

    # 另一个线程占着锁 → 这次被跳过，只留标记
    conn._llm_prefix_warmup_lock.acquire()
    try:
        conn._warm_llm_prefix("tools_changed")
        assert conn.llm.calls == [], conn.llm.calls
        assert conn._llm_prefix_warmup_pending is True
        assert scheduled == [], scheduled
    finally:
        conn._llm_prefix_warmup_lock.release()

    # 锁空了：这次真正跑，跑完把挂起的那次补回去
    conn._warm_llm_prefix("connection_ready")
    assert len(conn.llm.calls) == 1, conn.llm.calls
    assert scheduled == ["pending"], scheduled
    assert conn._llm_prefix_warmup_pending is False
    assert not conn._llm_prefix_warmup_lock.locked(), "锁没放回去"

    # 连接已关闭时不再补
    conn._llm_prefix_warmup_pending = True
    conn.stop_event.set()
    scheduled.clear()
    conn._warm_llm_prefix("connection_ready")
    assert scheduled == [], scheduled
    print("(g) 锁忙跳过的预热在当前这次结束后补一次  ✓")


def test_h_stop_event_short_circuits():
    conn = make_conn()
    conn.stop_event.set()
    conn._warm_llm_prefix("connection_ready")
    assert conn.llm.calls == [], conn.llm.calls
    assert conn._llm_prefix_warmup_pending is False
    assert not conn._llm_prefix_warmup_lock.locked()
    print("(h) stop_event 已置位 → 预热直接返回  ✓")


if __name__ == "__main__":
    test_a_warmup_messages_exclude_real_history()
    test_b_debounce_collapses_three_changes()
    test_c_disabled_does_nothing()
    test_d_skip_while_chat_active()
    test_e_functions_identical_across_depths()
    test_f_prefix_change_logs_tool_name_diff()
    test_g_skipped_warmup_is_retried()
    test_h_stop_event_short_circuits()
    print("\n全部通过 ✓")
