from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path (backend/.env) rather than the CWD-relative ".env": the
# server must find the same file the config panel writes (see
# routers/config.py's ENV_PATH) no matter which directory it was launched
# from -- e.g. a desktop launcher won't necessarily cd into backend/ first.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE)

    # Empty-string defaults (rather than required fields) let the server boot
    # with no .env at all -- e.g. on first run before the config panel has
    # been used. Downstream code (OnvifClient, /api/system/status) already
    # try/excepts ONVIF/RTSP calls and degrades to "disconnected" instead of
    # crashing.
    NVR_IP: str = ""
    NVR_PORT: int = 554
    ONVIF_PORT: int = 80
    NVR_USERNAME: str = ""
    NVR_PASSWORD: str = ""
    CAMERA_CHANNEL_ID: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
