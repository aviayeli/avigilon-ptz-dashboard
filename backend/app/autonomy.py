import threading
import time
import traceback
from enum import Enum
from functools import lru_cache
from typing import Optional

from app.alarm import start_alarm, stop_alarm
from app.detection import Detection
from app.events import get_event_log
from app.onvif_client import get_onvif_client
from app.video_stream import get_latest_detections, get_motion_status, get_video_stream_manager

LOOP_TICK_SECONDS = 0.05

RASTER_ROWS = 3
PTZ_MOVE_TIMEOUT_SECONDS = 8.0
PTZ_STATUS_POLL_INTERVAL_SECONDS = 0.4

# Smart-scan: waypoints near recent motion/detections get "hotter" and are
# revisited out of order; everywhere else gets swept faster. Bounded so a
# false positive can't make the scan fixate on one spot forever -- the heat
# always decays, and the scan always falls back to normal sequential
# coverage once nothing stays hot.
HEAT_INCREMENT = 1.0
HEAT_DECAY = 0.9
HEAT_MAX = 10.0
HEAT_REVISIT_THRESHOLD = 3.0
SCAN_SPEED_SLOW = 0.2
SCAN_SPEED_FAST = 0.6

SMALL_BOX_AREA_RATIO = 0.05
ZOOM_PULSE_SECONDS = 0.5
ZOOM_PULSE_SPEED = 0.5
MAX_ZOOM_ATTEMPTS = 3

DRONE_LABEL = "drone"
DEFAULT_DRONE_CONFIDENCE_THRESHOLD = 0.5
MIN_CONFIDENCE_THRESHOLD = 0.05
MAX_CONFIDENCE_THRESHOLD = 0.95

_confidence_threshold_lock = threading.Lock()
_drone_confidence_threshold = DEFAULT_DRONE_CONFIDENCE_THRESHOLD


def get_confidence_threshold() -> float:
    with _confidence_threshold_lock:
        return _drone_confidence_threshold


def set_confidence_threshold(value: float) -> float:
    global _drone_confidence_threshold
    clamped = max(MIN_CONFIDENCE_THRESHOLD, min(MAX_CONFIDENCE_THRESHOLD, value))
    with _confidence_threshold_lock:
        _drone_confidence_threshold = clamped
    return clamped

TRACK_DEADBAND_RATIO = 0.12
# Kept modest deliberately: faster tracking motion blurs the captured frame,
# which can make YOLO intermittently miss the target mid-correction. Prefer
# smaller, more frequent corrections over large, blur-inducing ones.
TRACK_MAX_VELOCITY = 0.3
TARGET_BOX_AREA_RATIO = 0.08
TRACK_ZOOM_SPEED = 0.2
LOST_TARGET_TIMEOUT_SECONDS = 3.0


class Mode(str, Enum):
    IDLE = "idle"
    SEARCHING = "searching"
    INVESTIGATING = "investigating"
    TRACKING = "tracking"


def _box_area(box: tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = box
    return max(0, x2 - x1) * max(0, y2 - y1)


def _largest_detection(detections: list[Detection]) -> Optional[Detection]:
    if not detections:
        return None
    return max(detections, key=lambda d: _box_area(d.box))


def _largest_drone_detection(detections: list[Detection]) -> Optional[Detection]:
    drones = [d for d in detections if d.label.lower() == DRONE_LABEL]
    return _largest_detection(drones)


def is_drone_currently_present() -> bool:
    # Detection runs continuously regardless of search state (see
    # VideoStreamManager._detection_loop), so this reflects the live camera
    # view at the moment it's called -- used as a pre-flight safety check before
    # starting a new scan.
    threshold = get_confidence_threshold()
    return any(
        d.label.lower() == DRONE_LABEL and d.confidence >= threshold
        for d in get_latest_detections()
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _build_raster_waypoints(
    pan_min: float, pan_max: float, tilt_min: float, tilt_max: float, rows: int = RASTER_ROWS
) -> list[tuple[float, float]]:
    # Zigzags through the rectangle row by row (boustrophedon pattern) so
    # consecutive waypoints are always adjacent -- no wasted travel jumping
    # back to a far corner between rows.
    if rows <= 1:
        tilts = [(tilt_min + tilt_max) / 2]
    else:
        tilts = [tilt_min + (tilt_max - tilt_min) * i / (rows - 1) for i in range(rows)]

    waypoints: list[tuple[float, float]] = []
    for i, tilt in enumerate(tilts):
        row = [(pan_min, tilt), (pan_max, tilt)]
        if i % 2 == 1:
            row.reverse()
        waypoints.extend(row)
    return waypoints


class AutonomyController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = Mode.IDLE
        self._alarm_active = False
        self._last_detection: Optional[Detection] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pan_min = -1.0
        self._pan_max = 1.0
        self._tilt_min = -1.0
        self._tilt_max = 1.0

    @property
    def mode(self) -> Mode:
        with self._lock:
            return self._mode

    def status(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode.value,
                "alarm_active": self._alarm_active,
                "last_detection": (
                    {
                        "label": self._last_detection.label,
                        "confidence": self._last_detection.confidence,
                    }
                    if self._last_detection
                    else None
                ),
            }

    def start(self, pan_min: float, pan_max: float, tilt_min: float, tilt_max: float) -> bool:
        """Returns False if refused because a drone is already present in view."""
        with self._lock:
            if self._running:
                return True

        # Safety check, done before touching any state: never start a new
        # scan while a drone is already visible -- e.g. if a previous
        # tracking session just ended but the drone hasn't actually left.
        if is_drone_currently_present():
            return False

        with self._lock:
            if self._running:
                return True
            self._pan_min = pan_min
            self._pan_max = pan_max
            self._tilt_min = tilt_min
            self._tilt_max = tilt_max
            self._running = True
            self._mode = Mode.SEARCHING
        get_event_log().add("search_started", f"pan {pan_min:.0f}..{pan_max:.0f}, tilt {tilt_min:.0f}..{tilt_max:.0f}")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        was_running = self._is_running()
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._safe_stop_camera()
        self._set_alarm(False)
        self._set_last_detection(None)
        with self._lock:
            self._mode = Mode.IDLE
        if was_running:
            get_event_log().add("search_stopped", "")

    def dismiss_alarm(self) -> None:
        # Operator acknowledgement: silences the alarm (server + browser via
        # the polled status) without changing mode. If still TRACKING, the
        # camera keeps following the target silently; the alarm re-arms only
        # on the next INVESTIGATING -> TRACKING confirmation.
        self._set_alarm(False)

    def _is_running(self) -> bool:
        with self._lock:
            return self._running

    def _set_mode(self, mode: Mode) -> None:
        with self._lock:
            self._mode = mode

    def _set_last_detection(self, detection: Optional[Detection]) -> None:
        with self._lock:
            self._last_detection = detection

    def _set_alarm(self, active: bool) -> None:
        with self._lock:
            if active == self._alarm_active:
                return
            self._alarm_active = active
        if active:
            start_alarm()
        else:
            stop_alarm()

    def _safe_stop_camera(self) -> None:
        try:
            get_onvif_client().stop()
        except Exception:
            pass

    def _reverse_zoom(self, onvif, applied_zoom_seconds: float) -> None:
        if applied_zoom_seconds <= 0:
            return
        onvif.continuous_move(0, 0, -ZOOM_PULSE_SPEED)
        time.sleep(applied_zoom_seconds)
        onvif.stop()

    def _run_loop(self) -> None:
        onvif = get_onvif_client()
        video = get_video_stream_manager()

        waypoints = _build_raster_waypoints(self._pan_min, self._pan_max, self._tilt_min, self._tilt_max)
        waypoint_heat = [0.0] * len(waypoints)
        waypoint_index = 0
        move_issued = False
        move_started_at = time.monotonic()
        last_status_poll_at = 0.0

        last_detection_at = time.monotonic()
        applied_zoom_seconds = 0.0
        zoom_attempts = 0
        was_in_deadband = False

        try:
            while self._is_running():
                now = time.monotonic()
                # Only the frame's dimensions are ever needed here (never
                # pixel data), so use the cheap shape-only accessor instead
                # of copying the full ~6MB frame on every tick (20x/sec).
                frame_shape = video.get_latest_frame_shape()
                # Detection itself runs continuously in the video pipeline
                # (VideoStreamManager._detection_loop), independent of search state,
                # so viewers see live boxes even when autonomy is idle. This
                # loop just reads the shared, already-computed result.
                last_detections = get_latest_detections()

                mode = self.mode

                if mode == Mode.SEARCHING:
                    activity = bool(last_detections) or get_motion_status().get("active", False)
                    if activity:
                        waypoint_heat[waypoint_index] = min(
                            HEAT_MAX, waypoint_heat[waypoint_index] + HEAT_INCREMENT
                        )

                    if not move_issued:
                        pan, tilt = waypoints[waypoint_index]
                        speed = SCAN_SPEED_SLOW if activity else SCAN_SPEED_FAST
                        print(
                            f"[AUTONOMY] scan -> waypoint {waypoint_index} "
                            f"pan={pan:.2f} tilt={tilt:.2f} speed={speed:.2f} activity={activity}",
                            flush=True,
                        )
                        onvif.absolute_move(pan, tilt, speed=speed)
                        move_issued = True
                        move_started_at = now
                        last_status_poll_at = now

                    if now - last_status_poll_at >= PTZ_STATUS_POLL_INTERVAL_SECONDS:
                        last_status_poll_at = now
                        try:
                            arrived = not onvif.get_ptz_status()["moving"]
                        except Exception:
                            arrived = True  # can't tell -- don't get stuck here forever
                        if arrived or now - move_started_at >= PTZ_MOVE_TIMEOUT_SECONDS:
                            waypoint_heat[waypoint_index] *= HEAT_DECAY
                            hottest_index = max(range(len(waypoints)), key=lambda i: waypoint_heat[i])
                            if (
                                hottest_index != waypoint_index
                                and waypoint_heat[hottest_index] >= HEAT_REVISIT_THRESHOLD
                            ):
                                waypoint_index = hottest_index
                                # Partial decay on the detour target too, so a
                                # stubborn false positive can't keep winning
                                # forever -- it cools down each visit.
                                waypoint_heat[hottest_index] *= HEAT_DECAY
                            else:
                                waypoint_index = (waypoint_index + 1) % len(waypoints)
                            move_issued = False

                    if last_detections:
                        onvif.stop()
                        move_issued = False
                        self._set_last_detection(last_detections[0])
                        zoom_attempts = 0
                        applied_zoom_seconds = 0.0
                        self._set_mode(Mode.INVESTIGATING)

                elif mode == Mode.INVESTIGATING:
                    frame_area = frame_shape[0] * frame_shape[1] if frame_shape is not None else 0
                    top = _largest_detection(last_detections)
                    self._set_last_detection(top)

                    if top is None:
                        self._reverse_zoom(onvif, applied_zoom_seconds)
                        move_issued = False
                        self._set_mode(Mode.SEARCHING)
                        continue

                    box_area = _box_area(top.box)
                    is_small = frame_area > 0 and (box_area / frame_area) < SMALL_BOX_AREA_RATIO

                    if is_small and zoom_attempts < MAX_ZOOM_ATTEMPTS:
                        zoom_attempts += 1
                        onvif.continuous_move(0, 0, ZOOM_PULSE_SPEED)
                        time.sleep(ZOOM_PULSE_SECONDS)
                        onvif.stop()
                        applied_zoom_seconds += ZOOM_PULSE_SECONDS
                        continue

                    if top.label.lower() == DRONE_LABEL and top.confidence >= get_confidence_threshold():
                        self._set_alarm(True)
                        last_detection_at = now
                        was_in_deadband = False
                        # Only fetch the actual full frame here (not every
                        # tick) since this is the one spot that genuinely
                        # needs pixel data, for the saved snapshot.
                        get_event_log().add(
                            "drone_detected",
                            f"confidence {top.confidence:.0%}",
                            frame=video.get_latest_frame_annotated(),
                        )
                        self._set_mode(Mode.TRACKING)
                    else:
                        self._reverse_zoom(onvif, applied_zoom_seconds)
                        move_issued = False
                        self._set_mode(Mode.SEARCHING)

                elif mode == Mode.TRACKING:
                    top = _largest_drone_detection(last_detections)
                    if top is not None:
                        last_detection_at = now
                        self._set_last_detection(top)
                        print(
                            f"[TRACK] box={top.box} confidence={top.confidence:.2f} "
                            f"since_last_gap={now - last_detection_at:.2f}s",
                            flush=True,
                        )
                        was_in_deadband = self._track_step(onvif, top, frame_shape, was_in_deadband)
                    elif now - last_detection_at >= LOST_TARGET_TIMEOUT_SECONDS:
                        print(
                            f"[TRACK] target lost -- no drone detection for "
                            f"{now - last_detection_at:.2f}s (timeout={LOST_TARGET_TIMEOUT_SECONDS}s)",
                            flush=True,
                        )
                        onvif.stop()
                        self._set_alarm(False)
                        self._set_last_detection(None)
                        move_issued = False
                        get_event_log().add("target_lost", "")
                        self._set_mode(Mode.SEARCHING)

                time.sleep(LOOP_TICK_SECONDS)
        except Exception as exc:
            print("[AUTONOMY] loop error, stopping camera:", flush=True)
            traceback.print_exc()
            get_event_log().add("loop_error", str(exc))
        finally:
            self._safe_stop_camera()
            self._set_alarm(False)
            self._set_last_detection(None)
            with self._lock:
                self._running = False
                self._mode = Mode.IDLE

    def _track_step(
        self,
        onvif,
        detection: Detection,
        frame_shape: Optional[tuple[int, int]],
        was_in_deadband: bool,
    ) -> bool:
        if frame_shape is None:
            return was_in_deadband

        frame_h, frame_w = frame_shape
        x1, y1, x2, y2 = detection.box
        frame_cx, frame_cy = frame_w / 2, frame_h / 2

        offset_x = ((x1 + x2) / 2 - frame_cx) / frame_cx if frame_cx else 0
        offset_y = ((y1 + y2) / 2 - frame_cy) / frame_cy if frame_cy else 0

        frame_area = frame_w * frame_h
        box_ratio = _box_area(detection.box) / frame_area if frame_area else 0

        pan = 0.0 if abs(offset_x) < TRACK_DEADBAND_RATIO else _clamp(
            offset_x, -TRACK_MAX_VELOCITY, TRACK_MAX_VELOCITY
        )
        tilt = 0.0 if abs(offset_y) < TRACK_DEADBAND_RATIO else _clamp(
            -offset_y, -TRACK_MAX_VELOCITY, TRACK_MAX_VELOCITY
        )
        if box_ratio < TARGET_BOX_AREA_RATIO * 0.6:
            zoom = TRACK_ZOOM_SPEED
        elif box_ratio > TARGET_BOX_AREA_RATIO * 1.4:
            zoom = -TRACK_ZOOM_SPEED
        else:
            zoom = 0.0

        print(
            f"[TRACK] offset_x={offset_x:.2f} offset_y={offset_y:.2f} "
            f"box_ratio={box_ratio:.3f} (target={TARGET_BOX_AREA_RATIO}) "
            f"-> pan={pan:.2f} tilt={tilt:.2f} zoom={zoom:.2f}",
            flush=True,
        )

        if pan == 0.0 and tilt == 0.0 and zoom == 0.0:
            if not was_in_deadband:
                onvif.stop()
            return True

        onvif.continuous_move(pan, tilt, zoom)
        return False


@lru_cache
def get_autonomy_controller() -> AutonomyController:
    return AutonomyController()
