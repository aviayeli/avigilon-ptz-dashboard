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
from app.routers import autonomy, config, detection, events, ptz, stream, system

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
