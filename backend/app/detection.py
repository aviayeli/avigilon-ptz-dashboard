import time
from dataclasses import dataclass
from functools import lru_cache
from math import radians, tan
from pathlib import Path

import numpy as np

# Rough distance estimate only -- this camera has no calibration data and no
# rangefinder, so both constants below are assumptions, not measurements.
# distance = (real-world reference width * focal length in pixels) / box
# width in pixels. Never present the result as an exact measurement.
DRONE_REFERENCE_WIDTH_METERS = 0.35
ASSUMED_HFOV_DEGREES = 60.0

MODEL_PATH = Path(__file__).resolve().parent.parent / "best_merged.pt"

# The custom model only has a single "drone" class, so it has no way to say
# "this is a chair, not a drone" -- it can only say "drone" with some
# confidence. Cross-checking candidate detections against a general-purpose
# pretrained model (80 everyday COCO classes) catches the common case where
# it's confidently misfiring on ordinary stationary objects. This is a
# heuristic, not a guarantee -- COCO has no "drone"/aircraft class either, so
# it can only rule things out, never positively confirm a drone.
GENERAL_MODEL_NAME = "yolov8n.pt"
# Labels that, when confidently detected overlapping a drone candidate,
# contradict it. Covers indoor objects AND the outdoor yard this system
# actually watches (cars, trucks, people -- observed live: the custom model
# fired 72% "drone" on a box that was mostly a parked car). Deliberately
# excludes bird/airplane/kite: COCO sometimes classifies real drones as
# those, so rejecting on them would drop true positives. The IoU gate below
# protects a small real drone flying in front of a car: its box shares
# almost no IoU with the car's much larger box.
NON_DRONE_OBJECT_LABELS = {
    "chair", "couch", "bed", "dining table", "toilet", "tv", "laptop",
    "refrigerator", "oven", "sink", "bench", "suitcase", "backpack",
    "handbag", "potted plant", "book", "clock", "vase", "keyboard",
    "mouse", "remote", "microwave", "toaster", "cell phone",
    "person", "car", "truck", "bus", "motorcycle", "bicycle", "train", "boat",
}
FALSE_POSITIVE_OVERLAP_IOU = 0.3
FALSE_POSITIVE_CONFIDENCE = 0.4

# Rejection memory: yolov8n's read of the contradicting object flickers
# frame to frame, so a candidate rejected one second can leak through the
# next and trigger a pointless investigation (observed live: a dozen ~1.5s
# micro-investigations of the same parked car in three minutes). Remember
# recently rejected regions and keep rejecting candidates that reappear in
# roughly the same place. Pixel regions are only meaningful while the
# camera holds still, so the memory is cleared the moment the camera moves.
# Deliberately NOT refreshed on memory-based hits: a stationary false
# positive may leak one short investigation per expiry window, but a real
# drone that later hovers over the remembered spot is never suppressed
# indefinitely.
REJECTION_MEMORY_SECONDS = 10.0
REJECTION_MEMORY_IOU = 0.4
REJECTION_MEMORY_MAX_ENTRIES = 20
# Only ever touched from the single detection thread
# (VideoStreamManager._detection_loop), so no lock is needed.
_rejection_memory: list[tuple[tuple[int, int, int, int], float]] = []

# Import order matters: disable Ultralytics' online telemetry/update checks
# before the first YOLO() construction, since a hang/delay on this slow
# connection would otherwise stall the first detection call.
from ultralytics import settings as ultralytics_settings  # noqa: E402

ultralytics_settings.update({"sync": False})

from ultralytics import YOLO  # noqa: E402
import torch  # noqa: E402

# On a 2-physical-core CPU (this deployment target), torch defaulting to
# every logical core -- or even 2 threads -- saturates the whole physical
# CPU during each inference call, starving the video-capture thread and
# FastAPI's request handling of real execution time (hyperthreaded sibling
# threads share the same execution units, they don't add real capacity).
# Single-threaded inference is slower per call but keeps the rest of the
# app responsive while it runs, which matters more for a live UI than
# shaving inference time.
torch.set_num_threads(1)


@dataclass
class Detection:
    label: str
    confidence: float
    box: tuple[int, int, int, int]  # pixel xyxy


def estimate_distance_meters(detection: Detection, frame_width_px: int) -> float:
    # Rough order-of-magnitude estimate, not a measurement -- see the module
    # docstring above. Assumes the detected box width roughly matches
    # DRONE_REFERENCE_WIDTH_METERS at whatever distance it actually is.
    x1, _, x2, _ = detection.box
    box_width_px = max(1, x2 - x1)
    focal_length_px = frame_width_px / (2 * tan(radians(ASSUMED_HFOV_DEGREES) / 2))
    return (DRONE_REFERENCE_WIDTH_METERS * focal_length_px) / box_width_px


class DroneDetector:
    def __init__(self, model_path: Path) -> None:
        self._model = YOLO(str(model_path))
        self.last_inference_seconds = 0.0

    def detect(self, frame: np.ndarray) -> list[Detection]:
        start = time.monotonic()
        results = self._model(frame, verbose=False)
        elapsed = time.monotonic() - start
        self.last_inference_seconds = elapsed
        print(f"[DETECT] inference took {elapsed:.2f}s", flush=True)

        detections: list[Detection] = []
        result = results[0]
        names = result.names
        for box in result.boxes:
            cls_id = int(box.cls[0])
            confidence = float(box.conf[0])
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
            detections.append(
                Detection(
                    label=names[cls_id],
                    confidence=confidence,
                    box=(x1, y1, x2, y2),
                )
            )
        return detections


@lru_cache
def get_drone_detector() -> DroneDetector:
    return DroneDetector(MODEL_PATH)


@lru_cache
def get_general_detector() -> DroneDetector:
    # Passing a bare model name (not a local path) makes Ultralytics
    # auto-download the pretrained COCO weights on first use.
    return DroneDetector(Path(GENERAL_MODEL_NAME))


def _iou(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1, inter_y1 = max(ax1, bx1), max(ay1, by1)
    inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
    inter_area = max(0, inter_x2 - inter_x1) * max(0, inter_y2 - inter_y1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter_area
    return inter_area / union if union > 0 else 0.0


def filter_false_positive_drones(
    frame: np.ndarray, detections: list[Detection], camera_settled: bool = True
) -> list[Detection]:
    global _rejection_memory
    drone_candidates = [d for d in detections if d.label.lower() == "drone"]
    if not drone_candidates:
        return detections

    now = time.monotonic()
    if not camera_settled:
        # Remembered regions are pixel-space; any camera move (pan/tilt/zoom)
        # reframes or rescales the scene and invalidates all of them.
        _rejection_memory = []
    else:
        _rejection_memory = [(box, exp) for box, exp in _rejection_memory if exp > now]

    general_results = get_general_detector().detect(frame)

    verified: list[Detection] = []
    for detection in detections:
        if detection.label.lower() != "drone":
            verified.append(detection)
            continue

        contradicted = any(
            g.label.lower() in NON_DRONE_OBJECT_LABELS
            and g.confidence >= FALSE_POSITIVE_CONFIDENCE
            and _iou(detection.box, g.box) >= FALSE_POSITIVE_OVERLAP_IOU
            for g in general_results
        )
        remembered = (
            camera_settled
            and not contradicted
            and any(
                _iou(detection.box, box) >= REJECTION_MEMORY_IOU
                for box, _ in _rejection_memory
            )
        )
        if contradicted:
            if camera_settled:
                _rejection_memory.append(
                    (detection.box, now + REJECTION_MEMORY_SECONDS)
                )
                _rejection_memory = _rejection_memory[-REJECTION_MEMORY_MAX_ENTRIES:]
            print(
                f"[DETECT] rejected drone candidate (confidence={detection.confidence:.2f}) "
                "-- overlaps a confidently-classified non-drone object",
                flush=True,
            )
        elif remembered:
            print(
                f"[DETECT] rejected drone candidate (confidence={detection.confidence:.2f}) "
                "-- same region was rejected moments ago (memory)",
                flush=True,
            )
        else:
            verified.append(detection)

    return verified
