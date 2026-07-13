import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

# Force FFmpeg's RTSP transport to TCP. UDP RTP packets are prone to being
# dropped by Windows Firewall / NAT along the way, which otherwise shows up
# as the capture silently losing frames and reconnecting on a loop.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import numpy as np

from app.detection import Detection, filter_false_positive_drones, get_drone_detector
from app.onvif_client import get_onvif_client

RECONNECT_BACKOFF_SECONDS = 2.0
MJPEG_FRAME_INTERVAL_SECONDS = 1 / 15
JPEG_ENCODE_PARAMS = [cv2.IMWRITE_JPEG_QUALITY, 85]

DETECTION_INTERVAL_SECONDS = 1.0
DETECTION_IDLE_POLL_SECONDS = 0.05
DRONE_BOX_COLOR = (0, 0, 255)  # BGR red
OTHER_BOX_COLOR = (0, 165, 255)  # BGR orange

# Motion detection is a cheap grayscale frame-diff, independent of the much
# heavier throttled YOLO pass -- it runs on every frame so it reacts faster
# than the ~1s detection cadence.
MOTION_PIXEL_DIFF_THRESHOLD = 25
MOTION_AREA_RATIO_THRESHOLD = 0.02


@dataclass
class DetectionResult:
    # Detections plus the identity and capture time of the exact frame they
    # were computed from. Control code needs this to distinguish fresh
    # results from stale ones (inference runs at ~1Hz while the autonomy
    # loop ticks at 20Hz) -- e.g. "was this computed from a frame captured
    # AFTER my zoom move finished?".
    detections: list[Detection]
    frame_seq: int
    frame_captured_at: float  # time.monotonic() when the frame was captured
    completed_at: float  # time.monotonic() when inference finished
    inference_seconds: float


# Module-level (not instance) state: the latest detections/motion are shared
# read-mostly results consumed from several threads (autonomy loop, API
# routers, MJPEG generators), each guarded by their own lock.
_detections_lock = threading.Lock()
_latest_result: Optional[DetectionResult] = None

_motion_lock = threading.Lock()
_previous_gray_frame: Optional[np.ndarray] = None
_latest_motion = {"active": False, "confidence": 0.0}

_fps_lock = threading.Lock()
_fps_frame_count = 0
_fps_window_started_at = time.monotonic()
_latest_fps = 0.0
FPS_WINDOW_SECONDS = 1.0


def get_latest_detection_result() -> Optional[DetectionResult]:
    with _detections_lock:
        return _latest_result


def get_latest_detections() -> list[Detection]:
    with _detections_lock:
        return list(_latest_result.detections) if _latest_result else []


def get_motion_status() -> dict:
    with _motion_lock:
        return dict(_latest_motion)


def get_performance_stats() -> dict:
    with _fps_lock:
        fps = _latest_fps
    return {
        "fps": round(fps, 1),
        "inference_ms": round(get_drone_detector().last_inference_seconds * 1000),
    }


def _record_frame_for_fps() -> None:
    global _fps_frame_count, _fps_window_started_at, _latest_fps
    with _fps_lock:
        _fps_frame_count += 1
        elapsed = time.monotonic() - _fps_window_started_at
        if elapsed >= FPS_WINDOW_SECONDS:
            _latest_fps = _fps_frame_count / elapsed
            _fps_frame_count = 0
            _fps_window_started_at = time.monotonic()


def _update_motion(frame: np.ndarray) -> None:
    global _previous_gray_frame, _latest_motion

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (21, 21), 0)

    if _previous_gray_frame is None or _previous_gray_frame.shape != gray.shape:
        _previous_gray_frame = gray
        return

    diff = cv2.absdiff(_previous_gray_frame, gray)
    _previous_gray_frame = gray

    changed_ratio = float(np.count_nonzero(diff > MOTION_PIXEL_DIFF_THRESHOLD)) / diff.size
    with _motion_lock:
        _latest_motion = {
            "active": changed_ratio >= MOTION_AREA_RATIO_THRESHOLD,
            "confidence": min(1.0, changed_ratio / MOTION_AREA_RATIO_THRESHOLD),
        }


def _draw_detection(frame: np.ndarray, detection: Detection) -> None:
    x1, y1, x2, y2 = detection.box
    color = DRONE_BOX_COLOR if detection.label.lower() == "drone" else OTHER_BOX_COLOR
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    label = f"{detection.label} {detection.confidence:.0%}"
    cv2.putText(frame, label, (x1, max(0, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)


class VideoStreamManager:
    def __init__(self, stream_url: str) -> None:
        self._stream_url = stream_url
        self._cap: Optional[cv2.VideoCapture] = None
        # The stored frame is always clean (no overlay) and is never mutated
        # after publication -- all drawing happens on copies. That invariant
        # is what lets the detection worker borrow it by reference below.
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_seq = 0
        self._frame_captured_at = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._detection_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        self._detection_thread = threading.Thread(target=self._detection_loop, daemon=True)
        self._detection_thread.start()

    def stop(self) -> None:
        self._running = False
        for thread in (self._capture_thread, self._detection_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        self._capture_thread = None
        self._detection_thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _capture_loop(self) -> None:
        print(f"[VIDEO] capture loop starting, stream_url={self._stream_url!r}", flush=True)
        while self._running:
            if self._cap is None or not self._cap.isOpened():
                print(f"[VIDEO] opening capture: {self._stream_url!r}", flush=True)
                self._cap = cv2.VideoCapture(self._stream_url, cv2.CAP_FFMPEG)
                if not self._cap.isOpened():
                    print("[VIDEO] cap.isOpened() == False, retrying...", flush=True)
                    time.sleep(RECONNECT_BACKOFF_SECONDS)
                    continue
                print("[VIDEO] capture opened successfully", flush=True)

            ok, frame = self._cap.read()
            if not ok:
                print("[VIDEO] cap.read() failed, reconnecting...", flush=True)
                self._cap.release()
                self._cap = None
                time.sleep(RECONNECT_BACKOFF_SECONDS)
                continue

            _record_frame_for_fps()
            # Motion is computed on the clean frame, on this thread, every
            # frame -- it's cheap. YOLO inference is NOT: it runs on the
            # dedicated detection thread so a ~250ms (worst case ~500ms with
            # the verification pass) call can never block capture and back
            # frames up in the RTSP/TCP buffer, which showed up as live-view
            # latency growing exactly when something was detected.
            _update_motion(frame)
            with self._lock:
                self._latest_frame = frame
                self._frame_seq += 1
                self._frame_captured_at = time.monotonic()

    def _borrow_frame_for_detection(self) -> Optional[tuple[np.ndarray, int, float]]:
        # Returns the stored frame by reference, NOT a copy: published frames
        # are immutable (see __init__), and skipping a ~6MB copy per
        # inference matters on the 2-core deployment CPU.
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame, self._frame_seq, self._frame_captured_at

    def _detection_loop(self) -> None:
        global _latest_result
        last_seq = -1
        next_start_at = 0.0
        while self._running:
            now = time.monotonic()
            if now < next_start_at:
                time.sleep(min(DETECTION_IDLE_POLL_SECONDS, next_start_at - now))
                continue

            borrowed = self._borrow_frame_for_detection()
            if borrowed is None or borrowed[1] == last_seq:
                time.sleep(DETECTION_IDLE_POLL_SECONDS)
                continue
            frame, seq, captured_at = borrowed
            last_seq = seq

            started = time.monotonic()
            try:
                detections = get_drone_detector().detect(frame)
                detections = filter_false_positive_drones(frame, detections)
            except Exception as exc:
                print(f"[VIDEO] detection failed: {exc}", flush=True)
                detections = []
            finished = time.monotonic()

            result = DetectionResult(
                detections=detections,
                frame_seq=seq,
                frame_captured_at=captured_at,
                completed_at=finished,
                inference_seconds=finished - started,
            )
            with _detections_lock:
                _latest_result = result

            # Pace by inference *start* time so the configured cadence holds
            # regardless of how long inference itself took; always consumes
            # the newest frame and simply skips the ones it can't keep up with.
            next_start_at = started + DETECTION_INTERVAL_SECONDS

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def get_latest_frame_annotated(self) -> Optional[np.ndarray]:
        # The stored frame is clean, so operator-facing images (MJPEG stream,
        # event snapshots) get the current detection boxes drawn onto a copy.
        frame = self.get_latest_frame()
        if frame is None:
            return None
        for detection in get_latest_detections():
            _draw_detection(frame, detection)
        return frame

    def get_latest_frame_width(self) -> Optional[int]:
        # Cheap alternative to get_latest_frame() for callers that only need
        # the frame's width (e.g. the rough distance estimate) -- avoids
        # copying the full ~6MB frame just to read one dimension.
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.shape[1]

    def get_latest_frame_shape(self) -> Optional[tuple[int, int]]:
        # Same idea as get_latest_frame_width(), but (height, width) -- for
        # callers (autonomy.py's control loop) that only ever need frame
        # dimensions, never pixel data, but need both axes.
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.shape[0], self._latest_frame.shape[1]

    def mjpeg_generator(self):
        while True:
            frame = self.get_latest_frame_annotated()
            if frame is None:
                time.sleep(0.1)
                continue

            ok, buffer = cv2.imencode(".jpg", frame, JPEG_ENCODE_PARAMS)
            if not ok:
                time.sleep(MJPEG_FRAME_INTERVAL_SECONDS)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )
            time.sleep(MJPEG_FRAME_INTERVAL_SECONDS)


@lru_cache
def get_video_stream_manager() -> VideoStreamManager:
    stream_url = get_onvif_client().get_stream_uri()
    manager = VideoStreamManager(stream_url)
    manager.start()
    return manager
