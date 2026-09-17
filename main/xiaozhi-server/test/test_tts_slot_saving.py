# -*- coding: utf-8 -*-
"""OVS TTS 省会话槽单测（本地不连服务器）。

两种跑法：

1. pytest（本机 venv）::

       cd main/xiaozhi-server && .venv/bin/python -m pytest test/test_tts_slot_saving.py -q

2. 容器内 stdin（现场盒子无 pytest，也不往容器写文件）::

       docker exec -i -w /opt/xiaozhi-esp32-server xiaozhi-server \
           python3 - < main/xiaozhi-server/test/test_tts_slot_saving.py

覆盖：
  (a) 首句仍按 base 规则早出（遇第一个逗号级标点即切）；
  (b) 首句之后：N-1 字不发，第 N 字到了才发一条；
  (c) 多标点长文本在阈值内只发一条（而不是每个句号切一条）；
  (d) LAST 时余文不足阈值也发，且只收尾一次；
  (e) 空文本 LAST 只收尾、不发 TTS；
  (f) 在途流可中止：client_abort / stop_event / sentence_id 换轮 / close()
      四种都在 <1s 内退出，且中止后不再往音频队列里塞东西；
  (g) 连续 429 时 _post_with_retry 总耗时 ≤3.5s 且返回 None；
  (h) to_tts 采集：返回非空 opus 帧列表，且 tts_audio_queue 保持为空
      （没有 FIRST/LAST 混进播放队列）；
  (i) 采集期间 current_sentence_id != conn.sentence_id 不导致中止。
"""
import asyncio
import os
import queue
import sys
import threading
import time
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

import core.providers.tts.openvoicestream_tts as ovs  # noqa: E402
from core.providers.tts.base import TTSProviderBase  # noqa: E402
from core.providers.tts.dto.dto import (  # noqa: E402
    ContentType,
    InterfaceType,
    SentenceType,
    TTSMessageDTO,
)


class FakeConn:
    def __init__(self):
        self.stop_event = threading.Event()
        self.client_abort = False
        self.sentence_id = None
        self.audio_format = "pcm"
        self.sample_rate = 16000


def make_provider(**over):
    """不跑 TTSProvider.__init__（它会起 capabilities 探测线程 + 连网）。"""
    p = object.__new__(ovs.TTSProvider)
    TTSProviderBase.__init__(p, {}, False)
    p.interface_type = InterfaceType.SINGLE_STREAM
    p.base_url = "http://127.0.0.1:8621"
    p.api_url = f"{p.base_url}/tts/stream"
    p.api_key = ""
    p.speaker_id = None
    p.sid = None
    p.speaker_embedding_b64 = None
    p.available_speakers = {}
    p.speed = 1.0
    p.pitch = None
    p.language = None
    p.timeout = 30
    p.audio_format = "pcm"
    p.opus_encoder = None
    p.opus_sample_rate = None
    p.pcm_buffer = bytearray()
    p.subsequent_sentence_min_chars = 32
    p.tts_max_retries = 2
    p.retry_max_delay = 1.5
    p.retry_budget_seconds = 3.0
    p._closed = False
    p._collect_frames = None
    p._synth_lock = threading.Lock()
    p.conn = FakeConn()
    for k, v in over.items():
        setattr(p, k, v)
    return p


# --------------------------------------------------------------------------
# a~e：分段合并（驱动真实的 tts_text_priority_thread）
# --------------------------------------------------------------------------
class Driver:
    """起真实文本线程，记录它发了几条 TTS、收了几次尾。"""

    def __init__(self, p):
        self.p = p
        self.sent = []      # (text, is_last)
        self.finished = []  # 收尾次数
        p.to_tts_single_stream = self._send
        p._process_before_stop_play_files = self._finish
        self.sid = uuid.uuid4().hex
        p.conn.sentence_id = self.sid
        self.thread = threading.Thread(target=p.tts_text_priority_thread, daemon=True)
        self.thread.start()

    def _send(self, text, is_last=False):
        self.sent.append((text, is_last))
        # 真实实现里 is_last 的收尾发生在 text_to_speak 成功之后
        if is_last:
            self._finish()

    def _finish(self):
        self.finished.append(1)

    def put(self, sentence_type, content_type=ContentType.ACTION, text=None):
        self.p.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=self.sid,
                sentence_type=sentence_type,
                content_type=content_type,
                content_detail=text,
            )
        )

    def text(self, s):
        self.put(SentenceType.MIDDLE, ContentType.TEXT, s)

    def settle(self, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.p.tts_text_queue.empty():
            time.sleep(0.01)
        time.sleep(0.05)

    def stop(self):
        self.p.conn.stop_event.set()
        self.thread.join(timeout=2)


def test_a_first_sentence_early():
    p = make_provider()
    d = Driver(p)
    d.put(SentenceType.FIRST)
    d.text("你好，")
    d.settle()
    assert d.sent == [("你好", False)], f"首句未按 base 规则早出: {d.sent}"
    assert p.is_first_sentence is False
    d.stop()
    print("(a) 首句仍按 base 规则早出  ✓")


def test_b_threshold():
    p = make_provider()
    d = Driver(p)
    d.put(SentenceType.FIRST)
    d.text("好，")            # 消耗首句
    d.settle()
    assert len(d.sent) == 1, d.sent
    n = p.subsequent_sentence_min_chars
    d.text("甲" * (n - 1))     # N-1 字：不发
    d.settle()
    assert len(d.sent) == 1, f"N-1 字就发了: {d.sent}"
    d.text("乙")               # 凑满 N 字：发一条
    d.settle()
    assert len(d.sent) == 2, f"凑满 N 字仍未发: {d.sent}"
    assert d.sent[1][0] == "甲" * (n - 1) + "乙", d.sent[1]
    assert p.processed_chars == len("好，") + n, p.processed_chars
    d.stop()
    print(f"(b) 后续文本 {n - 1} 字不发、{n} 字发一条  ✓")


def test_c_multi_punct_one_stream():
    p = make_provider()
    d = Driver(p)
    d.put(SentenceType.FIRST)
    d.text("在。")
    d.settle()
    base_sent = len(d.sent)
    # 35 字、5 个句号：base 规则会切 5 条，合并后只应是 1 条
    for _ in range(5):
        d.text("甲乙丙丁戊己。")
    d.settle()
    extra = d.sent[base_sent:]
    assert len(extra) == 1, f"多标点长文本被切成 {len(extra)} 条: {extra}"
    assert len(extra[0][0]) >= p.subsequent_sentence_min_chars
    d.stop()
    print("(c) 多标点长文本在阈值内只发一条  ✓")


def test_d_last_flushes_short_tail():
    p = make_provider()
    d = Driver(p)
    d.put(SentenceType.FIRST)
    d.text("在。")
    d.settle()
    d.text("还有五个字")       # 远不足阈值
    d.settle()
    assert len(d.sent) == 1, f"不足阈值就发了: {d.sent}"
    d.put(SentenceType.LAST)
    d.settle()
    assert len(d.sent) == 2, f"LAST 未排空余文: {d.sent}"
    assert d.sent[1] == ("还有五个字", True), d.sent[1]
    assert len(d.finished) == 1, f"收尾 {len(d.finished)} 次，应为 1 次"
    d.stop()
    print("(d) LAST 排空不足阈值的余文且只收尾一次  ✓")


def test_e_empty_last():
    p = make_provider()
    d = Driver(p)
    d.put(SentenceType.FIRST)
    d.put(SentenceType.LAST)
    d.settle()
    assert d.sent == [], f"空文本却发了 TTS: {d.sent}"
    assert len(d.finished) == 1, f"收尾 {len(d.finished)} 次，应为 1 次"
    d.stop()
    print("(e) 空文本 LAST 只收尾一次  ✓")


# --------------------------------------------------------------------------
# f：在途流可中止
# --------------------------------------------------------------------------
class FakeEncoder:
    sample_rate = 16000
    channels = 1
    frame_size_ms = 60

    def __init__(self):
        self.frames = 0
        self.flushed = False

    def encode_pcm_to_opus_stream(self, pcm, end_of_stream=False, callback=None):
        self.frames += 1
        if end_of_stream:
            self.flushed = True
        if callback:
            callback(b"\x00" * 8)

    def close(self):
        pass


class FakeContent:
    """先吐一段音频，然后永远阻塞（模拟 OVS 慢慢合成）。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.blocked = asyncio.Event()  # 永不 set

    async def readany(self):
        if self.chunks:
            return self.chunks.pop(0)
        await self.blocked.wait()
        return b""


class FakeResp:
    def __init__(self, chunks, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "fake"

    async def release(self):
        return None


class FakeSession:
    def __init__(self, resp_factory):
        self._resp_factory = resp_factory
        self.posts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        return self._resp_factory()


def _install_fake_aiohttp(resp_factory):
    session = FakeSession(resp_factory)
    ovs.aiohttp = SimpleNamespace(
        ClientSession=lambda *a, **kw: session,
        ClientTimeout=lambda *a, **kw: None,
    )
    return session


def test_f_abort_inflight():
    import struct

    real_aiohttp = ovs.aiohttp
    try:
        for name, trigger in (
            ("client_abort", lambda p: setattr(p.conn, "client_abort", True)),
            ("stop_event", lambda p: p.conn.stop_event.set()),
            ("sentence_id 换轮", lambda p: setattr(p.conn, "sentence_id", "NEW-ROUND")),
            ("close()", lambda p: setattr(p, "_closed", True)),
        ):
            p = make_provider()
            sid = uuid.uuid4().hex
            p.current_sentence_id = sid
            p.conn.sentence_id = sid
            enc = FakeEncoder()
            p._ensure_encoder = lambda sr, _p=p, _e=enc: setattr(_p, "opus_encoder", _e)
            chunks = [struct.pack("<I", 16000) + b"\x01" * 1920, b"\x02" * 1920]
            _install_fake_aiohttp(lambda: FakeResp(chunks))

            async def run():
                task = asyncio.ensure_future(p.text_to_speak("测试文本"))
                await asyncio.sleep(0.15)
                before = p.tts_audio_queue.qsize()
                trigger(p)
                t0 = time.monotonic()
                result = await asyncio.wait_for(task, timeout=1.0)
                return result, time.monotonic() - t0, before

            result, elapsed, before = asyncio.run(run())
            after = p.tts_audio_queue.qsize()
            assert result is False, f"{name}: 应返回 False，得到 {result}"
            assert elapsed < 1.0, f"{name}: 退出耗时 {elapsed:.2f}s"
            assert after == before, f"{name}: 中止后仍入队 {after - before} 项"
            assert not enc.flushed, f"{name}: 中止后仍 flush 了尾音"
            assert len(p.pcm_buffer) == 0, f"{name}: pcm_buffer 未清"
            drained = []
            while not p.tts_audio_queue.empty():
                drained.append(p.tts_audio_queue.get()[0])
            assert SentenceType.LAST not in drained, f"{name}: 中止路径不应入队 LAST"
            print(f"(f) 中止：{name} 在 {elapsed * 1000:.0f}ms 内退出  ✓")
    finally:
        ovs.aiohttp = real_aiohttp


# --------------------------------------------------------------------------
# g：429 重试收敛
# --------------------------------------------------------------------------
def test_g_retry_budget():
    real_aiohttp = ovs.aiohttp
    try:
        p = make_provider()
        session = _install_fake_aiohttp(
            lambda: FakeResp([], status=429, headers={"Retry-After": "30"})
        )

        async def run():
            t0 = time.monotonic()
            r = await p._post_with_retry(session, {"text": "x"})
            return r, time.monotonic() - t0

        resp, elapsed = asyncio.run(run())
        assert resp is None, f"连续 429 应返回 None，得到 {resp}"
        assert elapsed <= 3.5, f"429 重试总耗时 {elapsed:.2f}s > 3.5s"
        assert session.posts <= p.tts_max_retries + 1, session.posts
        print(
            f"(g) 连续 429：{session.posts} 次请求、{elapsed:.2f}s 后返回 None  ✓"
        )
    finally:
        ovs.aiohttp = real_aiohttp


# --------------------------------------------------------------------------
# h / i：to_tts 非流式采集
# --------------------------------------------------------------------------
class FiniteContent(FakeContent):
    """吐完 chunks 就 EOF（FakeContent 是吐完后永久阻塞）。"""

    async def readany(self):
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class FiniteResp(FakeResp):
    def __init__(self, chunks, status=200, headers=None):
        super().__init__(chunks, status=status, headers=headers)
        self.content = FiniteContent(chunks)


def _collect_setup(p):
    """4 字节采样率头 + 2 帧 PCM 的假响应，并把编码器换成 FakeEncoder。"""
    import struct

    enc = FakeEncoder()
    p._ensure_encoder = lambda sr, _p=p, _e=enc: setattr(_p, "opus_encoder", _e)
    # frame_bytes = 16000 * 1 * 60 / 1000 * 2 = 1920
    chunks = [struct.pack("<I", 16000) + b"\x01" * 1920, b"\x02" * 1920]
    _install_fake_aiohttp(lambda: FiniteResp(chunks))
    return enc


def test_h_to_tts_collect():
    real_aiohttp = ovs.aiohttp
    try:
        p = make_provider()
        sid = uuid.uuid4().hex
        p.current_sentence_id = sid
        p.conn.sentence_id = sid
        enc = _collect_setup(p)

        frames = p.to_tts("我在这里哦")

        assert isinstance(frames, list), f"to_tts 应返回帧列表，得到 {type(frames)}"
        assert len(frames) >= 2, f"采集到的帧太少: {len(frames)}"
        assert all(isinstance(f, bytes) for f in frames), frames[:2]
        assert enc.frames >= 2, enc.frames
        assert p.tts_audio_queue.empty(), (
            f"采集不该往播放队列入队，队列里有 {p.tts_audio_queue.qsize()} 项"
        )
        assert p._collect_frames is None, "采集结束后 _collect_frames 未复位"
        print(f"(h) to_tts 采集到 {len(frames)} 帧、播放队列为空  ✓")
    finally:
        ovs.aiohttp = real_aiohttp


def test_i_collect_ignores_round():
    real_aiohttp = ovs.aiohttp
    try:
        p = make_provider()
        # 采集与对话轮次无关：上一轮的 sentence_id 残留不应把这条流判为过期
        p.current_sentence_id = "OLD-ROUND"
        p.conn.sentence_id = "NEW-ROUND"
        _collect_setup(p)

        frames = p.to_tts("我在这里哦")

        assert frames, "sentence_id 换轮把采集误判为中止了"
        assert p.tts_audio_queue.empty(), p.tts_audio_queue.qsize()
        print("(i) 采集期间 sentence_id 不匹配仍正常完成  ✓")
    finally:
        ovs.aiohttp = real_aiohttp


if __name__ == "__main__":
    test_a_first_sentence_early()
    test_b_threshold()
    test_c_multi_punct_one_stream()
    test_d_last_flushes_short_tail()
    test_e_empty_last()
    test_f_abort_inflight()
    test_g_retry_budget()
    test_h_to_tts_collect()
    test_i_collect_ignores_round()
    print("\n全部通过 ✓")
