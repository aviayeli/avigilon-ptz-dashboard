import threading
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Optional

from app.alarm import start_alarm, stop_alarm
from app.detection import Detection
from app.events import get_event_log
from app.onvif_client import get_onvif_client
from app.video_stream import (
    get_latest_detection_result,
    get_latest_detections,
    get_motion_status,
    get_video_stream_manager,
    set_drone_verification_enabled,
)

LOOP_TICK_SECONDS = 0.05

# Video staleness watchdog: if the newest captured frame is older than this,
# the system's "eyes" are frozen (RTSP stall or reconnect in progress) and
# every autonomous decision would be based on an old photograph -- so pause
# all autonomous motion instead of patrolling blind. 3s comfortably exceeds
# the worst measured detection cadence while staying operator-fast.
VIDEO_STALE_AFTER_SECONDS = 3.0

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

# Investigation: stop, stare, and adjust zoom until the detector's confidence
# clears the operator threshold or the attempt budget runs out. Zoom pulses
# are non-blocking (deadline-based) so the loop stays responsive to stop().
SMALL_BOX_AREA_RATIO = 0.05  # below this, zoom IN for more pixels
LARGE_BOX_AREA_RATIO = 0.35  # above this, zoom OUT for context
ZOOM_PULSE_SECONDS = 0.5
ZOOM_PULSE_SPEED = 0.5
MAX_ZOOM_ATTEMPTS = 4  # total pulses, either direction, per investigation
# Ignore drone candidates below this confidence as scan triggers -- without a
# floor, 1%-confidence noise constantly interrupts waypoint coverage.
INVESTIGATE_MIN_CONFIDENCE = 0.15
# After an investigation exhausts its zoom budget without confirming, the
# unconfirmable candidate is usually still in view -- without a cooldown the
# scan re-investigates it back-to-back forever and the raster never advances
# (observed live: minutes frozen on one false positive). Candidate triggers
# are suppressed for this long; motion triggers stay live so a genuinely
# arriving drone still interrupts the scan immediately.
INVESTIGATE_FAIL_COOLDOWN_SECONDS = 8.0
# Evidence freshness: only detection results computed from frames captured
# this long AFTER the camera finished its last adjustment count -- earlier
# results may describe the pre-adjustment view (inference runs at ~1Hz while
# this loop ticks at 20Hz).
POST_ADJUST_SETTLE_SECONDS = 0.2
INVESTIGATION_RESULT_TIMEOUT_SECONDS = 2.5  # ~2 inference cycles + margin
MOTION_STARE_SECONDS = 2.5  # how long a motion-only trigger holds the stare
# Pause at each waypoint so the settle-gated frame diff and the ~1Hz detector
# get a stationary look at the sector -- still frames also detect far better
# than motion-blurred mid-pan ones.
WAYPOINT_DWELL_SECONDS = 1.2
# Autonomy undoes its own net zoom-in when returning to the scan (the
# operator's original framing defines the coverage area). Clamped since the
# bookkeeping is approximate; overshoot just reaches the wide stop.
ZOOM_RESTORE_MAX_SECONDS = 6.0

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
# Hysteresis: once locked, hold the track down to this fraction of the
# confirm threshold, so momentary confidence dips (blur, partial occlusion)
# don't drop a lock that took an investigation to acquire.
TRACK_KEEP_CONFIDENCE_RATIO = 0.6
# Corrections are time-boxed: a command computed from an up-to-1s-old box
# must not keep running until the next result arrives. Move a little, stop,
# re-measure -- also keeps motion blur down for the next inference.
TRACK_CORRECTION_MAX_SECONDS = 0.45


class Mode(str, Enum):
    IDLE = "idle"
    SEARCHING = "searching"
    INVESTIGATING = "investigating"
    TRACKING = "tracking"


@dataclass
class _Investigation:
    # State for one investigation episode. wait_started_at is the evidence
    # freshness floor: only detection results computed from frames captured
    # after the camera finished its last adjustment count (see
    # POST_ADJUST_SETTLE_SECONDS). zoom_balance_seconds is the signed net
    # zoom time applied, so it can be undone on the way back to the scan.
    motion_triggered: bool
    started_at: float
    wait_started_at: float
    zoom_attempts: int = 0
    zoom_balance_seconds: float = 0.0
    pulse_ends_at: Optional[float] = None
    pulse_direction: float = 0.0


def _box_area(box: tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = box
    return max(0, x2 - x1) * max(0, y2 - y1)


def _largest_detection(detections: list[Detection]) -> Optional[Detection]:
    if not detections:
        return None
    return max(detections, key=lambda d: _box_area(d.box))


def _largest_drone_detection(
    detections: list[Detection], min_confidence: float = 0.0
) -> Optional[Detection]:
    drones = [
        d
        for d in detections
        if d.label.lower() == DRONE_LABEL and d.confidence >= min_confidence
    ]
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


def _ptz_with_retry(description: str, call, *args, **kwargs) -> None:
    # Fault boundary between control decisions and the SOAP transport: one
    # transient failure (bounded by the ONVIF client's operation timeout) is
    # retried immediately -- a single hiccup must not kill a live tracking
    # session. A second consecutive failure propagates to the loop's
    # fail-safe path (stop camera, land in IDLE). Deliberately no sleep
    # between attempts: stop() responsiveness outranks retry politeness.
    try:
        call(*args, **kwargs)
        return
    except Exception as exc:
        print(f"[AUTONOMY] {description} failed, retrying once: {exc}", flush=True)
    call(*args, **kwargs)


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


def _compute_track_velocities(
    detection: Detection, frame_shape: Optional[tuple[int, int]]
) -> tuple[float, float, float]:
    # Proportional control with a deadband: velocity scales with how far the
    # box center is from the frame center, zero inside the deadband so the
    # camera doesn't hunt around a well-centered target.
    if frame_shape is None:
        return 0.0, 0.0, 0.0

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
    return pan, tilt, zoom


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

    def start(self, pan_min: float, pan_max: float, tilt_min: float, tilt_max: float) -> None:
        # Poisoned-controller guard: a previous session's loop thread that
        # outlived its stop() join (blocked in a SOAP call, bounded by the
        # transport timeouts) may still issue camera commands until it dies.
        # Two loops must never command the camera concurrently, so refuse
        # to start a new session while the old thread is alive.
        stale = self._thread
        if stale is not None and stale.is_alive():
            raise RuntimeError(
                "previous autonomy session is still shutting down "
                "(camera call in flight) -- try again in a few seconds"
            )

        # If a drone is already visible when the operator starts the system
        # (e.g. a previous tracking session just ended but the drone never
        # left), engage it immediately: lock on, alarm, track. Detection runs
        # continuously regardless of autonomy state, so this reflects the
        # live view at this moment.
        engage_immediately = is_drone_currently_present()

        with self._lock:
            if self._running:
                return
            self._pan_min = pan_min
            self._pan_max = pan_max
            self._tilt_min = tilt_min
            self._tilt_max = tilt_max
            self._running = True
            self._mode = Mode.TRACKING if engage_immediately else Mode.SEARCHING
        get_event_log().add("search_started", f"pan {pan_min:.0f}..{pan_max:.0f}, tilt {tilt_min:.0f}..{tilt_max:.0f}")
        if engage_immediately:
            set_drone_verification_enabled(False)
            self._set_alarm(True)
            get_event_log().add(
                "drone_detected",
                "already in view at start",
                frame=get_video_stream_manager().get_latest_frame_annotated(),
            )
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        was_running = self._is_running()
        with self._lock:
            self._running = False
        thread = self._thread
        if thread is not None:
            # A loop blocked inside a slow SOAP call (bounded by the
            # transport timeouts) can outlive this join. Repeat stop()
            # calls -- e.g. one per manual command while poisoned -- use a
            # short join so operator commands aren't each delayed by the
            # full window.
            thread.join(timeout=5.0 if was_running else 0.5)
            if thread.is_alive():
                print(
                    "[AUTONOMY] loop thread did not exit in time (blocked "
                    "camera call?) -- controller poisoned: new sessions are "
                    "refused until it terminates",
                    flush=True,
                )
                get_event_log().add(
                    "loop_error",
                    "autonomy thread stuck in a camera call; wait a few "
                    "seconds and retry (restart the server if it persists)",
                )
            else:
                self._thread = None
        set_drone_verification_enabled(True)
        self._safe_stop_camera()
        self._set_alarm(False)
        self._set_last_detection(None)
        # Only claim IDLE when the loop thread is confirmed dead: IDLE is
        # what lets take_manual_control() hand the camera to the operator,
        # and a still-alive loop could otherwise command it concurrently.
        # (A poisoned loop's own finally-block sets IDLE when it exits.)
        if self._thread is None:
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
        except Exception as exc:
            # Swallowed on purpose (this is the stop-of-last-resort on every
            # exit path, and raising here would mask the original error) but
            # never silent: if even the failsafe stop failed, this is the
            # most important line in the session log.
            print(f"[AUTONOMY] FAILSAFE camera stop failed: {exc}", flush=True)

    def _begin_zoom_restore(self, onvif, zoom_balance_seconds: float, now: float) -> Optional[float]:
        # Absolute zoom restore isn't possible (AbsoluteMove zoom is
        # unverified on this hardware), so undo autonomy's own net zoom-in
        # time at the speed it was applied with. Returns the monotonic
        # deadline at which the zoom-out must be stopped (the SEARCHING
        # branch enforces it -- no blocking sleep, so stop() stays
        # responsive), or None if there is nothing to undo. Overshoot is
        # harmless: the lens just reaches its wide stop.
        seconds = _clamp(zoom_balance_seconds, 0.0, ZOOM_RESTORE_MAX_SECONDS)
        if seconds <= 0:
            return None
        _ptz_with_retry("zoom restore", onvif.continuous_move, 0, 0, -ZOOM_PULSE_SPEED)
        return now + seconds

    def _run_loop(self) -> None:
        onvif = get_onvif_client()
        video = get_video_stream_manager()

        waypoints = _build_raster_waypoints(self._pan_min, self._pan_max, self._tilt_min, self._tilt_max)
        waypoint_heat = [0.0] * len(waypoints)
        waypoint_index = 0
        move_issued = False
        move_started_at = time.monotonic()
        last_status_poll_at = 0.0
        dwell_until = 0.0

        investigation: Optional[_Investigation] = None
        zoom_restore_ends_at: Optional[float] = None
        candidate_suppressed_until = 0.0

        last_detection_at = time.monotonic()
        last_consumed_seq = -1
        correction_ends_at: Optional[float] = None
        correction_started_at = 0.0
        correction_zoom_direction = 0.0
        track_zoom_balance = 0.0

        def book_correction_zoom(ended_at: float) -> None:
            # Approximate signed seconds-at-pulse-speed bookkeeping so the
            # net zoom applied while tracking can be undone on target loss.
            nonlocal track_zoom_balance
            if correction_zoom_direction != 0.0:
                track_zoom_balance += (
                    correction_zoom_direction
                    * (ended_at - correction_started_at)
                    * (TRACK_ZOOM_SPEED / ZOOM_PULSE_SPEED)
                )

        video_stale = False

        try:
            while self._is_running():
                now = time.monotonic()

                # Staleness watchdog -- never scan blind. None (no frame ever,
                # e.g. stream still connecting at start) is deliberately not
                # treated as stale: that preserves existing start-up behavior;
                # this gate targets a stream that WAS live and then froze.
                frame_captured_at = video.get_latest_frame_captured_at()
                stale = (
                    frame_captured_at is not None
                    and now - frame_captured_at >= VIDEO_STALE_AFTER_SECONDS
                )
                if stale and not video_stale:
                    video_stale = True
                    age = now - frame_captured_at
                    print(
                        f"[AUTONOMY] video stale ({age:.1f}s without a new frame) "
                        "-- pausing autonomous motion",
                        flush=True,
                    )
                    get_event_log().add("video_stale", f"{age:.1f}s without a new frame")
                    _ptz_with_retry("stale-video stop", onvif.stop)
                    if correction_ends_at is not None:
                        book_correction_zoom(now)
                        correction_ends_at = None
                        correction_zoom_direction = 0.0
                    move_issued = False
                elif not stale and video_stale:
                    video_stale = False
                    print("[AUTONOMY] video recovered -- resuming", flush=True)
                    get_event_log().add("video_recovered", "")
                if video_stale:
                    time.sleep(LOOP_TICK_SECONDS)
                    continue

                # Only the frame's dimensions are ever needed here (never
                # pixel data), so use the cheap shape-only accessor instead
                # of copying the full ~6MB frame on every tick (20x/sec).
                frame_shape = video.get_latest_frame_shape()
                # Detection runs continuously in the video pipeline
                # (VideoStreamManager._detection_loop), independent of search
                # state. This loop consumes the shared result, using its
                # frame sequence/timestamp to reason about freshness --
                # inference runs at ~1Hz while this loop ticks at 20Hz.
                result = get_latest_detection_result()

                mode = self.mode

                if mode == Mode.SEARCHING:
                    if zoom_restore_ends_at is not None:
                        # Undoing zoom left over from an investigation or a
                        # lost track before resuming waypoint coverage.
                        if now >= zoom_restore_ends_at:
                            _ptz_with_retry("zoom restore stop", onvif.stop)
                            zoom_restore_ends_at = None
                    else:
                        candidate = (
                            _largest_drone_detection(result.detections, INVESTIGATE_MIN_CONFIDENCE)
                            if result is not None and now >= candidate_suppressed_until
                            else None
                        )
                        # Motion is suppressed while the camera itself moves
                        # (video_stream gates on OnvifClient motion state), so
                        # an active reading means something moved within a
                        # stationary view -- a real investigation trigger,
                        # not scan-induced pixel churn.
                        motion_active = get_motion_status().get("active", False)

                        if candidate is not None or motion_active:
                            waypoint_heat[waypoint_index] = min(
                                HEAT_MAX, waypoint_heat[waypoint_index] + HEAT_INCREMENT
                            )
                            _ptz_with_retry("investigation stop", onvif.stop)
                            move_issued = False
                            self._set_last_detection(candidate)
                            investigation = _Investigation(
                                motion_triggered=candidate is None,
                                started_at=now,
                                wait_started_at=now,
                            )
                            trigger = (
                                "motion"
                                if candidate is None
                                else f"candidate confidence={candidate.confidence:.2f}"
                            )
                            print(f"[AUTONOMY] investigating ({trigger})", flush=True)
                            self._set_mode(Mode.INVESTIGATING)
                        elif not move_issued:
                            if now >= dwell_until:
                                pan, tilt = waypoints[waypoint_index]
                                speed = (
                                    SCAN_SPEED_SLOW
                                    if waypoint_heat[waypoint_index] >= HEAT_REVISIT_THRESHOLD
                                    else SCAN_SPEED_FAST
                                )
                                print(
                                    f"[AUTONOMY] scan -> waypoint {waypoint_index} "
                                    f"pan={pan:.2f} tilt={tilt:.2f} speed={speed:.2f}",
                                    flush=True,
                                )
                                _ptz_with_retry(
                                    "waypoint move", onvif.absolute_move, pan, tilt, speed=speed
                                )
                                move_issued = True
                                move_started_at = now
                                last_status_poll_at = now
                        elif now - last_status_poll_at >= PTZ_STATUS_POLL_INTERVAL_SECONDS:
                            last_status_poll_at = now
                            try:
                                arrived = not onvif.get_ptz_status()["moving"]
                            except Exception:
                                arrived = True  # can't tell -- don't get stuck here forever
                            if arrived or now - move_started_at >= PTZ_MOVE_TIMEOUT_SECONDS:
                                print(
                                    f"[AUTONOMY] waypoint {waypoint_index} reached "
                                    f"after {now - move_started_at:.1f}s (arrived={arrived})",
                                    flush=True,
                                )
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
                                # Dwell before the next hop: the settle-gated
                                # frame diff and the ~1Hz detector both need a
                                # stationary look at this sector, and a still
                                # frame detects far better than a mid-pan,
                                # motion-blurred one.
                                dwell_until = now + WAYPOINT_DWELL_SECONDS

                elif mode == Mode.INVESTIGATING and investigation is not None:
                    inv = investigation
                    if inv.pulse_ends_at is not None:
                        # A zoom pulse is in flight; end it on schedule. No
                        # blocking sleep, so stop() takes effect within a tick.
                        if now >= inv.pulse_ends_at:
                            _ptz_with_retry("zoom pulse stop", onvif.stop)
                            inv.zoom_balance_seconds += inv.pulse_direction * ZOOM_PULSE_SECONDS
                            inv.pulse_ends_at = None
                            inv.wait_started_at = now
                    else:
                        # Evidence must postdate the camera's last adjustment
                        # plus a settle margin -- an older result may describe
                        # the pre-adjustment view.
                        fresh = (
                            result is not None
                            and result.frame_captured_at
                            >= inv.wait_started_at + POST_ADJUST_SETTLE_SECONDS
                        )
                        end_investigation = False
                        end_reason = ""
                        if not fresh:
                            if now - inv.wait_started_at >= INVESTIGATION_RESULT_TIMEOUT_SECONDS:
                                # Detector produced nothing usable in time.
                                end_investigation = True
                                end_reason = "no fresh detection result"
                        else:
                            top = _largest_drone_detection(result.detections)
                            self._set_last_detection(top)
                            if top is not None and top.confidence >= get_confidence_threshold():
                                # Confirmed. (The COCO false-positive filter
                                # already vetted this result upstream.)
                                self._set_alarm(True)
                                last_detection_at = now
                                last_consumed_seq = result.frame_seq
                                track_zoom_balance = inv.zoom_balance_seconds
                                correction_ends_at = None
                                correction_zoom_direction = 0.0
                                # Only fetch the actual full frame here (not
                                # every tick): this is the one spot that needs
                                # pixel data, for the saved snapshot.
                                get_event_log().add(
                                    "drone_detected",
                                    f"confidence {top.confidence:.0%}",
                                    frame=video.get_latest_frame_annotated(),
                                )
                                # Skip COCO verification while a confirmed
                                # target is being tracked.
                                set_drone_verification_enabled(False)
                                investigation = None
                                self._set_mode(Mode.TRACKING)
                            elif top is None:
                                # A motion-triggered stare waits out its
                                # window (the ~1Hz detector may need another
                                # pass at the now-stationary scene); a
                                # vanished candidate ends immediately.
                                if not (
                                    inv.motion_triggered
                                    and now - inv.started_at < MOTION_STARE_SECONDS
                                ):
                                    end_investigation = True
                                    end_reason = (
                                        "stare window expired"
                                        if inv.motion_triggered
                                        else "candidate vanished"
                                    )
                            elif inv.zoom_attempts >= MAX_ZOOM_ATTEMPTS:
                                # Budget exhausted without clearing the
                                # confidence bar: not identifiable as a drone.
                                end_investigation = True
                                end_reason = "attempt budget exhausted"
                            else:
                                # Below the bar: adjust zoom in the direction
                                # most likely to help -- in for more pixels on
                                # a small/mid target, out for context when the
                                # box already fills the view.
                                frame_area = (
                                    frame_shape[0] * frame_shape[1]
                                    if frame_shape is not None
                                    else 0
                                )
                                box_ratio = _box_area(top.box) / frame_area if frame_area else 0.0
                                direction = -1.0 if box_ratio > LARGE_BOX_AREA_RATIO else 1.0
                                inv.zoom_attempts += 1
                                print(
                                    f"[AUTONOMY] investigate zoom "
                                    f"{'out' if direction < 0 else 'in'} "
                                    f"(attempt {inv.zoom_attempts}/{MAX_ZOOM_ATTEMPTS}, "
                                    f"confidence={top.confidence:.2f}, box_ratio={box_ratio:.3f})",
                                    flush=True,
                                )
                                _ptz_with_retry(
                                    "zoom pulse",
                                    onvif.continuous_move, 0, 0, direction * ZOOM_PULSE_SPEED,
                                )
                                inv.pulse_direction = direction
                                inv.pulse_ends_at = now + ZOOM_PULSE_SECONDS

                        if end_investigation:
                            print(
                                f"[AUTONOMY] investigation ended ({end_reason}) "
                                f"after {now - inv.started_at:.1f}s, "
                                f"zoom_attempts={inv.zoom_attempts}",
                                flush=True,
                            )
                            if end_reason == "attempt budget exhausted":
                                candidate_suppressed_until = (
                                    now + INVESTIGATE_FAIL_COOLDOWN_SECONDS
                                )
                            zoom_restore_ends_at = self._begin_zoom_restore(
                                onvif, inv.zoom_balance_seconds, now
                            )
                            investigation = None
                            move_issued = False
                            self._set_last_detection(None)
                            self._set_mode(Mode.SEARCHING)

                elif mode == Mode.TRACKING:
                    if result is not None and result.frame_seq != last_consumed_seq:
                        last_consumed_seq = result.frame_seq
                        # Hysteresis: hold the track at a fraction of the
                        # confirm threshold so momentary confidence dips
                        # (motion blur, partial occlusion) don't drop a lock
                        # that took work to acquire.
                        top = _largest_drone_detection(
                            result.detections,
                            get_confidence_threshold() * TRACK_KEEP_CONFIDENCE_RATIO,
                        )
                        if top is not None:
                            gap = now - last_detection_at
                            last_detection_at = now
                            self._set_last_detection(top)
                            print(
                                f"[TRACK] box={top.box} confidence={top.confidence:.2f} "
                                f"gap_since_previous={gap:.2f}s",
                                flush=True,
                            )
                            pan, tilt, zoom = _compute_track_velocities(top, frame_shape)
                            if pan == 0.0 and tilt == 0.0 and zoom == 0.0:
                                if correction_ends_at is not None:
                                    _ptz_with_retry("deadband stop", onvif.stop)
                                    book_correction_zoom(now)
                                    correction_ends_at = None
                                    correction_zoom_direction = 0.0
                            else:
                                if correction_ends_at is not None:
                                    # Replacing a correction still in flight:
                                    # book its zoom time first.
                                    book_correction_zoom(now)
                                _ptz_with_retry(
                                    "tracking correction", onvif.continuous_move, pan, tilt, zoom
                                )
                                correction_started_at = now
                                correction_zoom_direction = (
                                    0.0 if zoom == 0.0 else (1.0 if zoom > 0 else -1.0)
                                )
                                # Time-box the correction instead of letting a
                                # command computed from an up-to-1s-old box run
                                # until the next result arrives: move a
                                # little, stop, re-measure. This also keeps
                                # motion blur down for the next inference.
                                correction_ends_at = now + TRACK_CORRECTION_MAX_SECONDS

                    if correction_ends_at is not None and now >= correction_ends_at:
                        _ptz_with_retry("correction stop", onvif.stop)
                        book_correction_zoom(now)
                        correction_ends_at = None
                        correction_zoom_direction = 0.0

                    if now - last_detection_at >= LOST_TARGET_TIMEOUT_SECONDS:
                        print(
                            f"[TRACK] target lost -- no qualifying detection for "
                            f"{now - last_detection_at:.2f}s (timeout={LOST_TARGET_TIMEOUT_SECONDS}s)",
                            flush=True,
                        )
                        _ptz_with_retry("target-lost stop", onvif.stop)
                        if correction_ends_at is not None:
                            book_correction_zoom(now)
                            correction_ends_at = None
                            correction_zoom_direction = 0.0
                        self._set_alarm(False)
                        self._set_last_detection(None)
                        get_event_log().add("target_lost", "")
                        set_drone_verification_enabled(True)
                        zoom_restore_ends_at = self._begin_zoom_restore(onvif, track_zoom_balance, now)
                        track_zoom_balance = 0.0
                        move_issued = False
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
            set_drone_verification_enabled(True)
            with self._lock:
                self._running = False
                self._mode = Mode.IDLE


@lru_cache
def get_autonomy_controller() -> AutonomyController:
    return AutonomyController()
