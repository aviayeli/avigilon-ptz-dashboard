import sys
import tempfile
from math import pi, sin
from struct import pack

# Server-side alarm playback is Windows-only (winsound is stdlib but Windows-
# specific). On other platforms this module no-ops -- the browser-side alarm
# (played client-side in the dashboard UI) still sounds regardless, so this
# is a degradation, not a loss of the alarm entirely.
_UNAVAILABLE_NOTICE_PRINTED = False

_ALARM_WAV_PATH: str | None = None


def _make_alarm_wav() -> bytes:
    # Short sine-wave alarm tone generated in memory -- no audio asset file.
    sample_rate = 8000
    duration_seconds = 0.5
    frequency = 900
    num_samples = int(sample_rate * duration_seconds)
    samples = bytearray()
    for i in range(num_samples):
        t = i / sample_rate
        value = int(32767 * sin(2 * pi * frequency * t))
        samples += pack("<h", value)

    data = bytes(samples)
    header = pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(data),
    )
    return header + data


def _write_alarm_wav_file() -> str:
    # winsound.PlaySound() disallows SND_MEMORY combined with SND_ASYNC (it
    # can't guarantee the Python bytes buffer stays alive for the duration
    # of async/looped playback) -- writing to a temp file sidesteps that,
    # since the OS then reads directly from disk.
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ptz_alarm_")
    with open(fd, "wb") as f:
        f.write(_make_alarm_wav())
    return path


def _get_alarm_wav_path() -> str:
    # Created lazily on first use (not at import time) and cached, since
    # writing a temp file is unnecessary work for processes that never
    # trigger an alarm.
    global _ALARM_WAV_PATH
    if _ALARM_WAV_PATH is None:
        _ALARM_WAV_PATH = _write_alarm_wav_file()
    return _ALARM_WAV_PATH


def _print_unavailable_notice_once() -> None:
    global _UNAVAILABLE_NOTICE_PRINTED
    if _UNAVAILABLE_NOTICE_PRINTED:
        return
    _UNAVAILABLE_NOTICE_PRINTED = True
    print(
        "[ALARM] server-side audio unavailable on this platform (Windows-only); "
        "browser alarm still sounds",
        flush=True,
    )


def start_alarm() -> None:
    if sys.platform != "win32":
        _print_unavailable_notice_once()
        return
    try:
        import winsound

        winsound.PlaySound(
            _get_alarm_wav_path(), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP
        )
    except Exception as exc:
        print(f"[ALARM] failed to start alarm sound: {exc}", flush=True)


def stop_alarm() -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound

        winsound.PlaySound(None, 0)
    except Exception as exc:
        print(f"[ALARM] failed to stop alarm sound: {exc}", flush=True)
