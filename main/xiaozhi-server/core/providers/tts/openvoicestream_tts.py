import os
import struct
import queue
import aiohttp
import asyncio
import traceback
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase
from core.utils import opus_encoder_utils, textUtils
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()


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
        self.sid = config.get("sid", 0)
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
            f"OpenVoiceStream TTS initialized, endpoint={self.api_url}, sid={self.sid}"
        )

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

    async def text_to_speak(self, text, is_last=False):
        """Stream TTS audio. First 4 bytes of body are LE uint32 sample rate."""
        payload = {"text": text, "sid": self.sid, "speed": self.speed}
        if self.pitch is not None:
            payload["pitch"] = self.pitch
        if self.language is not None:
            payload["language"] = self.language

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.api_url, json=payload) as resp:
                    if resp.status != 200:
                        logger.bind(tag=TAG).error(
                            f"TTS request failed: {resp.status}, {await resp.text()}"
                        )
                        self.tts_audio_queue.put((SentenceType.LAST, [], None))
                        return

                    self.pcm_buffer.clear()
                    self.tts_audio_queue.put((SentenceType.FIRST, [], text))

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
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    async def close(self):
        await super().close()
        if self.opus_encoder is not None:
            try:
                self.opus_encoder.close()
            except Exception:
                pass
            self.opus_encoder = None
