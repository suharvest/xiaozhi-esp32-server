import os
import struct
import queue
import threading
import aiohttp
import asyncio
import traceback
from typing import Optional
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner, convert_percentage_to_range
from core.providers.tts.base import TTSProviderBase
from core.utils import opus_encoder_utils, textUtils
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()


def _to_optional_int(v) -> Optional[int]:
    """Coerce a config value to int, or return None for empty/invalid."""
    if v is None:
        return None
    if isinstance(v, bool):
        # Bool is an int subclass; treat True/False as not-set to be safe
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class TTSProvider(TTSProviderBase):
    """OpenVoiceStream TTS streaming provider.

    Differences vs Kokoro remote_tts_stream:
      * Response body has NO 44-byte WAV header. Instead the first 4 bytes
        are a uint32 little-endian sample-rate, followed by raw int16 PCM.
      * Sample rate is read dynamically from the response and the Opus
        encoder is (re)created accordingly the first time we see a rate
        (or when the rate changes between requests).
      * Optional pitch / language fields can be passed through to the backend.
    """

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        # 默认端口 8621：OVS 容器内监听 8000，对外发布的宿主端口是 8621。
        # 写 8000 会让留空的配置静默连不上。
        self.base_url = config.get("base_url", "http://127.0.0.1:8621")
        self.api_url = f"{self.base_url}/tts/stream"
        # OVS_API_KEYS 启用时必须带；留空表示服务端未开鉴权。
        self.api_key = (config.get("api_key") or "").strip()
        # Speaker selection — priority: embedding > speaker_id > sid (legacy).
        # All default to None so we only send the field user actually set.
        self.speaker_id = _to_optional_int(config.get("speaker_id"))
        self.sid = _to_optional_int(config.get("sid"))  # legacy/deprecated
        self.speaker_embedding_b64 = config.get("speaker_embedding_b64") or None

        if self.speaker_embedding_b64 and self.speaker_id is not None:
            logger.bind(tag=TAG).warning(
                "Both speaker_embedding_b64 and speaker_id set; embedding wins"
            )

        self.available_speakers = {}  # id -> speaker dict, filled async by _fetch_capabilities

        self.speed = float(config.get("speed", 1.0))
        # Optional extras — only forwarded when not None
        pitch = config.get("pitch", None)
        self.pitch = float(pitch) if pitch is not None else None

        # 角色级的语速/音调覆盖模型级配置。
        #
        # 智控台「角色配置」里有音量/语速/音调三个滑块，manager-api 会把它们作为
        # ttsVolume / ttsRate / ttsPitch 注入 TTS config（ConfigServiceImpl:475-480）。
        # 在此之前只有火山双流一家消费这三个键，所以用户拖了滑块对 OVS 毫无反应 ——
        # 又是一个「配了没用」的坑。这里对齐上游语义把它接上。
        #
        # 量纲换算（两边完全不同，直接透传会得到荒唐的值）：
        #   ttsRate  百分比 -100~100  → OVS speed 倍率，合法区间 [0.25, 4.0]
        #       取 0.5~2.0、基准 1.0：这是听感上合理的范围。不用 0.25~4.0——那是
        #       服务端的**校验上限**，拿它当滑块量程会让 ±20% 的微调变成剧变。
        #   ttsPitch 百分比 -100~100  → OVS pitch 半音，合法区间 [-24, 24]
        #       取 ±12（一个八度），与火山双流的选择一致，也稳在服务端限制内。
        #   ttsVolume → **OVS 没有音量字段**（TTSRequest 里根本不存在），无法支持。
        if "ttsRate" in config and config["ttsRate"] is not None:
            self.speed = round(
                convert_percentage_to_range(
                    config["ttsRate"], min_val=0.5, max_val=2.0, base_val=1.0
                ),
                3,
            )
        if "ttsPitch" in config and config["ttsPitch"] is not None:
            self.pitch = round(
                convert_percentage_to_range(
                    config["ttsPitch"], min_val=-12.0, max_val=12.0, base_val=0.0
                ),
                3,
            )
        if config.get("ttsVolume") not in (None, 0):
            # 说清楚而不是静默忽略：用户拖了音量滑块，得知道它为什么没反应。
            logger.bind(tag=TAG).warning(
                "OpenVoiceStream 不支持音量调节（服务端 TTSRequest 无 volume 字段），"
                f"角色配置里的音量设置 {config.get('ttsVolume')} 将被忽略；"
                "如需调整请在设备端或播放链路上处理"
            )
        language = config.get("language", None)
        self.language = language if language else None
        # Manager API may serialize numeric form fields as JSON strings. aiohttp
        # compares ``total`` with numeric values internally, so passing "30"
        # raises: TypeError: '>' not supported between instances of 'str' and
        # 'int'. Normalize at the provider boundary just like speed/pitch.
        self.timeout = float(config.get("timeout", 30))
        self.audio_format = "pcm"
        self.before_stop_play_files = []

        # ---- OVS 会话槽节流参数 ----
        # 现场 OVS 会话池只有 2 个全局槽（ASR 1 + TTS 1）。每句话一条 HTTP 流
        # ⇒ 每轮问答被切成几句就抢几次槽。首句之后把分段合并成更少、更长的
        # 请求，槽位争抢次数随之下降；首句仍按 base.py 的规则尽早出以保延迟。
        self.subsequent_sentence_min_chars = int(
            config.get("subsequent_sentence_min_chars", 32)
        )
        # 429（too_many_sessions）重试收敛：槽满时死等只会把这一轮堵死，
        # 不如快速放弃交回上层。三个值都夹住最坏等待时间。
        self.tts_max_retries = int(config.get("max_retries", 2))
        self.retry_max_delay = float(config.get("retry_max_delay", 1.5))
        self.retry_budget_seconds = float(config.get("retry_budget_seconds", 3.0))

        # close() 后不再启动/继续任何在途流
        self._closed = False

        # Lazily created when we know the actual sample rate
        self.opus_encoder = None
        self.opus_sample_rate = None

        # PCM buffer
        self.pcm_buffer = bytearray()

        logger.bind(tag=TAG).info(
            f"OpenVoiceStream TTS initialized, endpoint={self.api_url}, "
            f"speaker_id={self.speaker_id}, sid={self.sid}, "
            f"clone_voice={'yes' if self.speaker_embedding_b64 else 'no'}"
        )

        # Fire-and-forget capabilities probe (non-blocking init).
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._fetch_capabilities())
        except RuntimeError:
            threading.Thread(
                target=lambda: asyncio.run(self._fetch_capabilities()), daemon=True
            ).start()

    def _ensure_encoder(self, sample_rate: int):
        """Create / replace Opus encoder when the response sample rate is known."""
        if self.opus_encoder is not None and self.opus_sample_rate == sample_rate:
            return
        # Replace stale encoder
        if self.opus_encoder is not None:
            try:
                self.opus_encoder.close()
            except Exception:
                pass
        logger.bind(tag=TAG).info(
            f"Creating Opus encoder for sample_rate={sample_rate}"
        )
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=sample_rate, channels=1, frame_size_ms=60
        )
        self.opus_sample_rate = sample_rate

    def _get_segment_text(self):
        """首句沿用 base 规则；之后按最小字数合并，减少 TTS 流条数。

        base.py 的后续句规则是「遇到句尾标点就切」，一段 LLM 回答常被切成
        4~8 条 HTTP 流，每条都要抢一次 OVS 会话槽。这里改成：攒够
        ``subsequent_sentence_min_chars`` 才发，否则继续缓冲；余文由
        LAST 时的 ``_process_remaining_text_stream`` 排空，不会丢字。
        """
        if self.is_first_sentence:
            return super()._get_segment_text()

        full_text = "".join(self.tts_text_buff)
        raw = full_text[self.processed_chars:]
        if len(raw) < self.subsequent_sentence_min_chars:
            return None
        self.processed_chars = len(full_text)
        segment_text = textUtils.get_string_no_punctuation_or_emoji(raw)
        return segment_text or None

    def tts_text_priority_thread(self):
        """Streaming text processing thread."""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                # 跨轮防泄：与 base.py 的默认实现保持一致——
                # 1) client_abort 期间丢弃所有待合成文本
                # 2) sentence_id 不属于当前活跃轮次的文本一并丢弃
                # 否则旧轮的 LLM 文本会在新轮开始后继续被合成并推流。
                if self.conn.client_abort:
                    # 丢弃的同时清缓冲，否则下一轮会把上一轮的残文一起合成
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    continue
                if message.sentence_id and message.sentence_id != self.conn.sentence_id:
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    continue
                # 标记当前活跃轮次：handle_opus / text_to_speak 的音频入队都靠这个 tag，
                # 这样 _audio_play_priority_thread 的 sentence_id 过滤才能识别 OVS 产出。
                if message.sentence_id:
                    self.current_sentence_id = message.sentence_id
                if message.sentence_type == SentenceType.FIRST:
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    # 与 base.py:408 对齐：新一轮的首句要走首句规则（尽早出声），
                    # 缺了它 OVS 子类会把整轮都当「后续句」缓冲，首音频被拖慢。
                    self.is_first_sentence = True
                    self.before_stop_play_files.clear()
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)
                elif ContentType.FILE == message.content_type:
                    logger.bind(tag=TAG).info(
                        f"Adding audio file to playlist: {message.content_file}"
                    )
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(
                                audio_data, message.content_detail
                            ),
                        )

                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(True)

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"TTS text processing failed: {str(e)}, type: {type(e).__name__}, stack: {traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars:]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                # processed_chars 是「已消费的字符数」，不是增量：这里原先用
                # += 会在多次调用后越界，后续文本被整段跳过。
                self.processed_chars = len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        try:
            text = MarkdownCleaner.clean_markdown(text)
            try:
                succeeded = asyncio.run(self.text_to_speak(text, is_last))
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"TTS generation failed: {text}, error: {e}"
                )
                succeeded = False

            if succeeded:
                logger.bind(tag=TAG).info(
                    f"TTS generation success: {text}"
                )
            else:
                logger.bind(tag=TAG).error(
                    f"TTS generation failed: {text}, please check network or service"
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS: {e}")
        finally:
            return None

    def _auth_headers(self) -> dict:
        """OVS 的鉴权头。OVS_API_KEYS 未启用时为空。"""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _make_stop_checker(self, sid):
        """返回一个「这条流是否该停」的判定函数。

        都是简单 bool 读，跨线程直接读即可，不需要锁。
        """

        def _stopped():
            if self._closed:
                return True
            conn = self.conn
            if conn is None:
                return False
            if getattr(conn, "client_abort", False):
                return True
            stop_event = getattr(conn, "stop_event", None)
            if stop_event is not None and stop_event.is_set():
                return True
            if sid and sid != getattr(conn, "sentence_id", sid):
                return True
            return False

        return _stopped

    async def _post_with_retry(self, session, payload, stopped=None):
        """POST /tts/stream，对两类可恢复状态退避重试：

        - 503：OVS 正在热重载后端
        - 429：会话槽满（``too_many_sessions``）。OVS 会带 ``Retry-After`` 头。
          单 worker 的后端（所有 RK 设备、以及任何未覆写 max_concurrent 的后端）
          一旦被卡住的请求占住唯一 slot，后续全是 429 —— 不退避重试就会整条
          TTS 链路一起崩掉。

        返回 response；最终失败返回 None。
        """
        delay = 0.1
        max_delay = self.retry_max_delay
        max_retries = self.tts_max_retries
        # 整个重试阶段（含请求与读错误正文）共享一个 deadline。槽满是常态而非
        # 偶发，等待时间必须有硬上界，否则这一轮的回答被整段堵死。
        deadline = asyncio.get_event_loop().time() + self.retry_budget_seconds
        for attempt in range(max_retries + 1):
            if stopped is not None and stopped():
                return None
            if asyncio.get_event_loop().time() >= deadline:
                logger.bind(tag=TAG).warning(
                    f"TTS retry budget {self.retry_budget_seconds}s exhausted"
                )
                return None
            resp = await session.post(
                self.api_url, json=payload, headers=self._auth_headers()
            )
            if resp.status not in (503, 429):
                if resp.status == 401:
                    body = await resp.text()
                    await resp.release()
                    logger.bind(tag=TAG).error(
                        f"TTS 返回 401：服务端开启了 OVS_API_KEYS，"
                        f"但本地 TTS 配置里的 api_key 为空或不正确。body={body[:200]}"
                    )
                    return None
                return resp

            status = resp.status
            retry_after = resp.headers.get("Retry-After")
            body = await resp.text()
            await resp.release()

            if attempt == max_retries:
                logger.bind(tag=TAG).error(
                    f"TTS still unavailable after {max_retries} retries: "
                    f"{status}, body={body[:200]}"
                )
                return None

            sleep_for = delay
            if status == 429 and retry_after:
                try:
                    # Retry-After 是服务端的明确指示，优先于我们的退避曲线，
                    # 但仍夹在 max_delay 内，避免一个离谱的值把这一句挂死。
                    sleep_for = min(float(retry_after), max_delay)
                except (TypeError, ValueError):
                    pass

            reason = "hot-reloading" if status == 503 else "session slots full"
            logger.bind(tag=TAG).warning(
                f"TTS {reason}: {status}, retry={attempt + 1}/{max_retries}, "
                f"sleep={sleep_for:.1f}s"
            )
            # 分片睡，睡的过程中设备可能已经断开/换轮，没必要再回来发请求
            loop = asyncio.get_event_loop()
            sleep_for = min(sleep_for, max(0.0, deadline - loop.time()))
            slept = 0.0
            while slept < sleep_for:
                if stopped is not None and stopped():
                    return None
                step = min(0.1, sleep_for - slept)
                await asyncio.sleep(step)
                slept += step
            delay = min(delay * 2, max_delay)
        return None

    async def _fetch_capabilities(self):
        """Probe /tts/capabilities at startup; fill self.available_speakers + log model_id."""
        url = f"{self.base_url}/tts/capabilities"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    if resp.status == 401:
                        logger.bind(tag=TAG).error(
                            "OVS TTS 返回 401：服务端开启了 OVS_API_KEYS，"
                            "但本地 TTS 配置里的 api_key 为空或不正确"
                        )
                        return
                    if resp.status == 503:
                        logger.bind(tag=TAG).warning(
                            "OVS TTS hot-reload in progress at startup; capabilities skipped"
                        )
                        return
                    if resp.status != 200:
                        logger.bind(tag=TAG).warning(
                            f"TTS capabilities unavailable: status={resp.status}"
                        )
                        return
                    data = await resp.json()
            speakers = data.get("speakers") or []
            self.available_speakers = {
                int(s["id"]): s for s in speakers if isinstance(s, dict) and "id" in s
            }
            logger.bind(tag=TAG).info(
                f"OVS TTS capabilities: model_id={data.get('model_id')!r} "
                f"backend={data.get('backend')!r} speakers={sorted(self.available_speakers.keys())}"
            )
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"TTS capabilities probe failed: {exc}")

    async def text_to_speak(self, text, is_last=False):
        """Stream TTS audio. First 4 bytes of body are LE uint32 sample rate."""
        payload = {"text": text}
        if self.speed is not None:
            payload["speed"] = self.speed
        # Speaker priority: embedding > speaker_id > legacy sid
        if self.speaker_embedding_b64 is not None:
            payload["speaker_embedding_b64"] = self.speaker_embedding_b64
        elif self.speaker_id is not None:
            payload["speaker_id"] = self.speaker_id
        elif self.sid is not None:
            payload["sid"] = self.sid
        if self.pitch is not None:
            payload["pitch"] = self.pitch
        if self.language is not None:
            payload["language"] = self.language

        # 这条流属于哪一轮。设备断开 / 打断 / 换轮之后，在途的 HTTP 流必须立刻
        # 放手——OVS 只有 2 个全局会话槽，一条合成完才释放意味着下一轮开口时
        # 槽还被占着，直接 429。
        sid = self.current_sentence_id
        _stopped = self._make_stop_checker(sid)

        if _stopped():
            logger.bind(tag=TAG).info("TTS aborted before request (stale round)")
            return False

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                resp = await self._post_with_retry(session, payload, stopped=_stopped)
                if resp is None:
                    if _stopped():
                        # 中止路径：收尾由上层（abort / 新一轮）负责，这里再补
                        # 一个 LAST 只会给已经作废的轮次多推一帧。
                        return False
                    self.tts_audio_queue.put((SentenceType.LAST, [], None, self.current_sentence_id))
                    return False
                async with resp:
                    if resp.status != 200:
                        logger.bind(tag=TAG).error(
                            f"TTS request failed: {resp.status}, {await resp.text()}"
                        )
                        self.tts_audio_queue.put((SentenceType.LAST, [], None, self.current_sentence_id))
                        return False

                    self.pcm_buffer.clear()
                    self.tts_audio_queue.put((SentenceType.FIRST, [], text, self.current_sentence_id))

                    # ---- Parse leading 4-byte LE sample rate header ----
                    header_buf = bytearray()
                    sample_rate = None
                    frame_bytes = None

                    # 用带短超时的轮询读代替 `async for`：后者会一直挂在
                    # readany() 上，设备断开后仍把整句合成完才释放会话槽。
                    aborted = False
                    while True:
                        if _stopped():
                            aborted = True
                            break
                        try:
                            chunk = await asyncio.wait_for(
                                resp.content.readany(), timeout=0.25
                            )
                        except asyncio.TimeoutError:
                            continue
                        data = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
                        if not data:
                            break  # EOF

                        # Accumulate header until we have 4 bytes
                        if sample_rate is None:
                            header_buf.extend(data)
                            if len(header_buf) < 4:
                                continue
                            sample_rate = struct.unpack("<I", bytes(header_buf[:4]))[0]
                            self._ensure_encoder(sample_rate)
                            frame_bytes = int(
                                self.opus_encoder.sample_rate
                                * self.opus_encoder.channels
                                * self.opus_encoder.frame_size_ms
                                / 1000
                                * 2  # 16-bit
                            )
                            # Remainder of header_buf after the 4-byte SR is PCM
                            data = bytes(header_buf[4:])
                            header_buf = bytearray()
                            if not data:
                                continue
                        # ----------------------------------------------------

                        self.pcm_buffer.extend(data)

                        while len(self.pcm_buffer) >= frame_bytes:
                            frame = bytes(self.pcm_buffer[:frame_bytes])
                            del self.pcm_buffer[:frame_bytes]
                            self.opus_encoder.encode_pcm_to_opus_stream(
                                frame, end_of_stream=False, callback=self.handle_opus
                            )

                    if aborted:
                        # 不 flush 尾音、不收尾播放文件：这一轮已经作废。
                        # 退出 `async with resp` 会关掉连接，OVS 侧随即释放槽。
                        self.pcm_buffer.clear()
                        logger.bind(tag=TAG).info(
                            "TTS stream aborted mid-flight; releasing OVS session slot"
                        )
                        return False

                    # Flush
                    if self.pcm_buffer and self.opus_encoder is not None:
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            bytes(self.pcm_buffer),
                            end_of_stream=True,
                            callback=self.handle_opus,
                        )
                        self.pcm_buffer.clear()

                    if is_last:
                        self._process_before_stop_play_files()

                    return True

        except Exception as e:
            if _stopped():
                logger.bind(tag=TAG).info(f"TTS stream aborted ({e})")
                self.pcm_buffer.clear()
                return False
            logger.bind(tag=TAG).error(f"TTS request exception: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None, self.current_sentence_id))
            return False

    async def close(self):
        # 先置位，让在途流在下一个 0.25s 轮询点自行退出
        self._closed = True
        await super().close()
        if self.opus_encoder is not None:
            try:
                self.opus_encoder.close()
            except Exception:
                pass
            self.opus_encoder = None
