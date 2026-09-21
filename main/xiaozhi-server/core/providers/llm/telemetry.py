# -*- coding: utf-8 -*-
"""LLM 调用遥测：前缀指纹 + TTFT。

现场 EdgeLLM（TRT-LLM，OpenAI 兼容）只为「开头 system 块（静态 system prompt +
tools 渲染结果）」保留 KV 检查点：前缀命中时 TTFT 1.3~2.3s，前缀失配要整段重算
prefill，实测 9~13s，而设备约 10s 收不到音频就断线。

所以每次调用都要能回答两个问题：
  1. 这次的前缀是什么（`prefix` = md5(静态 system + 排序后的 tools JSON)[:8]）
  2. 首 token 花了多久（`ttft`）

`instrument_stream()` 包住 provider 的流式生成器，不改变产出内容。
"""

import hashlib
import json
import time

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


def compute_prefix_md5(dialogue, functions) -> str:
    """前缀指纹 = md5(静态 system 内容 + json.dumps(functions, sort_keys=True))[:8]。

    静态 system 取 messages[0]（仅当 role == "system"），与
    `Dialogue.get_llm_dialogue_with_memory()` 的第一段对应；动态 system（时间、
    记忆、说话人）是第三段，本来就每轮变化，不计入指纹。
    """
    static_system = ""
    try:
        if dialogue:
            first = dialogue[0]
            if isinstance(first, dict) and first.get("role") == "system":
                static_system = first.get("content") or ""
    except Exception:
        static_system = ""
    try:
        tools_repr = json.dumps(
            functions or [], sort_keys=True, ensure_ascii=False, default=str
        )
    except Exception:
        tools_repr = str(functions)
    raw = f"{static_system}{tools_repr}".encode("utf-8", errors="replace")
    return hashlib.md5(raw).hexdigest()[:8]


def total_chars(dialogue) -> int:
    """粗略的请求体规模（字符数），用于判断是不是上下文变长把前缀挤掉了。"""
    try:
        return len(json.dumps(dialogue or [], ensure_ascii=False, default=str))
    except Exception:
        return -1


def _has_content(item) -> bool:
    """流里第一个「非空 delta」：content 有字，或带 tool_calls。"""
    if item is None:
        return False
    if isinstance(item, tuple) or isinstance(item, list):
        if len(item) == 2:
            content, tool_calls = item[0], item[1]
            return bool(content) or bool(tool_calls)
        return bool(item)
    return bool(item)


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def instrument_stream(stream, dialogue, functions=None, label="LLM call"):
    """透明包装流式生成器，结束时打一条 INFO。

    成功：`LLM call: prefix=<md5> tools=<n> msgs=<n> chars=<n> ttft=<x.xx>s total=<y.yy>s`
    异常：`LLM call failed: ... ttft=... total=... err=<类型>: <消息>`
    调用方提前 break（GeneratorExit）不打日志——那不是一次完整调用。
    """
    prefix = compute_prefix_md5(dialogue, functions)
    n_tools = len(functions or [])
    n_msgs = len(dialogue or [])
    chars = total_chars(dialogue)
    started = time.monotonic()
    ttft = None
    try:
        for item in stream:
            if ttft is None and _has_content(item):
                ttft = time.monotonic() - started
            yield item
    except GeneratorExit:
        raise
    except BaseException as e:
        logger.bind(tag=TAG).info(
            f"{label} failed: prefix={prefix} tools={n_tools} msgs={n_msgs} "
            f"chars={chars} ttft={_fmt(ttft)}s total={_fmt(time.monotonic() - started)}s "
            f"err={type(e).__name__}: {e}"
        )
        raise
    logger.bind(tag=TAG).info(
        f"{label}: prefix={prefix} tools={n_tools} msgs={n_msgs} chars={chars} "
        f"ttft={_fmt(ttft)}s total={_fmt(time.monotonic() - started)}s"
    )
