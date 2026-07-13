from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.video_stream import get_video_stream_manager

router = APIRouter(prefix="/api/stream")


@router.get("/mjpeg")
def mjpeg():
    return StreamingResponse(
        get_video_stream_manager().mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
