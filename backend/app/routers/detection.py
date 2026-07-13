from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.autonomy import (
    MAX_CONFIDENCE_THRESHOLD,
    MIN_CONFIDENCE_THRESHOLD,
    get_confidence_threshold,
    set_confidence_threshold,
)
from app.detection import estimate_distance_meters
from app.routers.auth import require_auth
from app.video_stream import (
    get_latest_detections,
    get_motion_status,
    get_performance_stats,
    get_video_stream_manager,
)

router = APIRouter(prefix="/api/detection", dependencies=[Depends(require_auth)])

DRONE_LABEL = "drone"


@router.get("/status")
def status():
    detections = get_latest_detections()
    drones = [d for d in detections if d.label.lower() == DRONE_LABEL]
    top_drone = max(drones, key=lambda d: d.confidence, default=None)

    drone_payload = None
    if top_drone is not None:
        frame_width = get_video_stream_manager().get_latest_frame_width()
        drone_payload = {
            "detected": True,
            "confidence": top_drone.confidence,
            "distance_m": (
                round(estimate_distance_meters(top_drone, frame_width), 1)
                if frame_width
                else None
            ),
        }

    return {
        "motion": get_motion_status(),
        "drone": drone_payload,
        "performance": get_performance_stats(),
    }


class SensitivityRequest(BaseModel):
    threshold: float


@router.get("/sensitivity")
def get_sensitivity():
    return {
        "threshold": get_confidence_threshold(),
        "min": MIN_CONFIDENCE_THRESHOLD,
        "max": MAX_CONFIDENCE_THRESHOLD,
    }


@router.post("/sensitivity")
def update_sensitivity(payload: SensitivityRequest):
    return {"threshold": set_confidence_threshold(payload.threshold)}
