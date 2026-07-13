from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.autonomy import get_autonomy_controller
from app.config import settings
from app.routers import auth, autonomy, detection, events, ptz, stream, system

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title="Avigilon PTZ Dashboard")

app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET_KEY)

app.include_router(auth.router)
app.include_router(autonomy.router)
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


@app.get("/login")
def login_page(request: Request):
    if request.session.get("authenticated") is True:
        return RedirectResponse("/", status_code=302)
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/")
def index_page(request: Request):
    if request.session.get("authenticated") is True:
        return FileResponse(FRONTEND_DIR / "index.html")
    return RedirectResponse("/login")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
