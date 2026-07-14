import threading
import time
from functools import lru_cache
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from onvif import ONVIFCamera

from app.config import settings

AUX_COMMANDS = {
    "wiper_on": "tt:Wiper|On",
    "wiper_off": "tt:Wiper|Off",
    "ir_on": "tt:IRLamp|On",
    "ir_off": "tt:IRLamp|Off",
}


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class OnvifClient:
    def __init__(self) -> None:
        self._camera: Optional[ONVIFCamera] = None
        self._media_service = None
        self._ptz_service = None
        self._imaging_service = None
        self._profile = None
        self._video_source_token: Optional[str] = None
        # "Center" is the (0, 0) reference point for operator-facing pan/tilt
        # ranges: wherever set_center_here() last recorded. Defaults to the
        # ONVIF origin (0, 0) until explicitly set.
        self._center_pan = 0.0
        self._center_tilt = 0.0
        # Discovered PTZ capabilities (position spaces, home support); cached
        # after the first successful GetConfigurationOptions round-trip.
        self._ptz_capabilities: Optional[dict] = None
        # Camera-motion bookkeeping for the motion-detection frame-diff (see
        # video_stream.py's _update_motion): reads happen from the video
        # capture thread, writes happen from autonomy/API threads issuing PTZ
        # commands, so this is guarded by its own lock rather than relying on
        # the GIL.
        self._motion_state_lock = threading.Lock()
        self._camera_moving: bool = False
        self._camera_motion_ended_at: float = 0.0

    def _mark_camera_moving(self) -> None:
        with self._motion_state_lock:
            self._camera_moving = True

    def _mark_camera_stopped(self) -> None:
        # Idempotent on purpose: only record the end timestamp on a
        # True->False transition, so repeated stop() calls (or repeated
        # not-moving status polls) don't keep pushing the settle window
        # forward and never let motion detection resume.
        with self._motion_state_lock:
            if self._camera_moving:
                self._camera_moving = False
                self._camera_motion_ended_at = time.monotonic()

    def is_camera_motion_settled(self, settle_seconds: float) -> bool:
        # Cheap, no SOAP call -- safe to poll from the video capture thread
        # on every frame.
        with self._motion_state_lock:
            if self._camera_moving:
                return False
            return (time.monotonic() - self._camera_motion_ended_at) >= settle_seconds

    @property
    def camera(self) -> ONVIFCamera:
        if self._camera is None:
            self._camera = ONVIFCamera(
                settings.NVR_IP,
                settings.ONVIF_PORT,
                settings.NVR_USERNAME,
                settings.NVR_PASSWORD,
            )
        return self._camera

    @property
    def media_service(self):
        if self._media_service is None:
            self._media_service = self.camera.create_media_service()
        return self._media_service

    @property
    def ptz_service(self):
        if self._ptz_service is None:
            self._ptz_service = self.camera.create_ptz_service()
        return self._ptz_service

    @property
    def imaging_service(self):
        if self._imaging_service is None:
            self._imaging_service = self.camera.create_imaging_service()
        return self._imaging_service

    def get_channel_profile(self):
        if self._profile is not None:
            return self._profile

        profiles = self.media_service.GetProfiles()
        channel = settings.CAMERA_CHANNEL_ID
        if channel < 0 or channel >= len(profiles):
            raise ValueError(
                f"CAMERA_CHANNEL_ID={channel} is out of range; "
                f"NVR exposed {len(profiles)} channel profile(s)"
            )

        self._profile = profiles[channel]
        self._video_source_token = self._profile.VideoSourceConfiguration.SourceToken
        return self._profile

    def get_stream_uri(self) -> str:
        # NVR-side RTSP paths are not a documented/stable contract, so we always
        # resolve the per-channel stream URL via ONVIF GetStreamUri instead of
        # hand-building an rtsp:// path from the channel id.
        profile = self.get_channel_profile()
        request = self.media_service.create_type("GetStreamUri")
        request.ProfileToken = profile.token
        request.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        response = self.media_service.GetStreamUri(request)
        return self._with_credentials(response.Uri)

    @staticmethod
    def _with_credentials(uri: str) -> str:
        # ONVIF's GetStreamUri response does not include credentials, but RTSP
        # itself is authenticated separately from the ONVIF/SOAP layer.
        parts = urlsplit(uri)
        if "@" in parts.netloc:
            return uri
        user = quote(settings.NVR_USERNAME, safe="")
        password = quote(settings.NVR_PASSWORD, safe="")
        netloc = f"{user}:{password}@{parts.netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def continuous_move(self, pan: float, tilt: float, zoom: float) -> None:
        profile = self.get_channel_profile()
        request = self.ptz_service.create_type("ContinuousMove")
        request.ProfileToken = profile.token
        request.Velocity = {
            "PanTilt": {"x": _clamp(pan), "y": _clamp(tilt)},
            "Zoom": {"x": _clamp(zoom)},
        }
        self.ptz_service.ContinuousMove(request)
        # Mark state only after the SOAP call returns successfully -- if it
        # raised, the camera never got the command.
        if pan != 0.0 or tilt != 0.0 or zoom != 0.0:
            self._mark_camera_moving()
        else:
            self._mark_camera_stopped()

    def stop(self) -> None:
        profile = self.get_channel_profile()
        request = self.ptz_service.create_type("Stop")
        request.ProfileToken = profile.token
        request.PanTilt = True
        request.Zoom = True
        self.ptz_service.Stop(request)
        self._mark_camera_stopped()

    def absolute_move(self, pan: float, tilt: float, speed: Optional[float] = None) -> None:
        profile = self.get_channel_profile()
        request = self.ptz_service.create_type("AbsoluteMove")
        request.ProfileToken = profile.token
        request.Position = {"PanTilt": {"x": _clamp(pan), "y": _clamp(tilt)}}
        if speed is not None:
            request.Speed = {"PanTilt": {"x": _clamp(speed, 0.0, 1.0), "y": _clamp(speed, 0.0, 1.0)}}
        self.ptz_service.AbsoluteMove(request)
        # absolute_move's arrival is never signaled by a stop() call -- the
        # autonomy loop polls get_ptz_status() every 0.4s while the move is
        # in flight, and that poll is what eventually calls
        # _mark_camera_stopped() below when MoveStatus stops reporting
        # "MOVING". Known limitation: if a caller issues an absolute_move and
        # then never polls status or calls stop(), suppression persists until
        # the next PTZ command. Acceptable because the autonomy loop always
        # polls during absolute moves and every code path that stops
        # scanning calls stop().
        self._mark_camera_moving()

    def get_ptz_status(self) -> dict:
        profile = self.get_channel_profile()
        status = self.ptz_service.GetStatus(profile.token)
        pan_tilt = status.Position.PanTilt
        move_status = status.MoveStatus.PanTilt if status.MoveStatus else None
        moving = move_status == "MOVING"
        # This poll doubles as the arrival oracle for absolute_move (whose
        # completion isn't otherwise signaled) at zero extra SOAP cost.
        if moving:
            self._mark_camera_moving()
        else:
            self._mark_camera_stopped()
        return {
            "pan": pan_tilt.x,
            "tilt": pan_tilt.y,
            "moving": moving,
        }

    def get_pan_tilt_limits(self) -> dict:
        # Falls back to the full normalized range if the camera doesn't
        # report configured mechanical limits for some reason.
        profile = self.get_channel_profile()
        limits = getattr(profile.PTZConfiguration, "PanTiltLimits", None)
        if limits is None or limits.Range is None:
            return {"pan_min": -1.0, "pan_max": 1.0, "tilt_min": -1.0, "tilt_max": 1.0}

        return {
            "pan_min": limits.Range.XRange.Min,
            "pan_max": limits.Range.XRange.Max,
            "tilt_min": limits.Range.YRange.Min,
            "tilt_max": limits.Range.YRange.Max,
        }

    def set_center_here(self) -> dict:
        # Saves the camera's current live position as the (0, 0) reference
        # point for pan/tilt going forward -- point the camera where you
        # want "center" via manual controls, then call this.
        status = self.get_ptz_status()
        self._center_pan = status["pan"]
        self._center_tilt = status["tilt"]
        return {"pan": self._center_pan, "tilt": self._center_tilt}

    def get_center(self) -> dict:
        return {"pan": self._center_pan, "tilt": self._center_tilt}

    def wait_until_stopped(
        self, timeout_seconds: float = 8.0, poll_seconds: float = 0.4
    ) -> bool:
        # Arrival oracle for self-terminating moves issued outside the
        # autonomy loop (GotoHomePosition): polls GetStatus -- which maintains
        # the camera-motion bookkeeping as a side effect -- until MoveStatus
        # stops reporting MOVING. Returns False on timeout. Shares the scan
        # loop's known race: a poll landing before the camera starts
        # reporting motion reads as already stopped.
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            try:
                if not self.get_ptz_status()["moving"]:
                    return True
            except Exception:
                # Status is unreadable, so the "moving" flag can never be
                # cleared by polling; leaving it set would mute motion
                # detection until the next PTZ command, which is worse than
                # a brief false-motion reading.
                self._mark_camera_stopped()
                raise
        return False

    def goto_home(self) -> None:
        # Field-verified 2026-07-14: GotoHomePosition physically moves this
        # camera (an earlier hardware note claiming it was a no-op is wrong).
        profile = self.get_channel_profile()
        request = self.ptz_service.create_type("GotoHomePosition")
        request.ProfileToken = profile.token
        try:
            self.ptz_service.GotoHomePosition(request)
        except Exception as exc:
            raise RuntimeError("Camera/NVR does not support GotoHomePosition") from exc
        # The camera is now sweeping to its home position. Block until it
        # settles so (a) motion suppression covers the sweep and is released
        # afterwards -- nothing else polls status on the manual /home path --
        # and (b) callers can rely on the camera actually being at home when
        # this returns.
        self._mark_camera_moving()
        self.wait_until_stopped()

    def continuous_focus(self, speed: float) -> None:
        self.get_channel_profile()
        request = self.imaging_service.create_type("Move")
        request.VideoSourceToken = self._video_source_token
        request.Focus = {"Continuous": {"Speed": _clamp(speed)}}
        self.imaging_service.Move(request)

    def stop_focus(self) -> None:
        self.get_channel_profile()
        request = self.imaging_service.create_type("Stop")
        request.VideoSourceToken = self._video_source_token
        self.imaging_service.Stop(request)

    def set_autofocus(self) -> None:
        self.get_channel_profile()
        request = self.imaging_service.create_type("SetImagingSettings")
        request.VideoSourceToken = self._video_source_token
        request.ImagingSettings = {"Focus": {"AutoFocusMode": "AUTO"}}
        self.imaging_service.SetImagingSettings(request)

    def continuous_iris(self, speed: float) -> None:
        # Core ONVIF Imaging spec does not define a continuous Iris move (only
        # Focus), but several NVR/camera vendors accept an Iris block on the
        # same Move request. Errors bubble up to the caller as-is.
        self.get_channel_profile()
        request = self.imaging_service.create_type("Move")
        request.VideoSourceToken = self._video_source_token
        request.Iris = {"Continuous": {"Speed": _clamp(speed)}}
        self.imaging_service.Move(request)

    def stop_iris(self) -> None:
        self.get_channel_profile()
        request = self.imaging_service.create_type("Stop")
        request.VideoSourceToken = self._video_source_token
        self.imaging_service.Stop(request)

    def send_aux_command(self, command: str) -> None:
        token = AUX_COMMANDS.get(command)
        if token is None:
            raise ValueError(f"Unknown aux command: {command}")

        profile = self.get_channel_profile()
        request = self.ptz_service.create_type("SendAuxiliaryCommand")
        request.ProfileToken = profile.token
        request.AuxiliaryData = token
        self.ptz_service.SendAuxiliaryCommand(request)

    def get_status(self) -> dict:
        nvr_connected = False
        onvif_connected = False
        try:
            self.camera.devicemgmt.GetSystemDateAndTime()
            nvr_connected = True
        except Exception:
            pass

        try:
            self.get_channel_profile()
            onvif_connected = True
        except Exception:
            pass

        return {
            "nvr_connected": nvr_connected,
            "onvif_connected": onvif_connected,
            "channel": settings.CAMERA_CHANNEL_ID,
        }


@lru_cache
def get_onvif_client() -> OnvifClient:
    return OnvifClient()
