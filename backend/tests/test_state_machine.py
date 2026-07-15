"""Offline smoke test of the AutonomyController state machine.

Runs without any third-party dependency and without camera hardware:
cv2/numpy/torch/ultralytics/onvif/pydantic_settings are stubbed, and the
real autonomy loop runs against a fake ONVIF client and controllable fake
detection results. Covers: investigation triggers (candidate + motion),
evidence-freshness gating, the confidence-seeking zoom loop and its give-up
path, tracking corrections and hysteresis, target loss and zoom restore,
immediate engagement when a drone is already in view at start, and stop()
responsiveness.

Run:  python3 backend/tests/test_state_machine.py
Exits nonzero on any failure. Timing-sensitive (drives the real 20Hz loop
in real time), so expect a ~20-30s wall-clock runtime.
"""
import os
import sys
import threading
import time
import types

# ---- stub heavy modules before importing the app ----
cv2 = types.SimpleNamespace(IMWRITE_JPEG_QUALITY=1, CAP_FFMPEG=0)
sys.modules["cv2"] = cv2

np = types.ModuleType("numpy")
np.ndarray = object
sys.modules["numpy"] = np

torch = types.SimpleNamespace(set_num_threads=lambda n: None)
sys.modules["torch"] = torch

ultra = types.ModuleType("ultralytics")
ultra.settings = types.SimpleNamespace(update=lambda d: None)
ultra.YOLO = object
sys.modules["ultralytics"] = ultra

onvif_mod = types.ModuleType("onvif")
onvif_mod.ONVIFCamera = object
sys.modules["onvif"] = onvif_mod

zeep_mod = types.ModuleType("zeep")
zeep_transports = types.ModuleType("zeep.transports")
zeep_transports.Transport = lambda **kwargs: types.SimpleNamespace(**kwargs)
zeep_mod.transports = zeep_transports
sys.modules["zeep"] = zeep_mod
sys.modules["zeep.transports"] = zeep_transports

ps = types.ModuleType("pydantic_settings")


class _BaseSettings:
    def __init__(self, **kwargs):
        for key, value in os.environ.items():
            setattr(self, key, value)
        for key, value in kwargs.items():
            setattr(self, key, value)


ps.BaseSettings = _BaseSettings
ps.SettingsConfigDict = lambda **kwargs: dict(kwargs)
sys.modules["pydantic_settings"] = ps

os.environ.update(
    NVR_IP="127.0.0.1", NVR_USERNAME="x", NVR_PASSWORD="x",
)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import app.autonomy as autonomy  # noqa: E402
from app.detection import Detection  # noqa: E402
from app.video_stream import DetectionResult  # noqa: E402


class FakeOnvif:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.moving = False
        # method name -> how many upcoming calls raise (SOAP fault injection)
        self.fail_next = {}
        # method name -> seconds the next call blocks (hung-SOAP injection)
        self.block_next = {}

    def _rec(self, *call):
        # Records the attempt first, then raises if a failure is queued --
        # so calls_of() counts attempts, letting scenarios assert retries.
        block = 0.0
        with self.lock:
            self.calls.append(call)
            remaining = self.fail_next.get(call[0], 0)
            if remaining > 0:
                self.fail_next[call[0]] = remaining - 1
                raise RuntimeError(f"injected {call[0]} failure")
            block = self.block_next.pop(call[0], 0.0)
        if block:
            time.sleep(block)  # outside the lock so calls_of() stays usable

    def continuous_move(self, pan, tilt, zoom):
        self._rec("continuous_move", pan, tilt, zoom)

    def absolute_move(self, pan, tilt, speed=None):
        self._rec("absolute_move", pan, tilt)
        self.moving = True

    def stop(self):
        self._rec("stop")
        self.moving = False

    def get_ptz_status(self):
        return {"pan": 0.0, "tilt": 0.0, "moving": self.moving}

    def calls_of(self, name):
        with self.lock:
            return [c for c in self.calls if c[0] == name]

    def clear(self):
        with self.lock:
            self.calls.clear()


class FakeVideo:
    def __init__(self):
        # Set to an old monotonic timestamp to simulate a frozen RTSP stream;
        # None means "frames are flowing" (always-fresh capture time).
        self.frozen_at = None

    def get_latest_frame_shape(self):
        return (720, 1280)

    def get_latest_frame_annotated(self):
        return None

    def get_latest_frame_captured_at(self):
        return self.frozen_at if self.frozen_at is not None else time.monotonic()


class SharedResult:
    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.seq = 0

    def publish(self, detections):
        with self.lock:
            self.seq += 1
            now = time.monotonic()
            self.result = DetectionResult(
                detections=detections, frame_seq=self.seq,
                frame_captured_at=now, completed_at=now, inference_seconds=0.1,
            )

    def get(self):
        with self.lock:
            return self.result


fake_onvif = FakeOnvif()
fake_video = FakeVideo()
shared = SharedResult()
motion = {"active": False, "confidence": 0.0}
verification = {"enabled": True}
events = []

autonomy.get_onvif_client = lambda: fake_onvif
autonomy.get_video_stream_manager = lambda: fake_video
autonomy.get_latest_detection_result = shared.get
autonomy.get_latest_detections = lambda: (shared.get().detections if shared.get() else [])
autonomy.get_motion_status = lambda: dict(motion)
autonomy.set_drone_verification_enabled = lambda v: verification.update(enabled=v)
autonomy.get_event_log = lambda: types.SimpleNamespace(
    add=lambda kind, detail, frame=None: events.append(kind)
)

Mode = autonomy.Mode
PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS: " if cond else "FAIL: ") + name, flush=True)


def wait_for(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    print(f"  (timeout waiting for: {what})", flush=True)
    return False


def drone(conf, box=(600, 320, 700, 420)):
    return Detection(label="drone", confidence=conf, box=box)


BIG_BOX = (200, 100, 1100, 650)  # large box: > SMALL ratio, passes size gate

ctl = autonomy.AutonomyController()

# --- scenario 1: plain scan issues waypoint moves ---
ctl.start(-0.5, 0.5, -0.2, 0.2)
check("scan starts in SEARCHING", ctl.mode == Mode.SEARCHING)
check("waypoint move issued", wait_for(lambda: fake_onvif.calls_of("absolute_move"), 2, "absolute_move"))
fake_onvif.moving = False  # arrival on next poll

# --- scenario 2: high-confidence candidate -> INVESTIGATING -> TRACKING ---
shared.publish([drone(0.9, BIG_BOX)])
check("candidate triggers INVESTIGATING or beyond",
      wait_for(lambda: ctl.mode in (Mode.INVESTIGATING, Mode.TRACKING), 2, "investigating"))
# fresh evidence: publish again so frame_captured_at postdates the mode entry + settle
ok = wait_for(lambda: ctl.mode == Mode.TRACKING or (time.monotonic(), shared.publish([drone(0.9, BIG_BOX)]))[0] is None, 5, "tracking")
check("confirmed drone -> TRACKING", ctl.mode == Mode.TRACKING)
check("alarm active while tracking", ctl.status()["alarm_active"])
check("verification disabled while tracking", verification["enabled"] is False)
check("drone_detected event logged", "drone_detected" in events)

# --- scenario 3: tracking correction issued on fresh result ---
fake_onvif.clear()
shared.publish([drone(0.9, (1000, 500, 1180, 650))])  # off-center small-ish box
check("tracking correction move issued",
      wait_for(lambda: fake_onvif.calls_of("continuous_move"), 2, "correction move"))

# --- scenario 4: target loss -> back to SEARCHING, alarm off, verification on ---
# stop publishing; loop should time out after LOST_TARGET_TIMEOUT_SECONDS (3s)
check("target loss returns to SEARCHING",
      wait_for(lambda: ctl.mode == Mode.SEARCHING, autonomy.LOST_TARGET_TIMEOUT_SECONDS + 3, "searching"))
check("alarm off after loss", not ctl.status()["alarm_active"])
check("verification re-enabled after loss", verification["enabled"] is True)
check("target_lost event logged", "target_lost" in events)

# --- scenario 5: low-confidence small candidate -> zoom-in pulse, then give up ---
fake_onvif.clear()
small_low = drone(0.2, (620, 350, 660, 390))  # tiny box, conf 0.2 (>=0.15 trigger, < 0.5 threshold)
shared.publish([small_low])
check("low-conf candidate triggers INVESTIGATING",
      wait_for(lambda: ctl.mode == Mode.INVESTIGATING, 3, "investigating(low)"))
publisher_stop = threading.Event()

def keep_publishing():
    while not publisher_stop.is_set():
        shared.publish([small_low])
        time.sleep(0.3)

t = threading.Thread(target=keep_publishing, daemon=True)
t.start()
check("zoom-in pulse issued during investigation",
      wait_for(lambda: any(c[3] > 0 for c in fake_onvif.calls_of("continuous_move")), 5, "zoom pulse"))
check("investigation gives up after attempt budget -> SEARCHING",
      wait_for(lambda: ctl.mode == Mode.SEARCHING, 15, "give up"))
zoom_out_after = any(c[3] < 0 for c in fake_onvif.calls_of("continuous_move"))
check("zoom restore (zoom-out) issued after failed investigation", zoom_out_after)

# --- scenario 5b: failed-investigation cooldown -- the still-visible
# unconfirmable candidate must NOT re-trigger investigation, and waypoint
# coverage must resume (the publisher keeps the candidate in view) ---
fake_onvif.clear()
reinvestigated = wait_for(lambda: ctl.mode == Mode.INVESTIGATING, 3, "re-investigation (should NOT happen)")
check("failed-investigation cooldown suppresses immediate re-trigger", not reinvestigated)
check("scan resumes waypoint coverage during cooldown",
      bool(fake_onvif.calls_of("absolute_move")))
publisher_stop.set()
t.join()

# --- scenario 6: stop() is responsive and resets state ---
started_stop = time.monotonic()
ctl.stop()
check("stop() completes quickly", time.monotonic() - started_stop < 2.0)
check("mode IDLE after stop", ctl.mode == Mode.IDLE)
check("camera stop issued on stop()", bool(fake_onvif.calls_of("stop")))

# --- scenario 7: start with drone already in view -> immediate TRACKING + alarm ---
shared.publish([drone(0.9, BIG_BOX)])
events.clear()
ctl2 = autonomy.AutonomyController()
ctl2.start(-0.5, 0.5, -0.2, 0.2)
check("engages immediately when drone already visible", ctl2.mode == Mode.TRACKING)
check("alarm active on immediate engage", ctl2.status()["alarm_active"])
check("immediate engage logged", "drone_detected" in events)
ctl2.stop()

# --- scenario 8: motion-only trigger -> INVESTIGATING, then stare times out ---
shared.publish([])
fake_onvif.clear()
ctl3 = autonomy.AutonomyController()
ctl3.start(-0.5, 0.5, -0.2, 0.2)
motion["active"] = True
check("motion triggers INVESTIGATING",
      wait_for(lambda: ctl3.mode == Mode.INVESTIGATING, 3, "motion investigate"))
motion["active"] = False

def publish_empty():
    while ctl3.mode == Mode.INVESTIGATING:
        shared.publish([])
        time.sleep(0.3)

t3 = threading.Thread(target=publish_empty, daemon=True)
t3.start()
check("motion stare times out back to SEARCHING",
      wait_for(lambda: ctl3.mode == Mode.SEARCHING, autonomy.MOTION_STARE_SECONDS + 3, "stare timeout"))
t3.join(timeout=1)
ctl3.stop()

# --- scenario 9: one transient PTZ SOAP failure is retried, scan survives ---
shared.publish([])
motion["active"] = False
fake_onvif.clear()
fake_onvif.moving = False
fake_onvif.fail_next["absolute_move"] = 1
ctl4 = autonomy.AutonomyController()
ctl4.start(-0.5, 0.5, -0.2, 0.2)
check("transient move failure is retried (two attempts recorded)",
      wait_for(lambda: len(fake_onvif.calls_of("absolute_move")) >= 2, 3, "retry attempt"))
check("scan still SEARCHING after transient failure", ctl4.mode == Mode.SEARCHING)
ctl4.stop()

# --- scenario 10: persistent PTZ failure -> fail-safe: loop lands in IDLE ---
fake_onvif.clear()
events.clear()
fake_onvif.fail_next["absolute_move"] = 2  # first attempt AND its retry fail
ctl5 = autonomy.AutonomyController()
ctl5.start(-0.5, 0.5, -0.2, 0.2)
check("persistent PTZ failure lands fail-safe in IDLE",
      wait_for(lambda: ctl5.mode == Mode.IDLE, 3, "fail-safe IDLE"))
check("loop_error event logged on persistent failure", "loop_error" in events)
check("failsafe camera stop attempted", bool(fake_onvif.calls_of("stop")))
ctl5.stop()

# --- scenario 11: frozen video pauses all autonomous motion, then resumes ---
shared.publish([])
fake_onvif.clear()
fake_onvif.moving = False
events.clear()
ctl6 = autonomy.AutonomyController()
ctl6.start(-0.5, 0.5, -0.2, 0.2)
check("scan active before freeze",
      wait_for(lambda: fake_onvif.calls_of("absolute_move"), 2, "pre-freeze move"))
fake_video.frozen_at = time.monotonic() - autonomy.VIDEO_STALE_AFTER_SECONDS - 1
check("video_stale event logged",
      wait_for(lambda: "video_stale" in events, 2, "video_stale event"))
check("camera stopped when video went stale", bool(fake_onvif.calls_of("stop")))
fake_onvif.clear()
moved_while_stale = wait_for(
    lambda: fake_onvif.calls_of("absolute_move"), 2.0, "moves while stale (should NOT happen)"
)
check("no autonomous moves while video is stale", not moved_while_stale)
check("still SEARCHING (paused, not dead) while stale", ctl6.mode == Mode.SEARCHING)
fake_video.frozen_at = None
check("video_recovered event logged",
      wait_for(lambda: "video_recovered" in events, 2, "video_recovered event"))
check("scan resumes waypoint coverage after recovery",
      wait_for(lambda: fake_onvif.calls_of("absolute_move"), 5, "post-recovery move"))
ctl6.stop()

# --- scenario 12: a hung camera call poisons the controller; start() is
# refused until the stuck loop thread actually dies (never two loops) ---
shared.publish([])
motion["active"] = False
fake_onvif.clear()
fake_onvif.moving = False
events.clear()
fake_onvif.block_next["absolute_move"] = 7.0  # longer than stop()'s 5s join
ctl7 = autonomy.AutonomyController()
ctl7.start(-0.5, 0.5, -0.2, 0.2)
check("blocking waypoint move issued",
      wait_for(lambda: fake_onvif.calls_of("absolute_move"), 2, "blocking move"))
stop_started = time.monotonic()
ctl7.stop()
stop_took = time.monotonic() - stop_started
check("stop() returns after the join window despite the stuck thread",
      4.0 <= stop_took <= 6.5)
refused = False
try:
    ctl7.start(-0.5, 0.5, -0.2, 0.2)
except RuntimeError:
    refused = True
check("start() refused while the previous loop thread is still alive", refused)
check("controller does not claim IDLE while poisoned", ctl7.mode != Mode.IDLE)
check("stuck-thread condition logged as loop_error event", "loop_error" in events)
check("loop exits to IDLE once the blocked call finally returns",
      wait_for(lambda: ctl7.mode == Mode.IDLE, 6, "post-block IDLE"))
restarted = False
try:
    ctl7.start(-0.5, 0.5, -0.2, 0.2)
    restarted = True
finally:
    ctl7.stop()
check("start() accepted again after the thread terminates", restarted)

# --- OnvifClient dead-man watchdog (real client, fake zeep PTZ service) ---
from datetime import timedelta  # noqa: E402

from app.onvif_client import OnvifClient  # noqa: E402


class FakePtzService:
    def __init__(self):
        self.ops = []
        self.reject_timeout = False

    def create_type(self, name):
        return types.SimpleNamespace(Timeout=None)

    def ContinuousMove(self, request):
        if self.reject_timeout and request.Timeout is not None:
            raise RuntimeError("ter:InvalidArgVal (Timeout)")
        self.ops.append(("ContinuousMove", request.Timeout))

    def Stop(self, request):
        self.ops.append(("Stop", None))


def make_ptz_client():
    client = OnvifClient()
    client._ptz_service = FakePtzService()
    client._profile = types.SimpleNamespace(
        token="prof",
        PTZConfiguration=types.SimpleNamespace(
            token="cfg", NodeToken="node", PanTiltLimits=None
        ),
        VideoSourceConfiguration=types.SimpleNamespace(SourceToken="src"),
    )
    return client


def stops_recorded(client):
    return [op for op in client._ptz_service.ops if op[0] == "Stop"]


# manual move arms the dead-man: Timeout element sent, stop fires unaided
client_a = make_ptz_client()
client_a.continuous_move(0.5, 0.0, 0.0, self_stop_seconds=0.4)
check("deadman: ONVIF Timeout element sent with manual move",
      client_a._ptz_service.ops[-1] == ("ContinuousMove", timedelta(seconds=0.4)))
check("deadman: watchdog stop fires when nothing follows",
      wait_for(lambda: stops_recorded(client_a), 1.5, "deadman stop"))
check("deadman: camera marked stopped after watchdog stop",
      client_a.is_camera_motion_settled(0.0))

# a follow-up command supersedes the pending dead-man (no spurious stop)
client_b = make_ptz_client()
client_b.continuous_move(0.5, 0.0, 0.0, self_stop_seconds=0.5)
time.sleep(0.2)
client_b.stop()  # operator released normally
stops_before = len(stops_recorded(client_b))
time.sleep(0.6)
check("deadman: superseded timer never fires a second stop",
      len(stops_recorded(client_b)) == stops_before)

# autonomy-style unbounded move (no self_stop) never schedules a watchdog
client_c = make_ptz_client()
client_c.continuous_move(0.3, 0.0, 0.0)
time.sleep(0.6)
check("deadman: not armed for autonomy moves (no self_stop)",
      not stops_recorded(client_c))
client_c.stop()

# NVR that rejects the optional Timeout: fallback move sent without it,
# support flag flips sticky, and the local watchdog still enforces the stop
client_d = make_ptz_client()
client_d._ptz_service.reject_timeout = True
client_d.continuous_move(0.5, 0.0, 0.0, self_stop_seconds=0.3)
check("deadman: Timeout rejection falls back to a plain move",
      ("ContinuousMove", None) in client_d._ptz_service.ops)
check("deadman: Timeout support flag flips sticky on rejection",
      client_d._continuous_move_timeout_supported is False)
check("deadman: local watchdog still stops without camera-side Timeout",
      wait_for(lambda: stops_recorded(client_d), 1.5, "fallback deadman stop"))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed", flush=True)
sys.exit(1 if FAIL else 0)
