from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    NVR_IP: str
    NVR_PORT: int = 554
    ONVIF_PORT: int = 80
    NVR_USERNAME: str
    NVR_PASSWORD: str
    CAMERA_CHANNEL_ID: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
