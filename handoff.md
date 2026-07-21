# handoff.md — Architect Handoff: Avigilon PTZ Drone-Detection Dashboard

**From:** Claude (AI Chief Software Architect), WSL2 development environment, 2026-07-13
**To:** The Claude instance on the Windows deployment machine (hardware-test day)
**Repo:** https://github.com/aviayeli/avigilon-ptz-dashboard — everything below is pushed to `origin/master` (HEAD `529f460`).

---

## 0. Status Update — 2026-07-21 (motion-first lock-on rewrite, on-site)

Operator-reported spec gap: a drone crossed the FOV and the system never
locked on, because tracking only began AFTER classification confirmed a
drone, and motion alone triggered a stationary "stare". **Behavior
inverted (approved by Avi):** any motion in a stationary view — or a
low-confidence drone candidate — now immediately locks the camera onto the
object and follows it (centering + zoom-sizing corrections + autofocus)
while classification runs *during* the follow. Classification is an
outcome of tracking, not a precondition.

**INVESTIGATING was rewritten** from stare/zoom-pulse to a follow-and-
identify loop. Its exits:
- drone candidate ≥ operator threshold → confirm → TRACKING + alarm
  (unchanged handoff into the existing TRACKING branch).
- COCO cross-check positively identifies the locked region as a non-drone
  (car/person/…) on ≥2 consecutive results (`NON_DRONE_ID_*`) → abandon,
  log `object_identified`, cooldown, resume scan. No alarm.
- object leaves FOV (no evidence for `LOST_TARGET_TIMEOUT_SECONDS`=3s) →
  resume scan, no cooldown.
- never identified within `UNIDENTIFIED_TRACK_BUDGET_SECONDS`=30s (bounded
  so the scan keeps covering the area) → give up, cooldown, resume.

**Supporting changes:**
- `video_stream._update_motion()` now localizes the largest moving region
  (dilate + `findContours` + `boundingRect`) and publishes `box`/`at`
  alongside `active`/`confidence`. Autonomy follows the motion box when the
  classifier has nothing that tick.
- `detection.filter_false_positive_drones()` now returns `(verified,
  rejected)` where `rejected` is `[(box, coco_label)]`; plumbed through
  `DetectionResult.rejected`. This is the channel that makes non-drone
  identification observable to autonomy (it used to silently drop them).
- Removed: the zoom-pulse investigation machinery
  (`MAX_ZOOM_ATTEMPTS`, `MOTION_STARE_SECONDS`, `_Investigation` pulse
  fields, `POST_ADJUST_SETTLE_SECONDS`, `investigate zoom` log lines).
  The end-of-investigation line now reports `corrections=` not
  `zoom_attempts=`; `summarize_telemetry.py` updated to match.
- Frontend: INVESTIGATING label → "עוקב אחר עצם לא מזוהה…"; new
  `object_identified` event label.

**Verified live (2026-07-21):** repeated lock-ons on real moving objects —
the camera now actively FOLLOWS (7 corrections over 10s in one episode)
instead of the old 2.5s stationary stare, exits cleanly on "left field of
view" with no false alarm. Suite **59/59** (was 53); confirmed stable
across 12 consecutive runs (fixed one flaky test that leaked scenario 5's
stale mock detection into scenario 5c). Must stay 59/59.

**Still open (unchanged by this work):** close-approach confidence
collapse — the reason lock-ons on a real drone may still not reach the
confirm threshold is the model, not the control loop. The P0 recording +
retraining mission (below) remains the top lever.

---

## 0.1. Status Update — 2026-07-14 evening (post-test, WSL2 dev machine)

Written after the hardware-test day, for the instance running the
2026-07-15 field session. Where this contradicts anything below, this wins.

**⚠ Hardware-model correction:** ONVIF `GotoHomePosition` **works** on this
camera (operator-verified in the field). Every older claim that it is a
no-op — below in §0.1 and in old code comments — is wrong. `goto_home()`
now marks camera motion and blocks on `wait_until_stopped()` until the
sweep settles (commit `c5123bb`), fixing a motion-suppression leak.

**Shipped since the test session:**
1. **UI redesign** (`8d288fd`): new tactical-dark markup/CSS from the Claude
   Design project. Same DOM contract — zero JS changes (verified: all 39
   ids + every selector/state class). Google Fonts links deliberately
   dropped (isolated network); `theme.css` deleted. **The zero-page-scroll
   check at the ops machine's real 1920×1080 must be redone** (§3 step 2).
2. **ONVIF capability discovery** (`a4a7f2e`): boot now logs `[ONVIF]`
   lines — absolute pan/tilt + zoom position spaces with ranges,
   `HomeSupported`, `FixedHomePosition`. `get_pan_tilt_limits()` is now
   the intersection of the generic space range and configured
   `PanTiltLimits`. **Grep the session log for `[ONVIF]` and record the
   values** — they gate the pending work below.

**2026-07-15 field checklist (agreed with Avi):**
1. Boot against the hardware; verify the redesigned UI (zero scroll,
   alarm banner, PTZ bindings) and capture the `[ONVIF]` lines.
2. **P0 recording mission:** close-approach drone passes via
   `backend/scripts/record_training_frames.py` (+ no-drone negatives).
   Telemetry shows model confidence collapses as box_ratio grows past
   ~0.6 (0.66→0.35→nothing; boxes clipped at frame edges) — the fix plan
   is P1 size-conditioned tracking-confidence floor, then P2 state-driven
   **padded** inference (padding, not downscaling: Ultralytics letterboxes
   to 640 regardless, so pre-downscaling can't shrink the drone's relative
   size). Check close frames for blur (focus?) vs. sharp silhouette.
3. Wiper Phase A item 2/3 (contamination + rain) if conditions allow.

**Pending, evidence-gated (do not start before the `[ONVIF]` log exists):**
steps 3–5 of the coordinate-mapping plan — home-on-boot + Set Center →
`calibrate_center()` (behind a `HOME_ON_BOOT` flag; note the button's
semantics change), wrap-aware pan mapping (pan is 360° continuous — the
current clamp amputates seam-crossing sectors), degrees UI only if a
degrees space is reported.

**Pre-field-test stability batch (2026-07-14 late, commits `6d9dae0`…
`270c3c9`):** bounded SOAP timeouts on every ONVIF transport (5s op/10s
connect — a hung NVR TCP connection can no longer freeze the autonomy loop
mid-move); retry-once fault boundary on all loop-issued PTZ calls (one
transient SOAP error no longer kills a live track; two consecutive still
fail-safe to IDLE); RTSP socket receive timeout + **video staleness
watchdog** (frame older than 3s → camera stopped, `video_stale` event,
autonomous motion paused until `video_recovered` — never scan blind); boot
refuses to start if either `best_merged.pt` or `yolov8n.pt` is missing from
`backend/` (offline network — no auto-download). Suite is now **38/38**
(was 26).

**Phantom-movement defenses (2026-07-15, commits `1860d04`…`0b9cef0`),
after the operator reported the camera moving while the dashboard was
IDLE:**
- `[PTZ]` audit logging at the OnvifClient choke point — every backend
  motion command is now a timestamped session-log line. **Diagnostic rule:
  physical motion with no `[PTZ]` line and no `/api/ptz` POST in the same
  window = externally commanded** (check camera Park Action / auto-tracking
  / tours, ACC rules, other clients).
- Boot-time best-effort Stop — a fresh server never inherits stale motion
  from a hard-killed predecessor.
- 5s dead-man on manual moves (ONVIF ContinuousMove Timeout + local
  watchdog + 2s frontend keepalive) — a tab that dies mid-hold can no
  longer leave the camera moving. **Field-verify:** hold a d-pad button
  >5s (must keep moving), and check the log for "NVR rejected
  ContinuousMove Timeout" (fallback engaged is fine, just note it).
- Poisoned-controller guard — stop() no longer claims IDLE while the loop
  thread is stuck in a camera call; start() returns 503 until it dies
  (bounded by the 5s SOAP timeouts — retry, don't restart).

Suite is now **53/53** — it must stay 53/53.

---

## 0.1. Status Update — 2026-07-14 (hardware-test day, written on-site)

The §3 protocol was executed against the real camera. Where this section
contradicts the older sections below, this section wins.

**Verified working on hardware:** boot + session logging, live stream,
detection boxes, manual PTZ (d-pad / zoom / focus / wiper / IR — operator:
"all working"), single-screen layout at 1920×1080 with zero page scroll,
and the full autonomous chain: search → investigate → confirm →
track + alarm → target lost → zoom restore → re-search. The server alarm
fired repeatedly during real tracking (operator raised no audibility
issue). The regression suite grew 24 → **26 scenarios** (investigation
cooldown); it must stay 26/26.

**Live findings → fixes shipped today (all evidence in
`backend/logs/session-20260714-*.log` + `backend/snapshots/`):**
1. **Car-as-drone false positive** — snapshot shows a 72% "drone" box that
   is mostly a parked car. Root cause: the COCO cross-check list had only
   indoor labels. Fix: `NON_DRONE_OBJECT_LABELS` (detection.py) now also
   has person/car/truck/bus/motorcycle/bicycle/train/boat. Deliberately
   NOT added: bird/airplane/kite — COCO calls real drones those.
2. **Filter flicker leak** — yolov8n's read of the contradicting object
   flickers frame to frame, so the same candidate leaked through every few
   seconds (a dozen ~1.5s micro-investigations in 3 minutes). Fix: 10s
   rejection memory (IoU 0.4) in the filter, cleared on any camera motion,
   never refreshed by memory hits so it can't suppress a spot forever.
3. **"Scan stuck"** (operator report) — failed (budget-exhausted)
   investigations re-triggered back-to-back on the same still-visible
   candidate; the raster made no progress for minutes. Fix:
   `INVESTIGATE_FAIL_COOLDOWN_SECONDS = 8.0` suppresses candidate triggers
   after a budget-exhausted end; motion triggers stay live.
4. This machine's local `.env` still had the removed auth-era keys
   (`DASHBOARD_*`, `SESSION_SECRET_KEY`) → pydantic `extra_forbidden`
   crash at boot. Cleaned. Same symptom on another deployment = same cause.

**Open issues (telemetry-backed, deliberately not yet fixed):**
- **Close-target tracking loss** (reproduced twice): an approaching drone
  outgrows the frame (box ratio 0.475 → 0.82) faster than the fixed
  0.2-velocity zoom-out corrects, the model's confidence collapses on
  frame-filling targets, → 3s timeout. At close range the lens was already
  at its wide stop — box_ratio sat pinned ~0.47 across 10+ zoom-out pulses
  (physical no-op), so control tuning cannot fully fix this; model
  retraining on close-range frames is the real lever.
- `_largest_drone_detection` selects by box area, which actively favors
  close clutter over a small real drone (operator watched the camera
  prefer a false positive over the actual drone). Candidate change:
  confidence-based selection — but it interacts with tracking continuity,
  so do it as its own measured change.
- Waypoint arrivals look suspiciously fast: full-width pans reported
  "reached" in 0.4s (one status poll). Either genuinely fast preset moves
  or a GetStatus race (polled before the camera starts reporting motion).
  The 1.2s dwell masks it either way; physically unverified.
- The NVR channel delivers ~9 fps @ ~0.9 Mb/s, not 25/30 — NVR config,
  not a bug.

**Wiper Phase A (§4.1) answered:** `On` runs **one self-terminating cycle
per press** — the fire-and-forget ideal; no Off-timer logic needed for a
future auto-wiper. Cycle length not yet timed.

**Model retraining is now the top lever** — remaining misses/false fires
are model quality, not control logic. New tools (operator will record and
retrain `best_merged.pt` on a stronger machine):
- `backend/scripts/record_training_video.py` — stream-copies the native
  H.264 to `backend/training_data/*.mp4` (~0 CPU; needs
  `pip install imageio-ffmpeg`, already in this machine's venv; `-an`
  because the NVR's pcm_mulaw audio track can't be copied into MP4).
- `backend/scripts/record_training_frames.py` — clean full-res JPEGs at a
  fixed interval, for direct labeling.
- Collection guidance: fly where detection fails (near the cars, very
  close to the camera, far against sky) AND record no-drone scenes as
  background negatives — that is what teaches the model the car is not a
  drone. Event snapshots in `backend/snapshots/` have detection boxes
  burned in — useful for failure review, **unusable as training images**.

---

## 1. Your Role & Directives

You are the **AI Chief Software Architect, Engineering Manager, and Technical Lead** for this project. Before doing anything else, read **`.claude/ai_architect_guidelines.md`** in the repo root and follow it strictly. Non-negotiables from it, proven valuable this week:

- Mission priority order: **Correctness → Maintainability → Simplicity → Performance → Smart Resource Allocation.** Never trade correctness for speed.
- Understand code before changing it; minimal diffs; root cause over symptom.
- Delegate isolated, well-specified tasks to cheaper worker models, but **you must review every delegated result yourself before accepting it** — reviews this week caught real bugs (a stuck motion-suppression flag, missing constants that crashed the tracking loop).
- **No authentication, ever** (login/session code was deliberately removed — do not reintroduce or suggest it). No unrequested features (no Docker, DBs, admin panels, etc.).
- Meaningful atomic commits; push when work is complete and verified.
- **Physical safety:** this machine drives a real motorized camera. Any change touching `continuous_move`/`absolute_move` paths must be tested cautiously. The autonomy loop's `finally` block and the FastAPI shutdown handler both stop the camera — never weaken those.

## 2. System Architecture & Current State

**Stack:** FastAPI backend (Python, `backend/app/`), vanilla Hebrew-RTL HTML/CSS/JS frontend (`frontend/`, no build step), served by the backend. Camera reached via NVR: ONVIF (`onvif-zeep`) for PTZ, RTSP-over-TCP (OpenCV/FFmpeg) for video. YOLO (Ultralytics) for detection: custom single-class drone model `best_merged.pt`, cross-checked against COCO `yolov8n.pt` to reject stationary-object false positives.

**Everything autonomous is custom-built — the camera's own tours/auto-tracking are NOT used.** ONVIF is used only for motion primitives.

Key design decisions (all deliberate — don't "fix" them without evidence):

- **State machine** (`backend/app/autonomy.py`): IDLE → SEARCHING → INVESTIGATING → TRACKING, in a 20 Hz loop on its own thread.
  - *SEARCHING:* boustrophedon raster over operator-set pan/tilt range via `AbsoluteMove` + 0.4s status polling (8s move timeout); **dwells ~1.2s per waypoint** so detection sees stationary frames; decaying "heat" prioritizes active sectors. Triggers to investigate: drone candidate ≥ 15% confidence, or motion.
  - *INVESTIGATING:* confidence-seeking zoom loop — in for pixels, out for context (box > 35% of frame), max 4 non-blocking pulses; evidence must come from frames captured **after** the last camera adjustment settled (`DetectionResult.frame_captured_at`); motion-only triggers hold a 2.5s stationary stare.
  - *TRACKING:* **move-settle-measure cadence** — one proportional correction per new detection result, time-boxed to ~0.45s, then stop and re-measure; confidence hysteresis holds the lock at 60% of the confirm threshold; 3s without a qualifying detection = lost → net autonomy zoom is undone → back to SEARCHING. Starting while a drone is visible engages TRACKING+alarm immediately.
- **Detection pipeline** (`backend/app/video_stream.py`): YOLO runs on a dedicated thread (~1 Hz), never blocking capture; results published as `DetectionResult` (detections + frame seq + capture timestamp — the freshness contract). Stored frames are clean; boxes drawn on copies at MJPEG/snapshot time. COCO verification is **skipped during TRACKING only**. Frame-diff motion is **suppressed while the camera itself moves** (+0.75s settle; `OnvifClient` tracks its own motion state).
- **Operator priority** (`backend/app/routers/ptz.py`): any manual PTZ command *stops* an active scan and executes — never a 409. `POST /api/autonomy/alarm/dismiss` silences the alarm without changing mode.
- **Cross-platform alarm** (`backend/app/alarm.py`): `winsound` lazily imported, Windows-only; browser alarm always works.
- **Single-screen UI:** viewport-locked grid, zero page scroll; event log always visible (only internal scroller); PTZ never disabled.

**Shipped yesterday (2026-07-13), specifically for today:**
1. **Session telemetry** (`backend/app/session_log.py`): every run auto-writes `backend/logs/session-<ts>-<pid>.log`, each line `HH:MM:SS`-prefixed (note: `--reload` creates two processes → two files; the worker child is the real one).
2. **Tuning report** (`backend/scripts/summarize_telemetry.py`, stdlib-only): parses a session log → inference latency, waypoint travel/timeouts, investigation outcomes, tracking cadence, deadband ratio, + data-driven tuning hints.
3. **First-run config panel** (gear icon in topbar): sets NVR IP/ports/credentials/channel, live connection test, atomic `.env` write (restart required to apply). Server boots without any `.env` (degrades to "disconnected").

**Testing:** `python backend/tests/test_state_machine.py` — 24 scenarios driving the real autonomy loop with all heavy deps stubbed; needs nothing installed; ~25s. Run it after ANY change to `autonomy.py`. It must stay at 24/24.

## 3. Today's Mission — Hardware Test Protocol

**Setup** (if not already done): Python 3.10+, `python -m venv venv`, `venv\Scripts\activate`, `pip install -r backend/requirements.txt` (use `--index-url https://download.pytorch.org/whl/cpu` for torch — no GPU here). Configure via the gear-icon panel (use "בדוק חיבור" against the real NVR — this also validates the new panel) or `.env`. Start from `backend/`: `uvicorn app.main:app` (skip `--reload` for clean single-process logs).

**Protocol — execute and measure:**
1. Confirm boot: `[SESSION] logging to ...` line, video stream live, detection boxes appear, **server alarm audibly works on Windows** (first hardware validation of `alarm.py` — trigger via a test detection or temporarily lowering the sensitivity slider).
2. Verify the **single-screen layout at this machine's real resolution** — it was never visually verified (no browser in the dev env). Zero page scroll is the requirement.
3. Manual PTZ sanity pass: d-pad, zoom, focus, wiper/IR buttons.
4. Run a scan; verify: waypoint motion + dwell, investigation on a real drone or moving object, track + alarm, dismiss button, manual-override-stops-scan, zoom restore after target loss.
5. After each session: `python backend\scripts\summarize_telemetry.py`. Have the operator note wall-clock times of physical events (log lines are timestamped).

**Watch for → maps to these tunables (all constants at the top of `autonomy.py` / `video_stream.py`):**
- `arrived=False` waypoint timeouts → `PTZ_MOVE_TIMEOUT_SECONDS` (8s) or scan speeds.
- Tracking `gap_since_previous` ≫ 1s → detection cadence starving; strengthens Tier 1 case.
- Deadband ratio > 80% → `TRACK_DEADBAND_RATIO` (0.12) too wide; oscillation → too narrow or `TRACK_CORRECTION_MAX_SECONDS` (0.45) too long.
- False investigations from wind/rain/birds → `INVESTIGATE_MIN_CONFIDENCE` (0.15), `MOTION_AREA_RATIO_THRESHOLD`.
- Motion falsely active right after moves → `MOTION_SETTLE_AFTER_MOVE_SECONDS` (0.75).
- Zoom restore over/undershoot after target loss → the `ZOOM_PULSE_*`/`ZOOM_RESTORE_MAX_SECONDS` bookkeeping (it's approximate by design).
- Check whether `GetStatus` reports a usable `Position.Zoom` value → unlocks zoom-aware gains (Tier 1 #4).
- Inference p95 (report section 2) → decides OpenVINO priority.

Tune constants **one at a time**, re-run, compare reports. Commit tuning changes with the measured numbers in the commit message.

## 4. Wiper Feature — Phase A (data collection only, no code)

1. **Wiper semantics:** with the manual buttons (`Wiper - הפעלה/כיבוי` → ONVIF `SendAuxiliaryCommand` `tt:Wiper|On/Off`), determine: does `On` run one self-terminating cycle, or wipe continuously until `Off`? How long is a cycle? Record in notes.
2. **Contamination ground truth:** spray water on the lens (mist and heavy drops), then run a scan + track cycle. The session log + `backend/snapshots/` capture what contamination looks like through this lens/detector. Also smudge test (fingerprint) if acceptable.
3. **Rain interaction:** note whether water on the lens or falling spray triggers false motion investigations or drone false positives — this affects the *existing* system regardless of the wiper feature.
Phase B (offline detector on this footage — static-across-pan artifact check + sharpness metrics) and Phase C (integration behind persistence/cooldown/never-wipe-while-tracking safeguards) come only after A. The fallback KISS option is a scheduled wipe.

## 5. Pending Roadmap (waiting on today's telemetry)

**Tier 1 (in priority order):** ① OpenVINO export for the Intel CPU (likely 2–4× inference; measure first), ② ROI-cropped inference while tracking, ③ alpha-beta filter for target prediction (not full Kalman yet), ④ zoom-aware control gains (needs today's `Position.Zoom` answer), ⑤ adaptive inference cadence by mode.
**Tier 2:** re-acquisition sweep on target loss, capture watchdog, JSONL event persistence, logging-module migration.
**Deployment:** Inno Setup installer bundling Python embeddable + all wheels + both model weights (fully offline — target networks are isolated); launcher starts uvicorn and opens `msedge --app=http://localhost:8000`. **PyInstaller-freezing and Electron were evaluated and rejected** — don't revisit without new evidence. ⚠️ **Blocker before distributing to third parties:** Ultralytics is AGPL-3.0 and the repo has no LICENSE — a business decision (AGPL compliance or commercial license) belongs to Avi, not to you.

**Environment facts:** the WSL2 dev machine had no deps and no camera — everything hardware-facing was verified only by code review and the stub test suite. You are the first instance that can actually watch this system move. Trust the telemetry over assumptions — including mine.

— Claude, AI Chief Software Architect (2026-07-13)
