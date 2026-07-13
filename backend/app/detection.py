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
STATIONARY_OBJECT_LABELS = {
    "chair", "couch", "bed", "dining table", "toilet", "tv", "laptop",
    "refrigerator", "oven", "sink", "bench", "suitcase", "backpack",
    "handbag", "potted plant", "book", "clock", "vase", "keyboard",
    "mouse", "remote", "microwave", "toaster", "cell phone",
}
FALSE_POSITIVE_OVERLAP_IOU = 0.3
FALSE_POSITIVE_CONFIDENCE = 0.4

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


def filter_false_positive_drones(frame: np.ndarray, detections: list[Detection]) -> list[Detection]:
    drone_candidates = [d for d in detections if d.label.lower() == "drone"]
    if not drone_candidates:
        return detections

    general_results = get_general_detector().detect(frame)

    verified: list[Detection] = []
    for detection in detections:
        if detection.label.lower() != "drone":
            verified.append(detection)
            continue

        contradicted = any(
            g.label.lower() in STATIONARY_OBJECT_LABELS
            and g.confidence >= FALSE_POSITIVE_CONFIDENCE
            and _iou(detection.box, g.box) >= FALSE_POSITIVE_OVERLAP_IOU
            for g in general_results
        )
        if contradicted:
            print(
                f"[DETECT] rejected drone candidate (confidence={detection.confidence:.2f}) "
                "-- overlaps a confidently-classified stationary object",
                flush=True,
            )
        else:
            verified.append(detection)

    return verified
