from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.autonomy import Mode, get_autonomy_controller
from app.onvif_client import MANUAL_MOVE_SELF_STOP_SECONDS, get_onvif_client

router = APIRouter(prefix="/api/ptz")


def take_manual_control() -> None:
    # Operator always wins: a manual PTZ command while autonomy is active
    # stops the autonomous loop (and any motion it commanded) before the
    # manual command executes, instead of rejecting the operator with a 409.
    controller = get_autonomy_controller()
    if controller.mode != Mode.IDLE:
        controller.stop()


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


@router.post("/move", dependencies=[Depends(take_manual_control)])
def move(payload: MoveRequest):
    try:
        # Dead-man armed: a browser that dies mid-hold (lost Stop request)
        # must not leave the camera moving forever. The frontend refreshes
        # the move every ~2s while the button is held.
        get_onvif_client().continuous_move(
            payload.pan,
            payload.tilt,
            payload.zoom,
            self_stop_seconds=MANUAL_MOVE_SELF_STOP_SECONDS,
        )
    except Exception as exc:
        raise HTTPException(502, f"PTZ move failed: {exc}") from exc
    return {"ok": True}


@router.post("/stop", dependencies=[Depends(take_manual_control)])
def stop():
    try:
        get_onvif_client().stop()
    except Exception as exc:
        raise HTTPException(502, f"PTZ stop failed: {exc}") from exc
    return {"ok": True}


@router.post("/home", dependencies=[Depends(take_manual_control)])
def home():
    try:
        get_onvif_client().goto_home()
    except Exception as exc:
        raise HTTPException(502, f"PTZ home failed: {exc}") from exc
    return {"ok": True}


@router.post("/center", dependencies=[Depends(take_manual_control)])
def set_center():
    # Saves wherever the camera currently is (position it first via manual
    # controls) as the (0, 0) reference for pan/tilt search ranges.
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
