import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)
MAX_EVENTS = 100
JPEG_ENCODE_PARAMS = [cv2.IMWRITE_JPEG_QUALITY, 85]


@dataclass
class Event:
    timestamp: str
    type: str
    message: str
    snapshot: Optional[str] = None


class EventLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[Event] = deque(maxlen=MAX_EVENTS)

    def add(self, event_type: str, message: str, frame: Optional[np.ndarray] = None) -> None:
        snapshot_url = self._save_snapshot(frame) if frame is not None else None
        event = Event(
            timestamp=datetime.now(timezone.utc).isoformat(),
            type=event_type,
            message=message,
            snapshot=snapshot_url,
        )
        with self._lock:
            self._events.appendleft(event)

    def _save_snapshot(self, frame: np.ndarray) -> Optional[str]:
        filename = f"{int(time.time() * 1000)}.jpg"
        ok, buffer = cv2.imencode(".jpg", frame, JPEG_ENCODE_PARAMS)
        if not ok:
            return None
        (SNAPSHOT_DIR / filename).write_bytes(buffer.tobytes())
        # Served through an authenticated endpoint, not a public static mount --
        # this is sensitive camera footage, not a generic static asset.
        return f"/api/events/snapshots/{filename}"

    def get_recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in list(self._events)[:limit]]


@lru_cache
def get_event_log() -> EventLog:
    return EventLog()
