import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.config import settings

router = APIRouter(prefix="/api/config")

# backend/.env, derived from this file's own location rather than the
# process CWD -- uvicorn can be launched from any working directory, but
# this router must always read/write the one .env pydantic-settings loads
# at startup (see app/config.py's `env_file=".env"`, which IS resolved
# relative to CWD, so in practice the server should always be launched from
# backend/ -- this constant just makes this router's own file access CWD
# independent).
ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


def _read_env_file() -> dict:
    """Parse KEY=VALUE lines from backend/.env, ignoring comments/blanks.

    Deliberately stdlib-only and forgiving (no quoting/escaping support) --
    this mirrors the plain format written by _write_env_file and shown in
    .env.example, not a general .env parser.
    """
    values: dict = {}
    if not ENV_PATH.exists():
        return values

    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = _env_unquote(value.strip())
    return values


def _env_quote(value: str) -> str:
    # Values containing characters the plain KEY=VALUE form can't round-trip
    # (python-dotenv treats an unquoted " #" as an inline comment; quotes and
    # backslashes are ambiguous) are written double-quoted with escapes --
    # the same convention python-dotenv itself parses, so pydantic-settings
    # and _read_env_file agree on the result. Plain values stay unquoted so
    # the file still reads like the hand-written .env.example.
    if any(ch in value for ch in (" ", "#", '"', "'", "\\")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _env_unquote(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _current_password(env_values: dict) -> str:
    # Never returned to the client -- only used server-side to fill in "keep
    # existing password" when a request omits nvr_password.
    return env_values.get("NVR_PASSWORD", settings.NVR_PASSWORD)


def _sanitize_error(message: str, password: str) -> str:
    # Belt-and-suspenders: onvif-zeep exceptions shouldn't normally include
    # connection credentials, but SOAP fault text can echo back parts of the
    # request in some server implementations, so scrub the password out of
    # anything we return to the client.
    if password:
        message = message.replace(password, "***")
    return message


class ConfigRequest(BaseModel):
    nvr_ip: str
    nvr_port: int = Field(ge=1, le=65535)
    onvif_port: int = Field(ge=1, le=65535)
    nvr_username: str
    # Empty/omitted means "keep the currently saved password" -- the config
    # panel never round-trips the real password back to the browser (see
    # GET's has_password flag), so there's no other way for a save/test that
    # doesn't change the password to express that.
    nvr_password: str = ""
    camera_channel_id: int = Field(ge=0)

    @field_validator("nvr_ip", "nvr_username", "nvr_password")
    @classmethod
    def _no_line_breaks(cls, value: str) -> str:
        # A newline inside a value would let one field inject arbitrary
        # KEY=VALUE lines into .env (e.g. overwrite NVR_IP via the password
        # field). No legitimate NVR credential contains control characters.
        if any(ch in value for ch in ("\n", "\r", "\x00")):
            raise ValueError("value must not contain line breaks or control characters")
        return value


@router.get("")
def get_config():
    # Re-read the .env file fresh on every call rather than trusting
    # app.config.settings: settings is a singleton constructed once at
    # import time, so after a save() below it would still reflect the old
    # values -- and the panel should re-open showing what was just saved.
    env_values = _read_env_file()
    return {
        "nvr_ip": env_values.get("NVR_IP", settings.NVR_IP),
        "nvr_port": int(env_values.get("NVR_PORT", settings.NVR_PORT)),
        "onvif_port": int(env_values.get("ONVIF_PORT", settings.ONVIF_PORT)),
        "nvr_username": env_values.get("NVR_USERNAME", settings.NVR_USERNAME),
        "camera_channel_id": int(env_values.get("CAMERA_CHANNEL_ID", settings.CAMERA_CHANNEL_ID)),
        "has_password": bool(_current_password(env_values)),
    }


@router.post("/test")
def test_connection(payload: ConfigRequest):
    # Imported here, not at module level: this router (and the config panel
    # it serves) must be reachable even on a fresh checkout before the ONVIF
    # stack is otherwise exercised, and this keeps the live-connection
    # attempt scoped to exactly this handler.
    from onvif import ONVIFCamera

    from app.onvif_client import build_bounded_transport

    password = payload.nvr_password or _current_password(_read_env_file())

    try:
        # A short-lived camera object, deliberately not the app's
        # get_onvif_client() singleton -- this is a point-in-time
        # connectivity check against whatever the user just typed, and must
        # not touch or reset the real client's cached services/profile.
        # Bounded transport: a typo'd IP must time out, not pin a threadpool
        # thread indefinitely.
        camera = ONVIFCamera(
            payload.nvr_ip,
            payload.onvif_port,
            payload.nvr_username,
            password,
            transport=build_bounded_transport(),
        )
        camera.devicemgmt.GetSystemDateAndTime()
        profiles = camera.create_media_service().GetProfiles()
    except Exception as exc:
        # An unreachable/misconfigured NVR is an expected outcome from this
        # endpoint, not a server fault -- report it as 200/ok:false rather
        # than a 502, so the frontend can show it inline without a thrown
        # fetch error.
        return {"ok": False, "error": _sanitize_error(str(exc), password)}

    channel_valid = 0 <= payload.camera_channel_id < len(profiles)
    return {"ok": True, "channels": len(profiles), "channel_valid": channel_valid}


@router.post("")
def save_config(payload: ConfigRequest):
    password = payload.nvr_password or _current_password(_read_env_file())

    # Same key order/comments as .env.example, so a saved file still reads
    # like the hand-written original.
    content = (
        "# Avigilon NVR connection\n"
        f"NVR_IP={_env_quote(payload.nvr_ip)}\n"
        f"NVR_PORT={payload.nvr_port}\n"
        f"ONVIF_PORT={payload.onvif_port}\n"
        f"NVR_USERNAME={_env_quote(payload.nvr_username)}\n"
        f"NVR_PASSWORD={_env_quote(password)}\n"
        "\n"
        "# Camera channel routed through the NVR (0-indexed profile/channel on the NVR)\n"
        f"CAMERA_CHANNEL_ID={payload.camera_channel_id}\n"
    )

    # Atomic write: a crash/power-loss mid-write must never leave a
    # truncated .env behind, since that would silently break the *next*
    # server start. Write to a temp file in the same directory (so
    # os.replace is a same-filesystem rename, not a cross-fs copy) and swap
    # it into place in one step.
    tmp_path = ENV_PATH.parent / ".env.tmp"
    tmp_path.write_text(content)
    os.replace(tmp_path, ENV_PATH)

    # restart_required: pydantic Settings (app.config.settings) and the
    # ONVIF/RTSP client singletons (get_onvif_client(), video capture) are
    # all constructed once at process startup from the old values. Live
    # re-initialization of a possibly-mid-move camera's connection state is
    # deliberately out of scope here -- the operator restarts the server to
    # pick up the new .env cleanly.
    return {"ok": True, "restart_required": True}
