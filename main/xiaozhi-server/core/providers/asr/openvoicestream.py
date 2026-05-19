import json
import asyncio
import aiohttp
import websockets
import opuslib_next
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

        self.ws_url_base = self.config.get(
            "ws_url", "ws://127.0.0.1:8000/asr/stream"
        )
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
        # Becomes True once we explicitly send end_utterance — only after that
        # do we honour a final from the backend (unless allow_backend_endpoint).
        self._stop_sent = False
        self._handling_voice_stop = False  # mutex guarding handle_voice_stop entry
        self._pre_roll_done = False
        self._conn = None

        self.decoder = opuslib_next.Decoder(16000, 1)

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
                async with session.get(url) as resp:
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
            max_size=1000000000,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
        )
        self.is_processing = True
        self.last_partial = ""
        self._stop_sent = False
        self._pre_roll_done = False
        self._final_event = asyncio.Event()
        self.receiver_task = asyncio.create_task(self._receive_loop(conn))

        # Pre-roll: replay last few cached opus packets as PCM so we don't
        # clip the very start of the utterance.
        if conn.asr_audio:
            for cached in conn.asr_audio[-10:]:
                try:
                    pcm = self.decoder.decode(cached, 960)
                    await self.asr_ws.send(pcm)
                except Exception as e:
                    logger.bind(tag=TAG).warning(
                        f"Pre-roll decode/send failed: {e}"
                    )
                    break
        self._pre_roll_done = True

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
                        # Backend self-endpointed; treat as a stable partial.
                        if text:
                            self.last_partial = text
                        continue
                    # Either we triggered this via _send_stop_request, OR the
                    # backend detected its own endpoint (only when
                    # allow_backend_endpoint=True). First-wins mutex with
                    # receive_audio's client_voice_stop path.
                    if self._handling_voice_stop and not self._stop_sent:
                        # Another path already scheduled handle_voice_stop;
                        # just signal the final and exit.
                        self.text = text or self.last_partial
                        if self._final_event is not None:
                            self._final_event.set()
                        break
                    self.text = text or self.last_partial
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
            try:
                await self._start_session(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Failed to open OpenVoiceStream ASR session: {e}"
                )
                await self._cleanup()
                return

        if self.asr_ws is not None and self.is_processing and self._pre_roll_done:
            try:
                pcm_frame = self.decoder.decode(audio, 960)
                await self.asr_ws.send(pcm_frame)
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"OpenVoiceStream ASR send failed: {e}"
                )
                await self._cleanup()

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
        if getattr(self, "decoder", None) is not None:
            try:
                del self.decoder
            except Exception:
                pass
            self.decoder = None
