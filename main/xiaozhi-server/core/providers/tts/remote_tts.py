import os
import httpx
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.base_url = config.get("base_url", "http://192.168.10.35:8000")
        self.sid = config.get("sid", 0)
        self.speed = config.get("speed", 1.0)
        self.timeout = config.get("timeout", 10)
        self.client = httpx.Client(timeout=self.timeout)
        logger.bind(tag=TAG).info(
            f"Remote TTS initialized, endpoint={self.base_url}/tts"
        )

    async def text_to_speak(self, text, output_file):
        try:
            resp = self.client.post(
                f"{self.base_url}/tts",
                json={"text": text, "sid": self.sid, "speed": self.speed},
            )
            if resp.status_code != 200:
                logger.bind(tag=TAG).error(
                    f"Remote TTS error: status={resp.status_code}, body={resp.text[:100]}"
                )
                return None

            wav_bytes = resp.content
            logger.bind(tag=TAG).debug(
                f"Remote TTS ok: text={text[:20]}, size={len(wav_bytes)}"
            )

            if output_file:
                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                with open(output_file, "wb") as f:
                    f.write(wav_bytes)
                return output_file

            return wav_bytes

        except Exception as e:
            logger.bind(tag=TAG).error(f"Remote TTS request failed: {e}")
            return None
