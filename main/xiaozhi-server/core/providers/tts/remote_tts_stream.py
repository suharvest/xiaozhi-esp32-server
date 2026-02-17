import os
import time
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
    """Remote Kokoro TTS with streaming support."""

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.base_url = config.get("base_url", "http://192.168.10.35:8000")
        self.api_url = f"{self.base_url}/tts/stream"
        self.sid = config.get("sid", 50)
        self.speed = config.get("speed", 1.0)
        self.timeout = config.get("timeout", 30)
        self.audio_format = "pcm"
        self.before_stop_play_files = []

        # Opus encoder for 24000Hz sample rate (Kokoro output)
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=24000, channels=1, frame_size_ms=60
        )

        # PCM buffer
        self.pcm_buffer = bytearray()

        logger.bind(tag=TAG).info(
            f"Remote Stream TTS initialized, endpoint={self.api_url}, sid={self.sid}"
        )

    def tts_text_priority_thread(self):
        """Streaming text processing thread."""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if message.sentence_type == SentenceType.FIRST:
                    # Initialize parameters
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
                    # Process remaining text
                    self._process_remaining_text_stream(True)

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"TTS text processing failed: {str(e)}, type: {type(e).__name__}, stack: {traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        """Process remaining text and generate speech."""
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
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
        """Stream TTS audio, pushing audio chunks as they arrive."""
        payload = {"text": text, "sid": self.sid, "speed": self.speed}

        # Frame size in bytes for Opus encoding (24000Hz, mono, 60ms)
        frame_bytes = int(
            self.opus_encoder.sample_rate
            * self.opus_encoder.channels
            * self.opus_encoder.frame_size_ms
            / 1000
            * 2  # 16-bit = 2 bytes
        )

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

                    # Skip WAV header (44 bytes) - only for first chunk
                    header_skipped = False

                    # Process streaming audio data
                    async for chunk in resp.content.iter_any():
                        data = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
                        if not data:
                            continue

                        # Skip WAV header on first chunk
                        if not header_skipped:
                            if len(data) > 44:
                                data = data[44:]
                            else:
                                # Header spans multiple chunks (rare)
                                continue
                            header_skipped = True

                        self.pcm_buffer.extend(data)

                        # Encode complete frames
                        while len(self.pcm_buffer) >= frame_bytes:
                            frame = bytes(self.pcm_buffer[:frame_bytes])
                            del self.pcm_buffer[:frame_bytes]

                            self.opus_encoder.encode_pcm_to_opus_stream(
                                frame, end_of_stream=False, callback=self.handle_opus
                            )

                    # Flush remaining data
                    if self.pcm_buffer:
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            bytes(self.pcm_buffer),
                            end_of_stream=True,
                            callback=self.handle_opus,
                        )
                        self.pcm_buffer.clear()

                    # If last segment, finish audio processing
                    if is_last:
                        self._process_before_stop_play_files()

        except Exception as e:
            logger.bind(tag=TAG).error(f"TTS request exception: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    async def close(self):
        """Resource cleanup."""
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
