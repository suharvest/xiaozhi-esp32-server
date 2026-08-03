import base64
import json
import math
import os
import struct
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiohttp import web

from core.api.base_handler import BaseHandler


class VoiceprintAdapterHandler(BaseHandler):
    """voiceprint-api compatible adapter backed by seeed-local-voice CAM++."""

    def __init__(self, config: dict):
        super().__init__(config)
        adapter_config = config.get("voiceprint_adapter", {}) or {}
        self.embedding_url = adapter_config.get(
            "embedding_url",
            "http://127.0.0.1:8621/speaker/embedding",
        )
        self.embedding_api_key = adapter_config.get("embedding_api_key", "")
        self.api_key = adapter_config.get("api_key") or self._key_from_voiceprint_url()
        self.storage_path = adapter_config.get(
            "storage_path",
            os.path.join(os.getcwd(), "data", "voiceprint_embeddings.json"),
        )
        self.timeout = float(adapter_config.get("timeout", 10))

    def _key_from_voiceprint_url(self) -> str:
        voiceprint = self.config.get("voiceprint", {}) or {}
        raw_url = voiceprint.get("url", "")
        if not raw_url:
            return ""
        query = parse_qs(urlparse(raw_url).query)
        return query.get("key", [""])[0]

    def _check_auth(self, request) -> bool:
        if not self.api_key:
            return True
        query_key = request.query.get("key")
        if query_key == self.api_key:
            return True
        auth_header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        return auth_header.startswith(prefix) and auth_header[len(prefix):].strip() == self.api_key

    def _json(self, data: dict, status: int = 200):
        response = web.json_response(data, status=status)
        self._add_cors_headers(response)
        return response

    def _load_store(self) -> dict:
        if not os.path.exists(self.storage_path):
            return {"voiceprints": {}}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("voiceprints"), dict):
                return data
        except Exception as e:
            self.logger.error(f"读取声纹库失败: {e}")
        return {"voiceprints": {}}

    def _save_store(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.storage_path) or ".", exist_ok=True)
        tmp = self.storage_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.storage_path)

    async def _read_multipart(self, request):
        reader = await request.multipart()
        fields = {}
        file_bytes = None
        async for part in reader:
            if part.name == "file":
                file_bytes = await part.read(decode=False)
            else:
                fields[part.name] = (await part.text()).strip()
        return fields, file_bytes

    async def _extract_embedding(self, audio_data: bytes) -> dict:
        headers = {"Accept": "application/json"}
        if self.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.embedding_api_key}"

        form = aiohttp.FormData()
        form.add_field(
            "file",
            audio_data,
            filename="audio.wav",
            content_type="audio/wav",
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.embedding_url, headers=headers, data=form) as resp:
                try:
                    payload = await resp.json()
                except Exception:
                    payload = {"error": await resp.text()}
                if resp.status != 200:
                    raise RuntimeError(f"CAM++ embedding failed: HTTP {resp.status} {payload}")
                return payload

    @staticmethod
    def _decode_embedding(embedding_b64: str) -> list[float]:
        raw = base64.b64decode(embedding_b64)
        if len(raw) % 4 != 0:
            raise ValueError("embedding bytes length is not float32 aligned")
        count = len(raw) // 4
        return list(struct.unpack("<" + "f" * count, raw))

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na * nb)

    async def handle_health(self, request):
        if not self._check_auth(request):
            return self._json({"error": "unauthorized"}, status=401)

        store = self._load_store()
        return self._json(
            {
                "status": "healthy",
                "total_voiceprints": len(store.get("voiceprints", {})),
                "backend": "seeed-local-voice",
                "embedding_url": self.embedding_url,
            }
        )

    async def handle_register(self, request):
        if not self._check_auth(request):
            return self._json({"error": "unauthorized"}, status=401)

        try:
            fields, audio_data = await self._read_multipart(request)
            speaker_id = fields.get("speaker_id")
            if not speaker_id:
                return self._json({"error": "speaker_id required"}, status=400)
            if not audio_data:
                return self._json({"error": "file required"}, status=400)

            embedding = await self._extract_embedding(audio_data)
            embedding_b64 = embedding.get("embedding_b64")
            if not embedding_b64:
                return self._json({"error": "embedding_b64 missing from backend"}, status=502)

            store = self._load_store()
            store.setdefault("voiceprints", {})[speaker_id] = {
                "speaker_id": speaker_id,
                "embedding_b64": embedding_b64,
                "embedding_model": embedding.get("embedding_model"),
                "dim": embedding.get("dim"),
                "normalized": embedding.get("normalized"),
                "updated_at": time.time(),
            }
            self._save_store(store)
            return self._json(
                {
                    "status": "success",
                    "speaker_id": speaker_id,
                    "embedding_model": embedding.get("embedding_model"),
                    "dim": embedding.get("dim"),
                }
            )
        except Exception as e:
            self.logger.error(f"注册声纹失败: {e}")
            return self._json({"error": str(e)}, status=500)

    async def handle_identify(self, request):
        if not self._check_auth(request):
            return self._json({"error": "unauthorized"}, status=401)

        try:
            fields, audio_data = await self._read_multipart(request)
            if not audio_data:
                return self._json({"error": "file required"}, status=400)

            speaker_ids = [
                item.strip()
                for item in fields.get("speaker_ids", "").split(",")
                if item.strip()
            ]
            store = self._load_store()
            voiceprints = store.get("voiceprints", {})
            candidates = speaker_ids or list(voiceprints.keys())
            candidates = [speaker_id for speaker_id in candidates if speaker_id in voiceprints]
            if not candidates:
                return self._json({"speaker_id": None, "score": 0, "error": "no_registered_speakers"}, status=404)

            embedding = await self._extract_embedding(audio_data)
            query_b64 = embedding.get("embedding_b64")
            if not query_b64:
                return self._json({"error": "embedding_b64 missing from backend"}, status=502)

            query_model = embedding.get("embedding_model")
            query_dim = embedding.get("dim")
            query_vector = self._decode_embedding(query_b64)

            best_id: Optional[str] = None
            best_score = -1.0
            for speaker_id in candidates:
                item = voiceprints[speaker_id]
                if query_model and item.get("embedding_model") and item.get("embedding_model") != query_model:
                    continue
                if query_dim and item.get("dim") and int(item.get("dim")) != int(query_dim):
                    continue
                score = self._cosine(query_vector, self._decode_embedding(item["embedding_b64"]))
                if score > best_score:
                    best_id = speaker_id
                    best_score = score

            if best_id is None:
                return self._json({"speaker_id": None, "score": 0, "error": "no_compatible_speakers"}, status=404)

            return self._json(
                {
                    "speaker_id": best_id,
                    "score": best_score,
                    "embedding_model": query_model,
                    "dim": query_dim,
                }
            )
        except Exception as e:
            self.logger.error(f"识别声纹失败: {e}")
            return self._json({"error": str(e)}, status=500)
