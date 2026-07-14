# handoff.md — Architect Handoff: Avigilon PTZ Drone-Detection Dashboard

**From:** Claude (AI Chief Software Architect), WSL2 development environment, 2026-07-13
**To:** The Claude instance on the Windows deployment machine (hardware-test day)
**Repo:** https://github.com/aviayeli/avigilon-ptz-dashboard — everything below is pushed to `origin/master` (HEAD `529f460`).

---

## 0. Status Update — 2026-07-14 (hardware-test day, written on-site)

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
