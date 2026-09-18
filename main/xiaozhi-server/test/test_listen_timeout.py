# -*- coding: utf-8 -*-
"""listen 超时兜底单测（本地不连服务器）。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_listen_timeout.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_listen_timeout.py

覆盖五例：
  (a) listen start 后零音频零文本 → N 秒后队列出现兜底话术（FIRST+LAST），
      并出现含「listen 超时兜底」的 WARNING；
  (b) N 秒内到达 voice_stop/ASR 文本（handle_voice_stop 被调用）→ 到点不播；
  (c) client_abort=True → 不播；
  (d) 到点时音频仍在到达（1s 前刚收到帧）→ 不触发，顺延；
  (e) 顺延后连续 quiet 秒无新帧 → 触发一次（且只一次）。
"""
import asyncio
import os
import queue
import sys
import tempfile
import time
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
from loguru import logger as loguru_logger  # noqa: E402

# import 完成后（模块内 setup_logging 已跑）再挂捕获 sink
captured = []
loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")

MARKER = "listen 超时兜底"


class FakeTTS:
    def __init__(self):
        # 注意：真实实现用的是 queue.Queue（同步 put），不是 asyncio.Queue。
        # 用 asyncio.Queue 会让 provider 里的同步 .put() 变成未 await 的协程，
        # 队列永远是空的，测试会假失败（已踩过）。
        self.tts_text_queue = queue.Queue()
        self.stored = {}
        self.spoken = []

    def store_tts_text(self, sentence_id, text):
        self.stored[sentence_id] = text

    def tts_one_sentence(self, conn, content_type, content_detail=None):
        self.spoken.append(content_detail)


class FakeConn:
    def __init__(self, config):
        self.config = config
        self.session_id = uuid.uuid4().hex
        self.stop_event = asyncio.Event()
        self.client_abort = False
        self.asr_audio = []
        self.sentence_id = None
        self.voiceprint_provider = None
        self.tts = FakeTTS()
        self.reset_count = 0
        self.client_listen_mode = "manual"
        self.client_have_voice = False
        self.client_voice_stop = False
        self._last_audio_frame_ts = None

    def reset_audio_states(self):
        self.reset_count += 1
        self.asr_audio.clear()
        self.client_voice_stop = False


class FakeASR(ASRProviderBase):
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


async def _case_a():
    """零音频零文本 → 到点播兜底（FIRST+LAST）+ WARNING"""
    conn = FakeConn({"asr_listen_timeout_sec": 0.5})  # enabled 默认 True
    asr = FakeASR()
    asr._start_listen_timeout(conn)
    await asyncio.sleep(1.2)
    items = drain(conn.tts.tts_text_queue)
    types = [i.sentence_type for i in items]
    assert SentenceType.FIRST in types and SentenceType.LAST in types, f"缺FIRST/LAST: {types}"
    assert types.count(SentenceType.FIRST) == 1, "只应播一次"
    assert conn.tts.spoken == ["没听清，请再说一遍"], conn.tts.spoken
    assert any(MARKER in m for m in captured), "未见 listen 超时兜底 WARNING"
    assert conn.reset_count >= 1, "未复位本轮状态"
    print("(a) 零音频零文本 → 兜底话术 FIRST+LAST + WARNING  ✓")


async def _case_b():
    """N 秒内到达 voice_stop/ASR 文本 → 定时器已取消，不播"""
    # asr_empty/failure_reply 置空，隔离 handle_voice_stop 空结果分支的播报
    conn = FakeConn({
        "asr_listen_timeout_sec": 1.0,
        "asr_empty_reply": "",
        "asr_failure_reply": "",
    })
    asr = FakeASR()
    asr._start_listen_timeout(conn)
    await asyncio.sleep(0.2)
    # 模拟 voice_stop/ASR出文本到达：handle_voice_stop 入口会取消定时器
    await asr.handle_voice_stop(conn, [b""])
    await asyncio.sleep(1.8)
    items = drain(conn.tts.tts_text_queue)
    assert not items and not conn.tts.spoken, f"不应有播报: {items} {conn.tts.spoken}"
    assert not any(MARKER in m for m in captured), "不应触发超时兜底 WARNING"
    print("(b) 到达 ASR 文本/voice_stop → 不播兜底  ✓")


async def _case_c():
    """client_abort=True → 到点不播"""
    conn = FakeConn({"asr_listen_timeout_sec": 0.5})
    asr = FakeASR()
    asr._start_listen_timeout(conn)
    conn.client_abort = True
    await asyncio.sleep(1.2)
    items = drain(conn.tts.tts_text_queue)
    assert not items and not conn.tts.spoken, f"打断后不应播报: {items} {conn.tts.spoken}"
    assert not any(MARKER in m for m in captured), "打断后不应触发超时兜底 WARNING"
    print("(c) client_abort=True → 不播  ✓")


async def _case_d_defers_while_audio_arriving():
    """到点时 1s 前还有帧 → 不触发，顺延等一个 quiet 窗口。

    现场日志：`已等待=15s, 收到音频帧数=212` —— 用户连续说了 12.7s，VAD 没判停，
    硬计时到点插播「没听清」，5s 后 ASR 才把长句吐出来。
    """
    conn = FakeConn({
        "asr_listen_timeout_sec": 0.5,
        "asr_listen_timeout_quiet_sec": 1.0,
        "asr_listen_timeout_max_sec": 10,
    })
    asr = FakeASR()
    asr._start_listen_timeout(conn)
    # 持续喂帧，跨过 0.5s 到点
    for _ in range(9):
        await asr.receive_audio(conn, b"\x00" * 1920, True)
        await asyncio.sleep(0.1)
    # t≈0.9s：到点时刻早已过，但最后一帧距今仅 ~0.1s → 必须还没播
    items = drain(conn.tts.tts_text_queue)
    assert not items and not conn.tts.spoken, f"音频仍在到达时不应兜底: {items} {conn.tts.spoken}"
    assert not any(MARKER in m for m in captured), "音频仍在到达时不应触发兜底 WARNING"
    assert conn.asr_audio, "帧应已缓存"
    asr._cancel_listen_timeout(conn)
    print("(d) 到点时音频仍在到达 → 顺延不播  ✓")


async def _case_e_fires_after_quiet():
    """顺延后连续 quiet 秒无新帧 → 触发一次"""
    conn = FakeConn({
        "asr_listen_timeout_sec": 0.5,
        "asr_listen_timeout_quiet_sec": 1.0,
        "asr_listen_timeout_max_sec": 10,
    })
    asr = FakeASR()
    asr._start_listen_timeout(conn)
    for _ in range(9):
        await asr.receive_audio(conn, b"\x00" * 1920, True)
        await asyncio.sleep(0.1)
    # t≈0.9s：停止喂帧，等一个 quiet 窗口（1.0s）+ 余量
    await asyncio.sleep(1.6)
    items = drain(conn.tts.tts_text_queue)
    types = [i.sentence_type for i in items]
    assert types.count(SentenceType.FIRST) == 1, f"应只播一次: {types}"
    assert SentenceType.LAST in types, types
    assert conn.tts.spoken == ["没听清，请再说一遍"], conn.tts.spoken
    warns = [m for m in captured if MARKER in m]
    assert len(warns) == 1, warns
    assert "最后一帧距今=" in warns[0], warns[0]
    print("(e) 顺延后连续 quiet 无帧 → 触发一次  ✓")


# 注意：每个用例进入前必须“清空全部”，不能按进入前的长度切片删除
#（进入前长度通常为 0，del captured[:0] 是空操作，会把上一例的 WARNING
# 留在列表里，导致下一例假失败 —— 已踩过）。
def test_a_timeout_speaks_fallback():
    captured.clear()
    asyncio.run(_case_a())


def test_b_voice_stop_cancels_timer():
    captured.clear()
    asyncio.run(_case_b())


def test_c_client_abort_skips_fallback():
    captured.clear()
    asyncio.run(_case_c())


def test_d_defers_while_audio_arriving():
    captured.clear()
    asyncio.run(_case_d_defers_while_audio_arriving())


def test_e_fires_after_quiet_window():
    captured.clear()
    asyncio.run(_case_e_fires_after_quiet())


if __name__ == "__main__":
    test_a_timeout_speaks_fallback()
    test_b_voice_stop_cancels_timer()
    test_c_client_abort_skips_fallback()
    test_d_defers_while_audio_arriving()
    test_e_fires_after_quiet_window()
    print("\n全部通过 ✓")
