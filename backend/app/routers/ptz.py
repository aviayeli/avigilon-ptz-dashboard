from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.autonomy import Mode, get_autonomy_controller
from app.onvif_client import get_onvif_client
from app.routers.auth import require_auth

router = APIRouter(prefix="/api/ptz", dependencies=[Depends(require_auth)])


def require_manual_control() -> None:
    if get_autonomy_controller().mode != Mode.IDLE:
        raise HTTPException(409, "Autonomous search is active")


class MoveRequest(BaseModel):
    pan: float
    tilt: float
    zoom: float


class FocusRequest(BaseModel):
    mode: Literal["continuous", "auto"]
    speed: float = 0.0


class IrisRequest(BaseModel):
    speed: float


class AuxRequest(BaseModel):
    command: str


@router.post("/move", dependencies=[Depends(require_manual_control)])
def move(payload: MoveRequest):
    try:
        get_onvif_client().continuous_move(payload.pan, payload.tilt, payload.zoom)
    except Exception as exc:
        raise HTTPException(502, f"PTZ move failed: {exc}") from exc
    return {"ok": True}


@router.post("/stop", dependencies=[Depends(require_manual_control)])
def stop():
    try:
        get_onvif_client().stop()
    except Exception as exc:
        raise HTTPException(502, f"PTZ stop failed: {exc}") from exc
    return {"ok": True}


@router.post("/home", dependencies=[Depends(require_manual_control)])
def home():
    try:
        get_onvif_client().goto_home()
    except Exception as exc:
        raise HTTPException(502, f"PTZ home failed: {exc}") from exc
    return {"ok": True}


@router.post("/center", dependencies=[Depends(require_manual_control)])
def set_center():
    # Saves wherever the camera currently is (position it first via manual
    # controls) as the (0, 0) reference for pan/tilt search ranges --
    # GotoHomePosition is a no-op on this hardware, so this is the
    # user-defined substitute.
    try:
        center = get_onvif_client().set_center_here()
    except Exception as exc:
        raise HTTPException(502, f"Set center failed: {exc}") from exc
    return {"ok": True, **center}


@router.post("/focus")
def focus(payload: FocusRequest):
    try:
        client = get_onvif_client()
        if payload.mode == "auto":
            client.set_autofocus()
        elif payload.speed == 0:
            client.stop_focus()
        else:
            client.continuous_focus(payload.speed)
    except Exception as exc:
        raise HTTPException(502, f"Focus control failed: {exc}") from exc
    return {"ok": True}


@router.post("/iris")
def iris(payload: IrisRequest):
    try:
        client = get_onvif_client()
        if payload.speed == 0:
            client.stop_iris()
        else:
            client.continuous_iris(payload.speed)
    except Exception as exc:
        raise HTTPException(502, f"Iris control failed: {exc}") from exc
    return {"ok": True}


@router.post("/aux")
def aux(payload: AuxRequest):
    try:
        get_onvif_client().send_aux_command(payload.command)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Aux command failed: {exc}") from exc
    return {"ok": True}
