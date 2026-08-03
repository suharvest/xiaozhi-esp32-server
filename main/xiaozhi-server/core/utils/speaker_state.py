import threading
import time
from collections import deque
from typing import Optional


_lock = threading.Lock()
_events = deque(maxlen=200)


def record_speaker_result(
    *,
    session_id: Optional[str],
    device_id: Optional[str],
    speaker: Optional[str],
    content: Optional[str] = None,
    source: str = "voiceprint",
):
    if not speaker:
        return None

    event = {
        "timestamp": time.time(),
        "session_id": session_id,
        "device_id": device_id,
        "speaker": speaker,
        "content": content,
        "source": source,
    }
    with _lock:
        _events.append(event)
    return event


def get_latest_speaker(
    *,
    session_id: Optional[str] = None,
    device_id: Optional[str] = None,
):
    with _lock:
        snapshot = list(_events)

    for event in reversed(snapshot):
        if session_id and event.get("session_id") != session_id:
            continue
        if device_id and event.get("device_id") != device_id:
            continue
        return event
    return None


def list_speaker_events(
    *,
    limit: int = 20,
    session_id: Optional[str] = None,
    device_id: Optional[str] = None,
):
    limit = max(1, min(int(limit), 200))
    with _lock:
        snapshot = list(_events)

    matched = []
    for event in reversed(snapshot):
        if session_id and event.get("session_id") != session_id:
            continue
        if device_id and event.get("device_id") != device_id:
            continue
        matched.append(event)
        if len(matched) >= limit:
            break
    return matched
