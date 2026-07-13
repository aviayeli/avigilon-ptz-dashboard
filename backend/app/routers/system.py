from fastapi import APIRouter

from app.onvif_client import get_onvif_client

router = APIRouter(prefix="/api/system")


@router.get("/status")
def status():
    return get_onvif_client().get_status()
