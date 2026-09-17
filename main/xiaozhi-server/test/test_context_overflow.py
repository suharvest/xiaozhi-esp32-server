# -*- coding: utf-8 -*-
"""A1 工具结果上下文压缩（保真窗口 2 轮）+ A2 上下文超限裁剪重试 + A4 兜底去重 单测。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_context_overflow.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_context_overflow.py

覆盖：
  (a) 3 轮历史（每轮一次受控长度 tool 结果）后：第 3 轮及更早（窗口外）的
      tool 内容被压缩到 tool_result_max_chars 内且含候选摘要；
  (b) 最近 2 轮（保真窗口内）的 tool 内容与原始一致（逐字保留）；
      另测窗口内超宽松上限时只剥离巨型数组、say 全文保留；
  (c) enqueue_tool_report 收到的仍是原始串（上报/记忆不受影响）；
  (d) input_too_long → 确定性裁剪重试一次成功，上下文不清空；
  (e) 重试仍 input_too_long → 清空会话上下文（保留 system）+ FIRST+LAST
      兜底播报 + 「上下文超限」WARNING；
  (f) 裁剪函数协议完整性：system 保留、最近 2 轮、仅最近一次工具交互、
      tool_calls/tool 配对着齐；
  (g) A4：8 秒内兜底话术不重复播。
"""
import asyncio
import json
import os
import queue
import sys
import threading
import uuid
from types import SimpleNamespace


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

import core.connection as conn_mod  # noqa: E402
from core.connection import ConnectionHandler  # noqa: E402
from core.providers.asr.base import ASRProviderBase  # noqa: E402
from core.providers.tts.dto.dto import SentenceType  # noqa: E402
from core.utils.dialogue import Dialogue  # noqa: E402
from plugins_func.register import Action, ActionResponse  # noqa: E402
from config.logger import setup_logging  # noqa: E402
from loguru import logger as loguru_logger  # noqa: E402

captured = []
loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")

OVERFLOW_ERR = (
    "Error code: 400 - {'code': 'input_too_long', 'message': 'Input (10042 tokens) "
    "exceeds maximum (7064 tokens after reserving completion/speculative capacity). "
    "Try shortening conversation history.', 'context': {'got_tokens': 10042, "
    "'max': 7064, 'max_context_tokens': 8192, 'requested_output_tokens': 100}}"
)


def make_tool_result(n_candidates=8, say="接头（BCL02）当前库存17.0件，位于A032"):
    """受控长度的 query_stock 返回（约 1.5KB，带 candidates 大数组）。"""
    cands = [
        {
            "id": f"P{i:03d}",
            "name": f"接头型号{i}",
            "type": "material",
            "score": 0.9 - i * 0.01,
            "extra": {
                "sku": f"P{i:03d}",
                "variant": f"10-8-{i}",
                "stock": 10 + i,
                "location": f"A0{i:02d}",
                "debug_blob": "X" * 100,
            },
        }
        for i in range(n_candidates)
    ]
    return json.dumps(
        {"ok": True, "say": say, "data": {"total": n_candidates}, "candidates": cands},
        ensure_ascii=False,
    )


class FakeTTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()
        self.stored = {}
        self.spoken = []

    def store_tts_text(self, sentence_id, text):
        self.stored[sentence_id] = text

    def tts_one_sentence(self, conn, content_type, content_detail=None, **kw):
        self.spoken.append(content_detail)


def drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


def make_conn(config=None, llm=None):
    """绕过 __init__ 构造最小可用的 ConnectionHandler。"""
    conn = object.__new__(ConnectionHandler)
    conn.config = config or {}
    conn.logger = setup_logging()
    conn.dialogue = Dialogue()
    conn.dialogue.dialogue.append(
        conn_mod.Message(role="system", content="你是小智。<context>{{current_time}}</context>")
    )
    conn.tts = FakeTTS()
    conn.client_abort = False
    conn.stop_event = threading.Event()
    conn.intent_type = "function_call"
    conn.llm = llm
    conn.memory = None
    conn.loop = None
    conn.features = {"emoji": False}
    conn.session_id = uuid.uuid4().hex
    conn.sentence_id = None
    conn.current_speaker = None
    conn.system_introduced_speakers = set()
    conn._pending_tool_answer = False
    conn._last_tool_result_text = None
    return conn


def tool_call_chunk(call_id, name, args):
    return SimpleNamespace(
        index=None, id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


class ScriptedLLM:
    """calls: list of ('raise', err) | ('chunks', [chunk,...])"""

    def __init__(self, calls):
        self.calls = list(calls)
        self.seen_messages = []

    def _next(self, messages):
        self.seen_messages.append(messages)
        kind, payload = self.calls.pop(0)
        if kind == "raise":
            raise Exception(payload)
        return iter(payload)

    def response_with_functions(self, session_id, messages, functions=None):
        return self._next(messages)

    def response(self, session_id, messages):
        return self._next(messages)


class FakeFuncHandler:
    def __init__(self, results):
        self.results = list(results)

    def get_functions(self):
        return []

    async def handle_llm_function_call(self, conn, tool_call_data):
        return ActionResponse(action=Action.REQLLM, result=self.results.pop(0))


def run_chat_round(conn, query, tool_raw):
    """跑一轮完整 chat()：模型调 query_stock → 工具返回 tool_raw → 模型回答。"""
    conn.func_handler = FakeFuncHandler([tool_raw])
    conn.llm = ScriptedLLM([
        ("chunks", [(None, [tool_call_chunk(f"call_{uuid.uuid4().hex[:8]}", "query_stock", {"product_name": query})])]),
        ("chunks", [("好的，已查到。", None)]),
    ])
    conn.chat(query)
    drain(conn.tts.tts_text_queue)


def tool_messages(conn):
    return [m for m in conn.dialogue.dialogue if m.role == "tool"]


def test_abc_window_and_report():
    """(a)(b)(c)：3 轮 tool 结果，窗口外压缩、窗口内逐字、上报原始。"""
    reports = []
    orig_enqueue = conn_mod.enqueue_tool_report
    conn_mod.enqueue_tool_report = lambda *a, **k: reports.append(a)

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        conn = make_conn()
        conn.loop = loop
        raws = [make_tool_result(say=f"第{i}轮的查询结果话术") for i in range(1, 4)]
        for i, raw in enumerate(raws, 1):
            run_chat_round(conn, f"查一下库存{i}", raw)

        tools = tool_messages(conn)
        assert len(tools) == 3, f"应有3条tool消息: {len(tools)}"

        # (c) 上报路径收到原始串
        reported_results = [
            a[3] for a in reports if len(a) >= 4 and a[3] is not None
        ]
        for raw in raws:
            assert raw in reported_results, "enqueue_tool_report 未收到原始工具结果"
        print("(c) enqueue_tool_report 收到原始工具结果（上报/记忆不受影响）  ✓")

        # (b) 保真窗口 = 最近 2 轮：第 2、3 轮 tool 内容逐字保留
        assert tools[1].content == raws[1], "窗口内第2轮 tool 内容被改动"
        assert tools[2].content == raws[2], "窗口内第3轮 tool 内容被改动"
        print("(b) 最近 2 轮（保真窗口内）tool 内容逐字保留  ✓")

        # (a) 窗口外（第 1 轮）被压缩到上限内且含候选摘要（名字+库存+库位）
        c1 = tools[0].content
        max_chars = int(conn.config.get("tool_result_max_chars", 600))
        assert len(c1) <= max_chars + 32, f"窗口外历史未压到上限内: {len(c1)}"
        parsed = json.loads(c1.split("…[已截断")[0].rstrip(","))
        assert parsed.get("ok") is True and parsed.get("say") == "第1轮的查询结果话术", \
            f"短字段丢失: {c1[:200]}"
        top = parsed["candidates"]["top"]
        assert parsed["candidates"]["total"] == 8 and len(top) <= 5, "候选摘要条数不对"
        assert all("name" in it and "stock" in it and "location" in it for it in top), \
            f"候选摘要缺 名字/库存/库位: {top}"
        print("(a) 窗口外历史 tool 结果压缩到上限内且含候选摘要  ✓")
    finally:
        conn_mod.enqueue_tool_report = orig_enqueue
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)


def test_b2_latest_cap_strips_only_arrays():
    """(b 补充) 窗口内超 tool_result_latest_max_chars：只剥离巨型数组，say 全文保留。"""
    conn = make_conn({"tool_result_latest_max_chars": 400})
    big_say = "入库成功，这是一段必须逐字保留的话术。" * 10
    raw = json.dumps(
        {"ok": True, "say": big_say,
         "candidates": [{"name": f"物料{i}", "blob": "Y" * 200} for i in range(20)]},
        ensure_ascii=False,
    )
    assert len(raw) > 400
    out = conn._cap_latest_tool_result_for_context(raw, "query_stock")
    assert "已省略巨型数组" in out, f"巨型数组未被剥离: {out[:120]}"
    parsed = json.loads(out.split("…[已截断")[0].rstrip(",")) if "…[已截断" in out else json.loads(out)
    assert parsed["say"] == big_say, "say 未逐字保留"
    # 未超限 → 完全原样（用默认 4000 上限的连接）
    small = make_tool_result()
    conn_default = make_conn()
    assert conn_default._cap_latest_tool_result_for_context(small, "query_stock") == small
    # 写操作历史压缩：say 仍逐字
    hist = conn._compress_tool_result_for_context(
        json.dumps({"ok": True, "say": big_say * 5, "data": {"x": list(range(50))}},
                   ensure_ascii=False),
        "stock_in",
    )
    assert big_say * 5 in hist, "写操作历史压缩后 say 未逐字保留"
    print("(b2) 窗口内超限只剥离巨型数组、say 全文保留；写操作 say 逐字  ✓")


def test_de_overflow_retry_then_clear():
    """(d)(e)：input_too_long → 裁剪重试；仍失败 → 清上下文 + FIRST+LAST 兜底。"""
    captured.clear()
    # (d) 第一次 400，第二次成功
    conn = make_conn()
    conn.llm = ScriptedLLM([
        ("raise", OVERFLOW_ERR),
        ("chunks", [("裁剪后回答。", None)]),
    ])
    long_messages = [{"role": "system", "content": "你是小智"}]
    for i in range(6):
        long_messages.append({"role": "user", "content": f"第{i}轮问题"})
        long_messages.append({"role": "assistant", "content": f"第{i}轮回答"})
    stream = conn._run_llm_stream_with_overflow_retry(long_messages, [], "sid-d", 0)
    assert stream is not None, "裁剪重试应成功"
    assert any("上下文超限" in m for m in captured), "缺 上下文超限 WARNING"
    assert len(conn.dialogue.dialogue) == 1, "重试成功不应清空上下文"
    # 第二次调用拿到的是裁剪后的消息（最近 2 轮）
    retried = conn.llm.seen_messages[-1]
    users = [m for m in retried if m["role"] == "user"]
    assert len(users) == 2 and users[0]["content"] == "第4轮问题", f"裁剪不对: {users}"
    print("(d) input_too_long → 确定性裁剪后重试一次成功  ✓")
    captured.clear()

    # (e) 两次都 400 → 清上下文 + 兜底播报
    conn2 = make_conn()
    conn2.dialogue.dialogue.append(conn_mod.Message(role="user", content="旧问题"))
    conn2.dialogue.dialogue.append(conn_mod.Message(role="assistant", content="旧回答"))
    conn2.llm = ScriptedLLM([("raise", OVERFLOW_ERR), ("raise", OVERFLOW_ERR)])
    stream = conn2._run_llm_stream_with_overflow_retry(long_messages, [], "sid-e", 0)
    assert stream is None, "重试仍失败应返回 None"
    roles = [m.role for m in conn2.dialogue.dialogue]
    assert roles == ["system"], f"上下文未清空: {roles}"
    assert any("上下文超限" in m and "got_tokens" in m for m in captured), \
        "缺含 got/max 数字的 上下文超限 WARNING"
    items = drain(conn2.tts.tts_text_queue)
    types = [i.sentence_type for i in items]
    assert SentenceType.FIRST in types and SentenceType.LAST in types, f"缺FIRST/LAST: {types}"
    assert conn2.tts.spoken == ["信息太多，我先清一下，请再说一遍"], conn2.tts.spoken
    print("(e) 重试仍失败 → 清空上下文 + FIRST+LAST 兜底播报 + WARNING  ✓")
    captured.clear()


def test_f_prune_protocol():
    """(f) 裁剪函数：system 保留、最近 2 轮、仅最近一次工具交互、配对着齐。"""
    conn = make_conn()
    msgs = [{"role": "system", "content": "sys"}, {"role": "system", "content": "ctx"}]
    for i in range(4):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "query_stock", "arguments": "{}"}, "type": "function"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    pruned = conn._prune_messages_for_overflow_retry(msgs)
    systems = [m for m in pruned if m["role"] == "system"]
    assert len(systems) == 2, "system 未全保留"
    users = [m for m in pruned if m["role"] == "user"]
    assert [m["content"] for m in users] == ["u2", "u3"], f"未保留最近2轮: {users}"
    tools = [m for m in pruned if m["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["content"] == "r3", f"应只留最近一次工具结果: {tools}"
    tc_asst = [m for m in pruned if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(tc_asst) == 1 and tc_asst[0]["tool_calls"][0]["id"] == "c3"
    # 配对校验：每个 tool 的 id 都在 assistant.tool_calls 里；每个 tool_calls 都有响应
    known = {tc["id"] for m in tc_asst for tc in m["tool_calls"]}
    assert all(m["tool_call_id"] in known for m in tools), "存在孤儿 tool 消息"
    print("(f) 裁剪确定性 + OpenAI 工具协议完整  ✓")


def test_g_fallback_dedup():
    """(g) A4：8 秒内 _speak_fallback_phrase 不重复播。"""
    class _FakeASR(ASRProviderBase):
        async def speech_to_text(self, opus_data, session_id, artifacts=None):
            return "", None

    conn = make_conn()
    asr = object.__new__(_FakeASR)
    asr._speak_fallback_phrase(conn, "没听清，请再说一遍")
    asr._speak_fallback_phrase(conn, "没听清，请再说一遍")
    assert conn.tts.spoken == ["没听清，请再说一遍"], f"8秒内重复播报: {conn.tts.spoken}"
    print("(g) 兜底话术 8 秒内去重  ✓")


if __name__ == "__main__":
    test_abc_window_and_report()
    test_b2_latest_cap_strips_only_arrays()
    test_de_overflow_retry_then_clear()
    test_f_prune_protocol()
    test_g_fallback_dedup()
    print("\n全部通过 ✓")
