from fastapi import APIRouter
from pydantic import BaseModel

from app.autonomy import get_autonomy_controller
from app.onvif_client import get_onvif_client

router = APIRouter(prefix="/api/autonomy")

# The dashboard presents pan/tilt range as a friendly -100..100 scale rather
# than the camera's native normalized -1..1 ONVIF position space.
FRIENDLY_SCALE = 100.0


class StartRequest(BaseModel):
    pan_min: float
    pan_max: float
    tilt_min: float
    tilt_max: float


@router.post("/start")
def start(payload: StartRequest):
    client = get_onvif_client()
    limits = client.get_pan_tilt_limits()
    center = client.get_center()

    # Incoming pan_min/pan_max/tilt_min/tilt_max are offsets from the
    # user-defined center (see /api/ptz/center), not raw ONVIF coordinates.
    pan_min = max(limits["pan_min"], center["pan"] + payload.pan_min / FRIENDLY_SCALE)
    pan_max = min(limits["pan_max"], center["pan"] + payload.pan_max / FRIENDLY_SCALE)
    tilt_min = max(limits["tilt_min"], center["tilt"] + payload.tilt_min / FRIENDLY_SCALE)
    tilt_max = min(limits["tilt_max"], center["tilt"] + payload.tilt_max / FRIENDLY_SCALE)

    if pan_min > pan_max:
        pan_min, pan_max = pan_max, pan_min
    if tilt_min > tilt_max:
        tilt_min, tilt_max = tilt_max, tilt_min

    # If a drone is already in view, start() engages it immediately
    # (tracking + alarm) instead of refusing.
    get_autonomy_controller().start(pan_min, pan_max, tilt_min, tilt_max)
    return {"ok": True}


@router.post("/stop")
def stop():
    get_autonomy_controller().stop()
    return {"ok": True}


@router.post("/alarm/dismiss")
def dismiss_alarm():
    get_autonomy_controller().dismiss_alarm()
    return {"ok": True}


@router.get("/status")
def status():
    return get_autonomy_controller().status()


@router.get("/limits")
def limits():
    client = get_onvif_client()
    native = client.get_pan_tilt_limits()
    center = client.get_center()
    return {
        "pan_min": (native["pan_min"] - center["pan"]) * FRIENDLY_SCALE,
        "pan_max": (native["pan_max"] - center["pan"]) * FRIENDLY_SCALE,
        "tilt_min": (native["tilt_min"] - center["tilt"]) * FRIENDLY_SCALE,
        "tilt_max": (native["tilt_max"] - center["tilt"]) * FRIENDLY_SCALE,
    }
