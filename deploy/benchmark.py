#!/usr/bin/env python3
"""
Xiaozhi Server 全链路性能测试工具

测试各阶段耗时:
  1. LLM: TTFT (Time To First Token) + 总生成时间 + tok/s
  2. TTS: 每句合成时间 + RTF (Real Time Factor)
  3. E2E: 从发送文本到收到第一帧音频 / 全部音频

用法:
  python3 benchmark.py                    # 测试当前配置的 LLM
  python3 benchmark.py --llm mlc          # 测试 MLC 后端
  python3 benchmark.py --llm ollama       # 测试 Ollama 后端
  python3 benchmark.py --llm all          # 对比两个后端
  python3 benchmark.py --rounds 5         # 每项测试跑5轮
  python3 benchmark.py --e2e              # 包含 E2E WebSocket 测试
"""

import argparse
import asyncio
import json
import time
import sys
import os
from dataclasses import dataclass, field
from typing import List, Optional

# ============ 配置 ============
LLM_BACKENDS = {
    "mlc": {
        "base_url": "http://192.168.10.35:8000/v1",
        "model": "qwen3:8b",
        "api_key": "mlc",
    },
    "ollama": {
        "base_url": "http://192.168.10.35:11434/v1",
        "model": "qwen3:8b-chat",
        "api_key": "ollama",
    },
}

XIAOZHI_WS_URL = "ws://192.168.10.179:18000/xiaozhi/v1/"

TEST_PROMPTS = [
    {"name": "short_zh", "text": "你好", "desc": "短句中文"},
    {"name": "medium_zh", "text": "请用三句话介绍一下人工智能", "desc": "中等中文"},
    {"name": "short_en", "text": "Hello, how are you?", "desc": "短句英文"},
    {"name": "tool_call", "text": "现在几点了", "desc": "工具调用"},
]

TTS_TEST_SENTENCES = [
    "你好，我是小智。",
    "今天天气真不错，适合出去走走。",
    "Hello, nice to meet you!",
    "人工智能正在改变我们的生活方式。",
]


@dataclass
class ASRResult:
    text_input: str  # Text used to generate test audio
    audio_duration: float  # Duration of test audio (seconds)
    inference_time: float  # ASR inference time (seconds)
    rtf: float  # Real Time Factor (inference_time / audio_duration)
    recognized_text: str  # ASR output


@dataclass
class LLMResult:
    prompt: str
    ttft: float  # Time to first token (seconds)
    total_time: float  # Total generation time
    tokens: int  # Number of tokens generated
    tok_per_sec: float  # Tokens per second
    content: str  # Generated content (truncated)


@dataclass
class TTSResult:
    text: str
    synthesis_time: float  # Seconds to synthesize
    audio_duration: float  # Audio duration in seconds
    rtf: float  # Real Time Factor (synthesis_time / audio_duration)
    audio_size: int  # Bytes


@dataclass
class E2EResult:
    text: str
    time_to_stt_echo: float
    time_to_first_audio: float
    time_to_last_audio: float
    total_audio_frames: int
    llm_backend: str


# ============ LLM Benchmark ============
def benchmark_llm(backend_name: str, prompt_text: str, tools: list = None) -> LLMResult:
    """Benchmark LLM streaming response."""
    try:
        import openai
    except ImportError:
        print("ERROR: pip install openai")
        sys.exit(1)

    cfg = LLM_BACKENDS[backend_name]
    client = openai.OpenAI(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        timeout=60,
    )

    messages = [
        {"role": "system", "content": "你是小智，一个智能语音助手。请用简洁的语言回答问题。"},
        {"role": "user", "content": prompt_text},
    ]

    params = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "max_tokens": 256,
    }
    if tools:
        params["tools"] = tools

    t_start = time.perf_counter()
    ttft = None
    tokens = 0
    content_parts = []

    stream = client.chat.completions.create(**params)
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        c = getattr(delta, "content", "") or ""
        if c:
            if ttft is None:
                ttft = time.perf_counter() - t_start
            tokens += 1
            content_parts.append(c)

    total_time = time.perf_counter() - t_start
    if ttft is None:
        ttft = total_time

    content = "".join(content_parts)
    tok_per_sec = tokens / (total_time - ttft) if total_time > ttft else 0

    return LLMResult(
        prompt=prompt_text,
        ttft=ttft,
        total_time=total_time,
        tokens=tokens,
        tok_per_sec=tok_per_sec,
        content=content[:80],
    )


# ============ ASR Benchmark ============
def benchmark_asr(text: str) -> Optional[ASRResult]:
    """Benchmark ASR inference inside the container.
    Uses TTS to generate test audio, then runs ASR on it.
    """
    escaped_text = text.replace('"', '\\"')
    script = f'''
import time, sys, os, json
sys.path.insert(0, "/opt/xiaozhi-esp32-server")
os.chdir("/opt/xiaozhi-esp32-server")

import sherpa_onnx, numpy as np, wave

# --- Step 1: Generate test audio with TTS ---
model_dir = "models/vits-melo-tts-zh_en"
rule_fsts = ",".join(sorted(os.path.join(model_dir, f) for f in os.listdir(model_dir) if f.endswith(".fst")))
rule_fars = ",".join(sorted(os.path.join(model_dir, f) for f in os.listdir(model_dir) if f.endswith(".far")))

tts_config = sherpa_onnx.OfflineTtsConfig(
    model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(
            model=os.path.join(model_dir, "model.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            lexicon=os.path.join(model_dir, "lexicon.txt"),
            dict_dir=os.path.join(model_dir, "dict"),
            data_dir="",
        ),
        num_threads=4,
        provider="cpu",
    ),
    rule_fsts=rule_fsts,
    rule_fars=rule_fars,
    max_num_sentences=2,
)
tts = sherpa_onnx.OfflineTts(tts_config)

audio = tts.generate("{escaped_text}", sid=0, speed=1.0)
if not audio.samples:
    print(json.dumps({{"error": "TTS generated empty audio"}}))
    sys.exit(0)

# Save as 16kHz WAV for ASR (resample from TTS sample rate)
tts_sr = tts.sample_rate
asr_sr = 16000
samples = np.array(audio.samples, dtype=np.float32)

# Resample if needed
if tts_sr != asr_sr:
    duration = len(samples) / tts_sr
    new_len = int(duration * asr_sr)
    indices = np.linspace(0, len(samples) - 1, new_len)
    samples_16k = np.interp(indices, np.arange(len(samples)), samples).astype(np.float32)
else:
    samples_16k = samples
    duration = len(samples) / asr_sr

audio_duration = len(samples_16k) / asr_sr

# Save WAV
wav_path = "/tmp/asr_bench_test.wav"
pcm = (samples_16k * 32767).astype(np.int16)
with wave.open(wav_path, "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(asr_sr)
    wf.writeframes(pcm.tobytes())

# --- Step 2: Run ASR ---
asr_model_dir = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
asr_model_path = os.path.join(asr_model_dir, "model.int8.onnx")
asr_tokens_path = os.path.join(asr_model_dir, "tokens.txt")

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=asr_model_path,
    tokens=asr_tokens_path,
    num_threads=2,
    sample_rate=16000,
    feature_dim=80,
    decoding_method="greedy_search",
    debug=False,
    use_itn=True,
)

# Read WAV back
with wave.open(wav_path, "rb") as wf:
    assert wf.getnchannels() == 1
    assert wf.getsampwidth() == 2
    n_frames = wf.getnframes()
    data = wf.readframes(n_frames)
    samples_asr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

# Inference timing
t0 = time.perf_counter()
s = recognizer.create_stream()
s.accept_waveform(16000, samples_asr)
recognizer.decode_stream(s)
inference_time = time.perf_counter() - t0

recognized = s.result.text.strip()
rtf = inference_time / audio_duration if audio_duration > 0 else 0

print(json.dumps({{
    "audio_duration": audio_duration,
    "inference_time": inference_time,
    "rtf": rtf,
    "recognized_text": recognized
}}))

os.remove(wav_path)
'''
    import subprocess
    import tempfile

    # Write script to temp file to avoid encoding issues
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(script)
        script_path = f.name

    try:
        # Copy script to container
        subprocess.run(["docker", "cp", script_path, "xiaozhi-server:/tmp/asr_bench.py"],
                      capture_output=True, timeout=10)
        # Execute script in container
        result = subprocess.run(
            ["docker", "exec", "xiaozhi-server", "python3", "/tmp/asr_bench.py"],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        os.unlink(script_path)

    if result.returncode != 0:
        print(f"  ASR ERROR: {result.stderr[:200]}")
        return None

    try:
        data = json.loads(result.stdout.strip())
        if "error" in data:
            print(f"  ASR ERROR: {data['error']}")
            return None
        return ASRResult(
            text_input=text,
            audio_duration=data["audio_duration"],
            inference_time=data["inference_time"],
            rtf=data["rtf"],
            recognized_text=data["recognized_text"],
        )
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  ASR parse error: {e}, stdout={result.stdout[:200]}")
        return None


# ============ TTS Benchmark ============
def benchmark_tts(text: str) -> Optional[TTSResult]:
    """Benchmark TTS synthesis inside the container."""
    # This runs inside the container via docker exec
    escaped_text = text.replace('"', '\\"')
    script = f'''
import time, sys, os
sys.path.insert(0, "/opt/xiaozhi-esp32-server")
os.chdir("/opt/xiaozhi-esp32-server")

import sherpa_onnx, numpy as np, wave, io, json

model_dir = "models/vits-melo-tts-zh_en"
rule_fsts = ",".join(sorted(os.path.join(model_dir, f) for f in os.listdir(model_dir) if f.endswith(".fst")))
rule_fars = ",".join(sorted(os.path.join(model_dir, f) for f in os.listdir(model_dir) if f.endswith(".far")))

tts_config = sherpa_onnx.OfflineTtsConfig(
    model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(
            model=os.path.join(model_dir, "model.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            lexicon=os.path.join(model_dir, "lexicon.txt"),
            dict_dir=os.path.join(model_dir, "dict"),
            data_dir="",
        ),
        num_threads=4,
        provider="cpu",
    ),
    rule_fsts=rule_fsts,
    rule_fars=rule_fars,
    max_num_sentences=2,
)
tts = sherpa_onnx.OfflineTts(tts_config)
sr = tts.sample_rate

text = """{escaped_text}"""

t0 = time.perf_counter()
audio = tts.generate(text, sid=0, speed=1.0)
synthesis_time = time.perf_counter() - t0

if audio.samples:
    n_samples = len(audio.samples)
    audio_duration = n_samples / sr
    pcm = (np.array(audio.samples, dtype=np.float32) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    audio_size = buf.tell()
    rtf = synthesis_time / audio_duration if audio_duration > 0 else 0
    print(json.dumps({{"synthesis_time": synthesis_time, "audio_duration": audio_duration, "rtf": rtf, "audio_size": audio_size}}))
else:
    print(json.dumps({{"error": "empty audio"}}))
'''
    import subprocess
    import tempfile

    # Write script to temp file to avoid encoding issues
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(script)
        script_path = f.name

    try:
        # Copy script to container
        subprocess.run(["docker", "cp", script_path, "xiaozhi-server:/tmp/tts_bench.py"],
                      capture_output=True, timeout=10)
        # Execute script in container
        result = subprocess.run(
            ["docker", "exec", "xiaozhi-server", "python3", "/tmp/tts_bench.py"],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        os.unlink(script_path)

    if result.returncode != 0:
        print(f"  TTS ERROR: {result.stderr[:200]}")
        return None

    try:
        data = json.loads(result.stdout.strip())
        if "error" in data:
            print(f"  TTS ERROR: {data['error']}")
            return None
        return TTSResult(
            text=text,
            synthesis_time=data["synthesis_time"],
            audio_duration=data["audio_duration"],
            rtf=data["rtf"],
            audio_size=data["audio_size"],
        )
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  TTS parse error: {e}, stdout={result.stdout[:200]}")
        return None


# ============ E2E Benchmark ============
async def benchmark_e2e(text: str, llm_backend: str) -> Optional[E2EResult]:
    """Benchmark full pipeline via WebSocket."""
    try:
        import websockets
    except ImportError:
        print("ERROR: pip install websockets")
        return None

    device_id = f"bench-{int(time.time())}"
    headers = {"device-id": device_id}

    try:
        async with websockets.connect(XIAOZHI_WS_URL, additional_headers=headers) as ws:
            # Hello
            hello = {"type": "hello", "device_id": device_id, "device_name": "Benchmark", "device_mac": device_id}
            await ws.send(json.dumps(hello))
            resp = await asyncio.wait_for(ws.recv(), timeout=10)

            # Short wait for server initialization (reduced from 4s for faster response)
            await asyncio.sleep(0.5)

            # Send text
            listen = {"type": "listen", "state": "detect", "text": text}
            t_start = time.perf_counter()
            await ws.send(json.dumps(listen))

            time_stt = None
            time_first_audio = None
            time_last_audio = None
            audio_frames = 0

            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    elapsed = time.perf_counter() - t_start

                    if isinstance(msg, str):
                        data = json.loads(msg)
                        mtype = data.get("type", "")
                        state = data.get("state", "")
                        if mtype == "stt" and time_stt is None:
                            time_stt = elapsed
                        if mtype == "tts" and state == "stop":
                            time_last_audio = elapsed
                            break
                    else:
                        audio_frames += 1
                        if time_first_audio is None:
                            time_first_audio = elapsed
                        time_last_audio = elapsed

                except asyncio.TimeoutError:
                    print(f"  TIMEOUT: stt={time_stt}, audio={time_first_audio}, frames={audio_frames}")
                    break

            return E2EResult(
                text=text,
                time_to_stt_echo=time_stt or 0,
                time_to_first_audio=time_first_audio or 0,
                time_to_last_audio=time_last_audio or 0,
                total_audio_frames=audio_frames,
                llm_backend=llm_backend,
            )
    except Exception as e:
        print(f"  E2E ERROR: {e}")
        return None


# ============ Report ============
def print_llm_report(results: List[LLMResult], backend: str):
    print(f"\n{'='*60}")
    print(f"  LLM Benchmark: {backend.upper()}")
    print(f"  Backend: {LLM_BACKENDS[backend]['base_url']}")
    print(f"  Model: {LLM_BACKENDS[backend]['model']}")
    print(f"{'='*60}")
    print(f"{'Prompt':<20} {'TTFT':>8} {'Total':>8} {'Tokens':>7} {'tok/s':>7}")
    print(f"{'-'*60}")
    for r in results:
        print(f"{r.prompt[:20]:<20} {r.ttft*1000:>7.0f}ms {r.total_time*1000:>7.0f}ms {r.tokens:>7} {r.tok_per_sec:>6.1f}")
    avg_ttft = sum(r.ttft for r in results) / len(results) if results else 0
    avg_tps = sum(r.tok_per_sec for r in results) / len(results) if results else 0
    print(f"{'-'*60}")
    print(f"{'Average':<20} {avg_ttft*1000:>7.0f}ms {'':>8} {'':>7} {avg_tps:>6.1f}")


def print_tts_report(results: List[TTSResult]):
    print(f"\n{'='*60}")
    print(f"  TTS Benchmark: Sherpa-ONNX MeloTTS (4 threads)")
    print(f"{'='*60}")
    print(f"{'Text':<25} {'Synth':>8} {'Duration':>9} {'RTF':>6} {'Size':>8}")
    print(f"{'-'*60}")
    for r in results:
        print(f"{r.text[:25]:<25} {r.synthesis_time*1000:>7.0f}ms {r.audio_duration:>8.2f}s {r.rtf:>5.2f} {r.audio_size//1024:>6}KB")
    avg_rtf = sum(r.rtf for r in results) / len(results) if results else 0
    print(f"{'-'*60}")
    print(f"{'Average RTF':<25} {'':>8} {'':>9} {avg_rtf:>5.2f}")


def print_asr_report(results: List[ASRResult]):
    print(f"\n{'='*60}")
    print(f"  ASR Benchmark: Sherpa-ONNX SenseVoice (2 threads)")
    print(f"{'='*60}")
    print(f"{'Input Text':<20} {'Duration':>9} {'Inference':>10} {'RTF':>6} {'Recognized':<20}")
    print(f"{'-'*70}")
    for r in results:
        print(f"{r.text_input[:20]:<20} {r.audio_duration:>8.2f}s {r.inference_time*1000:>9.0f}ms {r.rtf:>5.2f} {r.recognized_text[:20]}")
    avg_rtf = sum(r.rtf for r in results) / len(results) if results else 0
    avg_inf = sum(r.inference_time for r in results) / len(results) if results else 0
    print(f"{'-'*70}")
    print(f"{'Average':<20} {'':>9} {avg_inf*1000:>9.0f}ms {avg_rtf:>5.2f}")


def print_e2e_report(results: List[E2EResult]):
    print(f"\n{'='*60}")
    print(f"  E2E Benchmark (WebSocket full pipeline)")
    print(f"{'='*60}")
    print(f"{'Text':<20} {'LLM':<8} {'STT':>7} {'1st Audio':>10} {'Last':>8} {'Frames':>7}")
    print(f"{'-'*60}")
    for r in results:
        print(f"{r.text[:20]:<20} {r.llm_backend:<8} {r.time_to_stt_echo*1000:>6.0f}ms {r.time_to_first_audio*1000:>9.0f}ms {r.time_to_last_audio*1000:>7.0f}ms {r.total_audio_frames:>7}")


def print_summary(llm_results: dict, tts_results: List[TTSResult], asr_results: List[ASRResult], e2e_results: List[E2EResult]):
    print(f"\n{'='*60}")
    print(f"  SUMMARY - Pipeline Latency Breakdown")
    print(f"{'='*60}")

    if asr_results:
        avg_inf = sum(r.inference_time for r in asr_results) / len(asr_results)
        avg_rtf = sum(r.rtf for r in asr_results) / len(asr_results)
        avg_dur = sum(r.audio_duration for r in asr_results) / len(asr_results)
        print(f"\n  ASR: Avg inference={avg_inf*1000:.0f}ms, RTF={avg_rtf:.2f}, Avg audio={avg_dur:.2f}s")

    for backend, results in llm_results.items():
        if results:
            avg_ttft = sum(r.ttft for r in results) / len(results)
            avg_total = sum(r.total_time for r in results) / len(results)
            avg_tps = sum(r.tok_per_sec for r in results) / len(results)
            print(f"  LLM ({backend}): TTFT={avg_ttft*1000:.0f}ms, Total={avg_total*1000:.0f}ms, {avg_tps:.1f} tok/s")

    if tts_results:
        avg_synth = sum(r.synthesis_time for r in tts_results) / len(tts_results)
        avg_rtf = sum(r.rtf for r in tts_results) / len(tts_results)
        print(f"  TTS: Avg synthesis={avg_synth*1000:.0f}ms, RTF={avg_rtf:.2f}")

    if e2e_results:
        avg_first = sum(r.time_to_first_audio for r in e2e_results) / len(e2e_results)
        avg_last = sum(r.time_to_last_audio for r in e2e_results) / len(e2e_results)
        print(f"  E2E: First audio={avg_first*1000:.0f}ms, Complete={avg_last*1000:.0f}ms")

    print(f"\n  Typical user-perceived latency (voice input):")
    asr_avg = sum(r.inference_time for r in asr_results) / len(asr_results) if asr_results else 0.3
    if llm_results:
        first_backend = list(llm_results.keys())[0]
        if llm_results[first_backend]:
            llm_ttft = sum(r.ttft for r in llm_results[first_backend]) / len(llm_results[first_backend])
            tts_avg = sum(r.synthesis_time for r in tts_results) / len(tts_results) if tts_results else 0.5
            print(f"    ASR + LLM TTFT + sentence accumulation + TTS synthesis")
            print(f"    ~{asr_avg*1000:.0f}ms + ~{llm_ttft*1000:.0f}ms + ~1000ms + ~{tts_avg*1000:.0f}ms")
            print(f"    = ~{(asr_avg + llm_ttft + 1.0 + tts_avg)*1000:.0f}ms to first audio")
    print()


# ============ Main ============
def main():
    parser = argparse.ArgumentParser(description="Xiaozhi Server Pipeline Benchmark")
    parser.add_argument("--llm", choices=["mlc", "ollama", "all"], default="mlc",
                        help="LLM backend to test (default: mlc)")
    parser.add_argument("--rounds", type=int, default=1, help="Number of rounds per test")
    parser.add_argument("--e2e", action="store_true", help="Include E2E WebSocket test")
    parser.add_argument("--no-tts", action="store_true", help="Skip TTS benchmark")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM benchmark")
    parser.add_argument("--no-asr", action="store_true", help="Skip ASR benchmark")
    args = parser.parse_args()

    backends = ["mlc", "ollama"] if args.llm == "all" else [args.llm]
    llm_results = {}
    tts_results = []
    asr_results = []
    e2e_results = []

    # LLM Benchmark
    if not args.no_llm:
        for backend in backends:
            print(f"\n--- Testing LLM: {backend} ---")
            results = []
            for r in range(args.rounds):
                for prompt in TEST_PROMPTS:
                    if prompt["name"] == "tool_call":
                        continue  # Skip tool_call for basic LLM test
                    print(f"  [{r+1}/{args.rounds}] {prompt['desc']}: {prompt['text'][:30]}...", end="", flush=True)
                    try:
                        result = benchmark_llm(backend, prompt["text"])
                        results.append(result)
                        print(f" TTFT={result.ttft*1000:.0f}ms, {result.tok_per_sec:.1f} tok/s")
                    except Exception as e:
                        print(f" ERROR: {e}")
            llm_results[backend] = results
            print_llm_report(results, backend)

    # ASR Benchmark
    if not args.no_asr:
        print(f"\n--- Testing ASR ---")
        asr_test_texts = [
            "你好，我是小智。",
            "今天天气真不错，适合出去走走。",
            "人工智能正在改变我们的生活方式。",
        ]
        for r in range(args.rounds):
            for sentence in asr_test_texts:
                print(f"  [{r+1}/{args.rounds}] {sentence[:30]}...", end="", flush=True)
                result = benchmark_asr(sentence)
                if result:
                    asr_results.append(result)
                    print(f" RTF={result.rtf:.2f}, {result.inference_time*1000:.0f}ms -> {result.recognized_text[:20]}")
                else:
                    print(" FAILED")
        if asr_results:
            print_asr_report(asr_results)

    # TTS Benchmark
    if not args.no_tts:
        print(f"\n--- Testing TTS ---")
        for r in range(args.rounds):
            for sentence in TTS_TEST_SENTENCES:
                print(f"  [{r+1}/{args.rounds}] {sentence[:30]}...", end="", flush=True)
                result = benchmark_tts(sentence)
                if result:
                    tts_results.append(result)
                    print(f" RTF={result.rtf:.2f}, {result.synthesis_time*1000:.0f}ms")
                else:
                    print(" FAILED")
        if tts_results:
            print_tts_report(tts_results)

    # E2E Benchmark
    if args.e2e:
        print(f"\n--- Testing E2E Pipeline ---")
        for backend in backends:
            for prompt in TEST_PROMPTS[:2]:  # Only short/medium
                print(f"  E2E [{backend}] {prompt['desc']}...", end="", flush=True)
                result = asyncio.run(benchmark_e2e(prompt["text"], backend))
                if result:
                    e2e_results.append(result)
                    print(f" 1st_audio={result.time_to_first_audio*1000:.0f}ms, total={result.time_to_last_audio*1000:.0f}ms")
                else:
                    print(" FAILED")
        if e2e_results:
            print_e2e_report(e2e_results)

    # Summary
    print_summary(llm_results, tts_results, asr_results, e2e_results)


if __name__ == "__main__":
    main()
