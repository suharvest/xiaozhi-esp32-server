import json
import asyncio
import aiohttp
import websockets
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    """Streaming ASR provider for OpenVoiceStream WebSocket service.

    Endpoint: ws://host:8000/asr/stream?sample_rate=...&language=...
      - Client sends int16 PCM binary frames, or JSON commands like
        {"command":"reset"|"end_utterance"}.
      - Server sends JSON: {"type":"partial"|"final","text":"...",
                            "is_final":bool,"is_stable":bool}.

    Lifecycle: one WebSocket per utterance. Connection is established when
    the first voiced frame of a new utterance arrives, and torn down after
    handle_voice_stop completes (or on error).
    """

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config or {}
        self.text = ""

        # 默认端口 8621：OVS 容器内监听 8000，但对外发布的宿主端口是 8621
        # （deploy/docker-compose.yml 的 "8621:8000"）。写 8000 会让留空的配置
        # 静默连不上。
        self.ws_url_base = self.config.get(
            "ws_url", "ws://127.0.0.1:8621/asr/stream"
        )
        # OVS_API_KEYS 启用时必须带；留空表示服务端未开鉴权。
        self.api_key = (self.config.get("api_key") or "").strip()
        self.sample_rate = int(self.config.get("sample_rate", 16000))
        self.language = self.config.get("language", "auto")
        self.final_timeout = float(self.config.get("final_timeout", 5.0))
        self.partial_results = bool(self.config.get("partial_results", False))
        self.fallback_to_partial = bool(
            self.config.get("fallback_to_partial", True)
        )
        self.allow_backend_endpoint = bool(
            self.config.get("allow_backend_endpoint", True)
        )
        self.output_dir = self.config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        # Per-utterance state
        self.asr_ws = None
        self.receiver_task = None
        self.is_processing = False
        self.last_partial = ""
        self._final_event: Optional[asyncio.Event] = None
        # 引擎自行断句后已确认的文本。OVS 的 VAD 一旦在句中触发（说话有停顿就会），
        # 它会结算当前段并**在自己那边开新的一段**；后续 EOF 只结算新段。不累加的话
        # 之前所有段的文本都会丢掉，只剩最后一小段。
        self._committed_text = ""
        # Becomes True once we explicitly send end_utterance — only after that
        # do we honour a final from the backend (unless allow_backend_endpoint).
        self._stop_sent = False
        self._handling_voice_stop = False  # mutex guarding handle_voice_stop entry
        self._pre_roll_done = False
        # Reconnect backoff. Without it a single failure becomes a storm:
        # the device feeds a 60 ms frame every 60 ms, each one retries the
        # connection, and OVS (whose ASR executor runs max_workers=1) reports
        # pool_saturated because the previous worker has not been released
        # yet. Measured ~5 reconnects/second, which turned one dropped
        # utterance into a permanently unusable session.
        self._connect_fail_count = 0
        self._next_connect_at = 0.0
        self._conn = None


        # Fire-and-forget capabilities probe (non-blocking init).
        import threading
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._fetch_capabilities())
        except RuntimeError:
            threading.Thread(
                target=lambda: asyncio.run(self._fetch_capabilities()), daemon=True
            ).start()

    async def _fetch_capabilities(self):
        """Probe /asr/capabilities at startup (best-effort, non-blocking)."""
        # Convert ws://host:port/asr/stream → http://host:port/asr/capabilities
        try:
            parsed = urlparse(self.ws_url_base if hasattr(self, "ws_url_base") else self.ws_url)
            scheme = "https" if parsed.scheme == "wss" else "http"
            url = f"{scheme}://{parsed.netloc}/asr/capabilities"
        except Exception as exc:
            logger.bind(tag=TAG).debug(f"ASR capabilities URL build failed: {exc}")
            return
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    if resp.status == 401:
                        logger.bind(tag=TAG).error(
                            "OVS ASR 返回 401：服务端开启了 OVS_API_KEYS，"
                            "但本地 ASR 配置里的 api_key 为空或不正确"
                        )
                        return
                    if resp.status == 503:
                        logger.bind(tag=TAG).warning(
                            "OVS ASR hot-reload in progress at startup; capabilities skipped"
                        )
                        return
                    if resp.status != 200:
                        logger.bind(tag=TAG).warning(
                            f"ASR capabilities unavailable: status={resp.status}"
                        )
                        return
                    data = await resp.json()
            logger.bind(tag=TAG).info(
                f"OVS ASR capabilities: backend={data.get('backend')!r} "
                f"sample_rate={data.get('sample_rate')} "
                f"capabilities={data.get('capabilities')}"
            )
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"ASR capabilities probe failed: {exc}")

    # ------------------------------------------------------------------
    # Connection setup / teardown
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict:
        """OVS 的鉴权头。走 header 而不是 ?token=，避免 key 落进日志里的 URL。"""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _build_ws_url(self) -> str:
        parsed = urlparse(self.ws_url_base)
        existing = dict(parse_qsl(parsed.query))
        existing.setdefault("sample_rate", str(self.sample_rate))
        existing.setdefault("language", self.language)
        new_query = urlencode(existing)
        return urlunparse(parsed._replace(query=new_query))

    async def _start_session(self, conn: "ConnectionHandler"):
        url = self._build_ws_url()
        logger.bind(tag=TAG).debug(f"Connecting OpenVoiceStream ASR ws: {url}")
        self._conn = conn
        self.asr_ws = await websockets.connect(
            url,
            additional_headers=self._auth_headers(),
            max_size=1000000000,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
        )
        self.is_processing = True
        self._connect_fail_count = 0
        self._next_connect_at = 0.0
        self.last_partial = ""
        self._stop_sent = False
        self._pre_roll_done = False
        self._final_event = asyncio.Event()
        self._committed_text = ""
        self.receiver_task = asyncio.create_task(self._receive_loop(conn))

        # Pre-roll: replay the last few cached frames so we don't clip the
        # very start of the utterance. conn.asr_audio holds **PCM** —
        # connection.py:159 documents it as "存储PCM帧列表，供VAD和ASR共享"
        # and the entry point decodes Opus once for everyone
        # (connection.py:377 "入口处直接解码PCM，避免VAD和ASR重复解码").
        if conn.asr_audio:
            for cached_pcm in conn.asr_audio[-10:]:
                try:
                    await self.asr_ws.send(cached_pcm)
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"Pre-roll send failed: {e}")
                    break
        self._pre_roll_done = True

    def _note_connect_failure(self):
        """Exponential backoff, capped at 5 s.

        The cap matters as much as the growth: a user waiting on a device
        should get another attempt within a few seconds, but OVS must get
        enough quiet to release its single ASR worker. 0.5s, 1s, 2s, 4s, 5s…
        """
        import time as _t

        self._connect_fail_count += 1
        delay = min(0.5 * (2 ** (self._connect_fail_count - 1)), 5.0)
        self._next_connect_at = _t.monotonic() + delay
        logger.bind(tag=TAG).warning(
            f"ASR connect failed ({self._connect_fail_count}); "
            f"backing off {delay:.1f}s before retrying"
        )

    async def _cleanup(self):
        self.is_processing = False
        if self.receiver_task and not self.receiver_task.done():
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except (asyncio.CancelledError, Exception):
                pass
        self.receiver_task = None
        if self.asr_ws is not None:
            try:
                await asyncio.wait_for(self.asr_ws.close(), timeout=2.0)
            except Exception as e:
                logger.bind(tag=TAG).debug(f"ws close error: {e}")
            finally:
                self.asr_ws = None
        self._final_event = None
        self._stop_sent = False
        self._committed_text = ""

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self, conn: "ConnectionHandler"):
        try:
            while self.asr_ws is not None:
                try:
                    raw = await self.asr_ws.recv()
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("OpenVoiceStream ASR ws closed")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"ws recv error: {e}")
                    break

                try:
                    msg = json.loads(raw) if isinstance(raw, (str, bytes)) else None
                except Exception:
                    msg = None
                if not isinstance(msg, dict):
                    continue

                mtype = msg.get("type")
                text = msg.get("text", "") or ""
                is_final = bool(msg.get("is_final", False))

                if mtype == "partial":
                    if text:
                        self.last_partial = text
                    # We currently don't echo partial results upstream; the
                    # `partial_results` flag is reserved for future use.
                    continue

                if mtype == "final":
                    # Honour final only if we explicitly asked for endpoint,
                    # or operator opted into backend-driven endpointing.
                    if not is_final:
                        if text:
                            self.last_partial = text
                        continue
                    if not self._stop_sent and not self.allow_backend_endpoint:
                        # 引擎自己断句了。我们不把它当作「这轮说完了」（那会腰斩
                        # 用户还没说完的话），但**必须把这一段的文本收下来** ——
                        # 引擎已经在它那边翻篇了，后续 EOF 只会结算新的一段。
                        #
                        # 早先这里写的是 `self.last_partial = text`（覆盖），于是
                        # 用户说话中间一停顿超过 vad_silence_ms，前面所有内容就被
                        # 静默丢弃，最终只拿到最后一小段。实测：4.32s 音频完整送达
                        # （实发字节与缓冲逐字节相等），却只识别出「嗯。」；把同一份
                        # 字节离线整段转写则是「帮我查一下 SKU002 的库存。」。
                        if text:
                            self._commit_segment(text)
                            self.last_partial = ""
                        continue
                    # Either we triggered this via _send_stop_request, OR the
                    # backend detected its own endpoint (only when
                    # allow_backend_endpoint=True). First-wins mutex with
                    # receive_audio's client_voice_stop path.
                    if self._handling_voice_stop and not self._stop_sent:
                        # Another path already scheduled handle_voice_stop;
                        # just signal the final and exit.
                        self.text = self._join_committed(text or self.last_partial)
                        if self._final_event is not None:
                            self._final_event.set()
                        break
                    self.text = self._join_committed(text or self.last_partial)
                    if self._final_event is not None:
                        self._final_event.set()
                    if not self._stop_sent and self.allow_backend_endpoint:
                        # Backend self-endpointed; we must drive the rest of
                        # the pipeline since framework won't auto-call
                        # handle_voice_stop for STREAM providers.
                        self._handling_voice_stop = True
                        if self._conn is not None:
                            snapshot = (
                                list(self._conn.asr_audio)
                                if hasattr(self._conn, "asr_audio")
                                else []
                            )
                            asyncio.create_task(
                                self.handle_voice_stop(self._conn, snapshot)
                            )
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.bind(tag=TAG).error(f"OpenVoiceStream receive loop error: {e}")

    # ------------------------------------------------------------------
    # Public ASR hooks
    # ------------------------------------------------------------------

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn, audio, audio_have_voice):
        # Base class manages conn.asr_audio buffering / VAD bookkeeping.
        await super().receive_audio(conn, audio, audio_have_voice)

        # Open a new ws on the first voiced frame of an utterance.
        if audio_have_voice and not self.is_processing and self.asr_ws is None:
            import time as _t
            if _t.monotonic() < self._next_connect_at:
                return  # still backing off; drop this frame rather than pile on
            try:
                await self._start_session(conn)
            except Exception as e:
                # OVS 鉴权失败是 accept 之后 close 4401，不是 HTTP 401 —— 单看
                # 异常文本很难认出来，这里显式翻译，否则现场只会看到"连接失败"。
                if "4401" in str(e):
                    logger.bind(tag=TAG).error(
                        f"ASR 连接被拒(4401 未授权): {self.ws_url_base} —— "
                        "服务端开启了 OVS_API_KEYS，请在智控台的 ASR 配置里填 api_key"
                    )
                else:
                    logger.bind(tag=TAG).error(
                        f"ASR 连接失败: {self.ws_url_base} —— {e}。"
                        "请检查地址/端口是否正确、OVS 服务是否已就绪(/readyz)"
                    )
                await self._cleanup()
                self._note_connect_failure()
                return

        if self.asr_ws is not None and self.is_processing and self._pre_roll_done:
            try:
                # `audio` is already PCM (see pre-roll comment above). Decoding
                # it as Opus here fed libopus raw samples and raised
                # `corrupted stream` on every single frame, so ASR never
                # produced a result and the device sat in "listening" forever.
                await self.asr_ws.send(audio)
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"OpenVoiceStream ASR send failed: {e}"
                )
                await self._cleanup()
                # Count this too: a mid-utterance drop is followed by the very
                # next audio frame trying to reopen, which is exactly how the
                # storm starts.
                self._note_connect_failure()

        # STREAM providers must self-trigger handle_voice_stop; framework's
        # auto-call in base.py:76 only applies to non-STREAM. Mirror the
        # pattern used by aliyun_stream.py:257, doubao_stream.py:203.
        if (
            conn.client_voice_stop
            and not self._handling_voice_stop
            and len(conn.asr_audio) >= 15
        ):
            self._handling_voice_stop = True
            asr_audio_snapshot = list(conn.asr_audio)
            await self.handle_voice_stop(conn, asr_audio_snapshot)
            return

    def _commit_segment(self, text: str) -> None:
        """收下引擎自行断句产出的一段文本。"""
        text = (text or "").strip()
        if not text:
            return
        self._committed_text = self._concat(self._committed_text, text)

    @staticmethod
    def _concat(left: str, right: str) -> str:
        """拼接两段识别文本。

        中文之间不加空格（加了 TTS 会读出停顿、字幕也难看），拉丁字母相邻时补一个
        空格，否则 "SKU" + "0002" 会粘成 "SKU0002" 影响后续匹配。
        """
        if not left:
            return right
        if not right:
            return left
        need_space = left[-1].isascii() and left[-1].isalnum() and \
            right[0].isascii() and right[0].isalnum()
        return left + (" " if need_space else "") + right

    def _join_committed(self, tail: str) -> str:
        return self._concat(self._committed_text, (tail or "").strip())

    async def _send_stop_request(self):
        """Tell backend the utterance is over so it produces a final.

        Empirically the OpenVoiceStream server reliably emits {"type":"final"}
        on the empty-bytes EOF marker (server then closes the socket), while
        the {"command":"end_utterance"} JSON command does not surface a final
        in the streaming backend path. Use the empty-bytes path.
        """
        if self.asr_ws is None:
            return
        try:
            self._stop_sent = True
            await self.asr_ws.send(b"")
            logger.bind(tag=TAG).debug("Sent EOF (b'') to OpenVoiceStream")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"EOF send failed: {e}")

    async def handle_voice_stop(self, conn: "ConnectionHandler", asr_audio_task):
        """Wait for the final transcript, fall back to last partial on timeout."""
        try:
            if self.asr_ws is not None and self._final_event is not None:
                await self._send_stop_request()
                try:
                    await asyncio.wait_for(
                        self._final_event.wait(), timeout=self.final_timeout
                    )
                except asyncio.TimeoutError:
                    logger.bind(tag=TAG).warning(
                        f"OpenVoiceStream final timeout after {self.final_timeout}s; "
                        f"fallback_to_partial={self.fallback_to_partial}, "
                        f"last_partial={self.last_partial!r}"
                    )
                    if self.fallback_to_partial and self.last_partial:
                        self.text = self.last_partial
        except Exception as e:
            logger.bind(tag=TAG).error(f"OpenVoiceStream handle_voice_stop error: {e}")
        finally:
            # Always tear down the per-utterance session before delegating
            # to the base class, which will fan out the recognised text.
            await self._cleanup()

        try:
            await super().handle_voice_stop(conn, asr_audio_task)
        finally:
            # Reset for next utterance
            conn.reset_audio_states()
            # Clear per-utterance state for the next round.
            self.text = ""
            self.last_partial = ""
            self._handling_voice_stop = False
            self._stop_sent = False
            self._final_event = None
            # asr_ws already closed by _cleanup(); do not reclose here.

    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        audio_format: str = "opus",
        artifacts=None,
    ) -> Tuple[Optional[str], Optional[str]]:
        result = self.text
        self.text = ""
        return result, None

    async def close(self):
        await self._cleanup()
