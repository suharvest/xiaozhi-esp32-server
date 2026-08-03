from aiohttp import web

from core.api.base_handler import BaseHandler
from core.utils.speaker_state import get_latest_speaker, list_speaker_events


class SpeakerHandler(BaseHandler):
    async def handle_latest(self, request):
        session_id = request.query.get("session_id")
        device_id = request.query.get("device_id")

        event = get_latest_speaker(session_id=session_id, device_id=device_id)
        response = web.json_response(
            {
                "ok": event is not None,
                "data": event,
            },
            status=200 if event is not None else 404,
        )
        self._add_cors_headers(response)
        return response

    async def handle_events(self, request):
        session_id = request.query.get("session_id")
        device_id = request.query.get("device_id")
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20

        events = list_speaker_events(
            limit=limit,
            session_id=session_id,
            device_id=device_id,
        )
        response = web.json_response(
            {
                "ok": True,
                "data": events,
            }
        )
        self._add_cors_headers(response)
        return response
