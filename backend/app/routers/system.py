from fastapi import APIRouter, Request

from app.onvif_client import get_onvif_client

router = APIRouter(prefix="/api/system")


@router.get("/status")
def status(request: Request):
    return {
        **get_onvif_client().get_status(),
        "authenticated": request.session.get("authenticated") is True,
    }
