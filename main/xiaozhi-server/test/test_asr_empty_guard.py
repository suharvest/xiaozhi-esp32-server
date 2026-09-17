# -*- coding: utf-8 -*-
"""ASR 空识别兜底豁免单测（本地不连服务器）。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_asr_empty_guard.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_asr_empty_guard.py

覆盖四例：
  (a) just_woken_up=True + 空文本 → 不播兜底（唤醒回应的回声不是提问）；
  (b) 音频 3 帧 + 空文本 → 不播（< asr_empty_min_frames，误触）；
  (c) 音频 20 帧 + 空文本 + just_woken_up=False → 播「没听清」；
  (d) reason="timeout"（后端故障）即使 3 帧也播「识别服务暂时不可用」。
"""
import asyncio
import os
import queue
import sys
import tempfile
import uuid


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

from core.providers.asr.base import ASRProviderBase  # noqa: E402
from core.providers.tts.dto.dto import SentenceType  # noqa: E402

EMPTY_REPLY = "没听清，请再说一遍"
FAILURE_REPLY = "识别服务暂时不可用，请稍后再试"


class FakeTTS:
    def __init__(self):
        # 真实实现用的是 queue.Queue（同步 put），不是 asyncio.Queue。
        self.tts_text_queue = queue.Queue()
        self.stored = {}
        self.spoken = []

    def store_tts_text(self, sentence_id, text):
        self.stored[sentence_id] = text

    def tts_one_sentence(self, conn, content_type, content_detail=None):
        self.spoken.append(content_detail)


class FakeConn:
    def __init__(self, **over):
        self.config = {
            "asr_empty_reply": EMPTY_REPLY,
            "asr_failure_reply": FAILURE_REPLY,
        }
        self.config.update(over.pop("config", {}))
        self.session_id = uuid.uuid4().hex
        self.stop_event = asyncio.Event()
        self.client_abort = False
        self.just_woken_up = False
        self.asr_audio = []
        self.sentence_id = None
        self.voiceprint_provider = None
        self.tts = FakeTTS()
        for k, v in over.items():
            setattr(self, k, v)

    def reset_audio_states(self):
        self.asr_audio.clear()
        self.client_voice_stop = False


class FakeASR(ASRProviderBase):
    """speech_to_text 恒返回空文本，逼 handle_voice_stop 走空结果分支。"""

    def __init__(self):
        super().__init__()
        self.output_dir = tempfile.mkdtemp()

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        return "", None


def drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


def run_stop(conn, frames, reason=None):
    asr = FakeASR()
    asr.asr_failed_reason = reason
    asyncio.run(asr.handle_voice_stop(conn, [b"\x00"] * frames))
    return drain(conn.tts.tts_text_queue)


def test_a_just_woken_up_skips():
    conn = FakeConn(just_woken_up=True)
    items = run_stop(conn, frames=20)
    assert not items, f"唤醒后首段空识别不该入队: {items}"
    assert conn.tts.spoken == [], conn.tts.spoken
    print("(a) just_woken_up=True + 空文本 → 不播兜底  ✓")


def test_b_too_few_frames_skips():
    conn = FakeConn()
    items = run_stop(conn, frames=3)
    assert not items, f"3 帧空识别不该入队: {items}"
    assert conn.tts.spoken == [], conn.tts.spoken
    print("(b) 音频 3 帧 + 空文本 → 不播兜底  ✓")


def test_c_normal_empty_speaks():
    conn = FakeConn()
    items = run_stop(conn, frames=20)
    types = [i.sentence_type for i in items]
    assert SentenceType.FIRST in types and SentenceType.LAST in types, types
    assert conn.tts.spoken == [EMPTY_REPLY], conn.tts.spoken
    print("(c) 音频 20 帧 + 空文本 → 播「没听清」  ✓")


def test_d_backend_failure_always_speaks():
    conn = FakeConn()
    items = run_stop(conn, frames=3, reason="timeout")
    types = [i.sentence_type for i in items]
    assert SentenceType.FIRST in types and SentenceType.LAST in types, types
    assert conn.tts.spoken == [FAILURE_REPLY], conn.tts.spoken
    print("(d) reason=timeout 即使 3 帧仍播「识别服务暂时不可用」  ✓")


if __name__ == "__main__":
    test_a_just_woken_up_skips()
    test_b_too_few_frames_skips()
    test_c_normal_empty_speaks()
    test_d_backend_failure_always_speaks()
    print("\n全部通过 ✓")
