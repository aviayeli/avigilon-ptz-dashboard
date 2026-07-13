import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.events import SNAPSHOT_DIR, get_event_log

router = APIRouter(prefix="/api/events")

# Snapshot filenames are always generated as "<millis>.jpg" by EventLog --
# reject anything else so a crafted filename can't be used for path traversal.
_SAFE_FILENAME = re.compile(r"^\d+\.jpg$")


@router.get("")
def list_events(limit: int = 50):
    return get_event_log().get_recent(limit)


@router.get("/snapshots/{filename}")
def get_snapshot(filename: str):
    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(400, "Invalid filename")
    path = SNAPSHOT_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "Snapshot not found")
    return FileResponse(path, media_type="image/jpeg")
