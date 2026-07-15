import threading
from pathlib import Path

# Must run before anything else prints: wraps stdout/stderr so the whole
# session (telemetry, uvicorn logs, tracebacks) is captured to a file.
from app.session_log import init_session_log

init_session_log()

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.autonomy import get_autonomy_controller
from app.detection import assert_model_weights_present
from app.onvif_client import get_onvif_client
from app.routers import autonomy, config, detection, events, ptz, stream, system

# Refuse to boot without both YOLO weights files: the deployment network is
# isolated (no auto-download possible), and the alternative failure mode is
# a per-second detection exception silently swallowed into "no detections".
# Runs after init_session_log() so the message also lands in the session log.
assert_model_weights_present()

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title="Avigilon PTZ Dashboard")

app.include_router(autonomy.router)
app.include_router(config.router)
app.include_router(detection.router)
app.include_router(events.router)
app.include_router(ptz.router)
app.include_router(stream.router)
app.include_router(system.router)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.on_event("startup")
def ptz_boot_safety_and_discovery():
    # Two boot-time ONVIF actions on one background thread (never blocks or
    # fails boot -- the server must still come up degraded with no NVR
    # configured or reachable):
    #
    # 1. Best-effort safety Stop: if the previous process died hard (power
    #    cut, task kill) mid-ContinuousMove, the camera is still obeying
    #    that command and nothing else would ever clear it. One Stop at
    #    boot guarantees a fresh server never inherits stale motion.
    # 2. Read-only capability discovery, logged into the session log so
    #    hardware sessions capture what this NVR/camera stack reports.
    def _boot():
        client = get_onvif_client()
        try:
            client.stop()
            print("[PTZ] boot safety stop issued", flush=True)
        except Exception as exc:
            print(f"[PTZ] boot safety stop skipped (NVR unreachable?): {exc}", flush=True)
        try:
            client.get_ptz_capabilities()
        except Exception as exc:
            print(f"[ONVIF] boot capability discovery skipped: {exc}", flush=True)

    threading.Thread(target=_boot, daemon=True, name="ptz-boot").start()


@app.on_event("shutdown")
def stop_autonomy_on_shutdown():
    # Without this, a dev-server restart (--reload) or process exit while
    # the autonomy loop is mid continuous_move() leaves the camera moving
    # with nothing left to send it a Stop.
    get_autonomy_controller().stop()


@app.get("/")
def index_page():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
