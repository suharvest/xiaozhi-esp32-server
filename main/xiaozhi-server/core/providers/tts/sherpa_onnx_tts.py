import os
import io
import wave
import numpy as np
import sherpa_onnx
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        model_dir = config.get("model_dir", "models/vits-icefall-zh-aishell3")
        model_file = config.get("model_file", "model.onnx")
        tokens_file = config.get("tokens_file", "tokens.txt")
        lexicon_file = config.get("lexicon_file", "")
        dict_dir = config.get("dict_dir", "")
        data_dir = config.get("data_dir", "")
        num_threads = config.get("num_threads", 2)
        self.sid = config.get("sid", 0)
        self.speed = config.get("speed", 1.0)

        model_path = os.path.join(model_dir, model_file)
        tokens_path = os.path.join(model_dir, tokens_file)
        lexicon_path = os.path.join(model_dir, lexicon_file) if lexicon_file else ""
        dict_path = os.path.join(model_dir, dict_dir) if dict_dir else ""
        data_path = os.path.join(model_dir, data_dir) if data_dir else ""

        # Auto-discover FST/FAR rule files for text normalization
        rule_fsts = ",".join(sorted(
            os.path.join(model_dir, f)
            for f in os.listdir(model_dir) if f.endswith(".fst")
        ))
        rule_fars = ",".join(sorted(
            os.path.join(model_dir, f)
            for f in os.listdir(model_dir) if f.endswith(".far")
        ))

        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=model_path,
                    tokens=tokens_path,
                    lexicon=lexicon_path,
                    dict_dir=dict_path,
                    data_dir=data_path,
                ),
                num_threads=num_threads,
                provider="cpu",
            ),
            rule_fsts=rule_fsts,
            rule_fars=rule_fars,
            max_num_sentences=2,
        )
        self.tts = sherpa_onnx.OfflineTts(tts_config)
        self.sample_rate = self.tts.sample_rate
        logger.bind(tag=TAG).info(
            f"Sherpa-ONNX TTS initialized, model={model_path}, sample_rate={self.sample_rate}"
        )

    async def text_to_speak(self, text, output_file):
        audio = self.tts.generate(text, sid=self.sid, speed=self.speed)
        if not audio.samples:
            return None

        samples = np.array(audio.samples, dtype=np.float32)
        pcm_data = (samples * 32767).astype(np.int16)

        if output_file:
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            with wave.open(output_file, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                wf.writeframes(pcm_data.tobytes())
            return output_file
        else:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                wf.writeframes(pcm_data.tobytes())
            return buf.getvalue()
