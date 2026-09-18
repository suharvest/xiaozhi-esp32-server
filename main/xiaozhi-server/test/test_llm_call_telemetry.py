# -*- coding: utf-8 -*-
"""LLM 调用遥测单测（前缀指纹 + TTFT），本地不连服务器。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_llm_call_telemetry.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_llm_call_telemetry.py

覆盖：
  (a) 假流「先空 delta 再有 content」→ 日志含 prefix/tools/msgs/ttft/total，
      且 ttft 记在第一个非空 delta 上（不是第一个 chunk）；
  (b) 相同静态 system + 相同 functions → prefix md5 稳定；
      工具顺序变化 → prefix md5 不同（sort_keys 只排字典键，不排列表顺序）；
      动态 system（第二条 system）变化 → prefix md5 不变；
  (c) 流里抛异常 → 也打一条日志，含耗时与错误类型，异常继续向上抛。
"""
import os
import sys
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

from core.providers.llm import telemetry  # noqa: E402


class _FakeLogger:
    """替换 telemetry.logger，收集 INFO 行。"""

    def __init__(self):
        self.lines = []

    def bind(self, **kwargs):
        return self

    def info(self, msg):
        self.lines.append(msg)

    def debug(self, msg):
        pass


def _capture(stream, dialogue, functions=None):
    fake = _FakeLogger()
    original = telemetry.logger
    telemetry.logger = fake
    try:
        out = list(telemetry.instrument_stream(stream, dialogue, functions))
    finally:
        telemetry.logger = original
    return out, fake.lines


def _capture_raises(stream, dialogue, functions=None):
    fake = _FakeLogger()
    original = telemetry.logger
    telemetry.logger = fake
    err = None
    try:
        list(telemetry.instrument_stream(stream, dialogue, functions))
    except Exception as e:  # noqa: BLE001
        err = e
    finally:
        telemetry.logger = original
    return err, fake.lines


STATIC_SYSTEM = "你是仓库助手，请简短回答。"
DIALOGUE = [
    {"role": "system", "content": STATIC_SYSTEM},
    {"role": "system", "content": "<context>现在 10:00</context>"},
    {"role": "user", "content": "10-8四通还有多少"},
]
TOOLS = [
    {"type": "function", "function": {"name": "query_stock", "description": "查库存"}},
    {"type": "function", "function": {"name": "direct_answer", "description": "直接回答"}},
]


def _fake_stream(delay=0.05):
    """先两个空 delta，再给 content —— 模拟 OpenAI 兼容流的开头空块。"""
    yield ("", None)
    yield (None, None)
    time.sleep(delay)
    yield ("四", None)
    yield ("通", None)


def test_a_ttft_logged_on_first_nonempty_delta():
    out, lines = _capture(_fake_stream(delay=0.05), DIALOGUE, TOOLS)
    assert len(out) == 4, out
    assert len(lines) == 1, lines
    line = lines[0]
    for token in ("LLM call: prefix=", "tools=2", "msgs=3", "chars=", "ttft=", "total="):
        assert token in line, (token, line)
    ttft = float(line.split("ttft=")[1].split("s")[0])
    assert ttft >= 0.05, line  # 空 delta 不算首 token
    print(f"(a) 首个非空 delta 记 ttft  ✓  {line}")


def test_b_prefix_md5_identity():
    same = telemetry.compute_prefix_md5(DIALOGUE, TOOLS)
    again = telemetry.compute_prefix_md5(list(DIALOGUE), list(TOOLS))
    assert same == again, (same, again)

    reordered = telemetry.compute_prefix_md5(DIALOGUE, list(reversed(TOOLS)))
    assert reordered != same, "工具顺序变化必须体现在指纹里"

    dynamic_changed = [
        {"role": "system", "content": STATIC_SYSTEM},
        {"role": "system", "content": "<context>现在 23:59</context>"},
        {"role": "user", "content": "别的问题"},
    ]
    assert telemetry.compute_prefix_md5(dynamic_changed, TOOLS) == same, "动态段不该进指纹"

    other_static = [{"role": "system", "content": STATIC_SYSTEM + "！"}]
    assert telemetry.compute_prefix_md5(other_static, TOOLS) != same
    print("(b) 指纹：静态 system + 工具顺序敏感，动态 system 不敏感  ✓")


def test_c_exception_path_logs_once():
    def boom():
        yield ("你", None)
        raise RuntimeError("connection reset")

    err, lines = _capture_raises(boom(), DIALOGUE, TOOLS)
    assert isinstance(err, RuntimeError), err
    assert len(lines) == 1, lines
    assert "LLM call failed:" in lines[0] and "total=" in lines[0], lines[0]
    assert "RuntimeError: connection reset" in lines[0], lines[0]
    print(f"(c) 异常路径也打一条  ✓  {lines[0]}")


if __name__ == "__main__":
    test_a_ttft_logged_on_first_nonempty_delta()
    test_b_prefix_md5_identity()
    test_c_exception_path_logs_once()
    print("\n全部通过 ✓")
