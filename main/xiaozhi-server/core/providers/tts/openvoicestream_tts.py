import os
import struct
import queue
import threading
import aiohttp
import asyncio
import traceback
from typing import Optional
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
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
        self.base_url = config.get("base_url", "http://127.0.0.1:8000")
        self.api_url = f"{self.base_url}/tts/stream"
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
        language = config.get("language", None)
        self.language = language if language else None
        self.timeout = config.get("timeout", 30)
        self.audio_format = "pcm"
        self.before_stop_play_files = []

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
                    continue
                if message.sentence_id and message.sentence_id != self.conn.sentence_id:
                    continue
                # 标记当前活跃轮次：handle_opus / text_to_speak 的音频入队都靠这个 tag，
                # 这样 _audio_play_priority_thread 的 sentence_id 过滤才能识别 OVS 产出。
                if message.sentence_id:
                    self.current_sentence_id = message.sentence_id
                if message.sentence_type == SentenceType.FIRST:
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
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
                self.processed_chars += len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        try:
            max_repeat_time = 5
            text = MarkdownCleaner.clean_markdown(text)
            try:
                asyncio.run(self.text_to_speak(text, is_last))
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"TTS generation failed {5 - max_repeat_time + 1} times: {text}, error: {e}"
                )
                max_repeat_time -= 1

            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(
                    f"TTS generation success: {text}, retries: {5 - max_repeat_time}"
                )
            else:
                logger.bind(tag=TAG).error(
                    f"TTS generation failed: {text}, please check network or service"
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS: {e}")
        finally:
            return None

    async def _post_with_503_retry(self, session, payload):
        """POST to /tts/stream with 503 (hot-reload) retry; returns response or None on final failure."""
        delay = 0.1
        max_delay = 5.0
        max_retries = 3
        for attempt in range(max_retries + 1):
            resp = await session.post(self.api_url, json=payload)
            if resp.status != 503:
                return resp
            body = await resp.text()
            await resp.release()
            if attempt == max_retries:
                logger.bind(tag=TAG).error(
                    f"TTS still unavailable after {max_retries} retries: 503, body={body[:200]}"
                )
                return None
            logger.bind(tag=TAG).warning(
                f"TTS hot-reloading: 503, retry={attempt + 1}/{max_retries}, sleep={delay:.1f}s"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
        return None

    async def _fetch_capabilities(self):
        """Probe /tts/capabilities at startup; fill self.available_speakers + log model_id."""
        url = f"{self.base_url}/tts/capabilities"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
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

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                resp = await self._post_with_503_retry(session, payload)
                if resp is None:
                    self.tts_audio_queue.put((SentenceType.LAST, [], None, getattr(self, 'current_sentence_id', None)))
                    return
                async with resp:
                    if resp.status != 200:
                        logger.bind(tag=TAG).error(
                            f"TTS request failed: {resp.status}, {await resp.text()}"
                        )
                        self.tts_audio_queue.put((SentenceType.LAST, [], None, getattr(self, 'current_sentence_id', None)))
                        return

                    self.pcm_buffer.clear()
                    self.tts_audio_queue.put((SentenceType.FIRST, [], text, getattr(self, 'current_sentence_id', None)))

                    # ---- Parse leading 4-byte LE sample rate header ----
                    header_buf = bytearray()
                    sample_rate = None
                    frame_bytes = None

                    async for chunk in resp.content.iter_any():
                        data = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
                        if not data:
                            continue

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

        except Exception as e:
            logger.bind(tag=TAG).error(f"TTS request exception: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None, getattr(self, 'current_sentence_id', None)))

    async def close(self):
        await super().close()
        if self.opus_encoder is not None:
            try:
                self.opus_encoder.close()
            except Exception:
                pass
            self.opus_encoder = None
