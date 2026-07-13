import sys
import threading
import time
from pathlib import Path

# Captures everything the server prints (the [AUTONOMY]/[TRACK]/[DETECT]
# telemetry lines, uvicorn's own logging on stderr, tracebacks) into a
# per-run session file, while still echoing to the console. One file per
# process start -- during a hardware-test day every server start becomes one
# self-contained log to feed scripts/summarize_telemetry.py. Deliberately a
# stdout/stderr tee rather than a logging-module migration: it captures the
# existing print()-based telemetry with zero changes to the control loops.

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
KEEP_SESSIONS = 30

_initialized = False


class _Tee:
    # Both stdout and stderr wrap the same file object and share one lock,
    # so interleaved writes from multiple threads stay line-coherent. The
    # file side is line-buffered per stream (each complete line is written
    # with an HH:MM:SS prefix) so telemetry can be correlated with what was
    # physically observed during a hardware test; the console side passes
    # text through untouched.
    def __init__(self, original, logfile, lock):
        self._original = original
        self._logfile = logfile
        self._lock = lock
        self._pending = ""

    def write(self, text):
        with self._lock:
            written = self._original.write(text)
            try:
                self._pending += text
                while "\n" in self._pending:
                    line, self._pending = self._pending.split("\n", 1)
                    self._logfile.write(f"{time.strftime('%H:%M:%S')} {line}\n")
            except Exception:
                # Logging must never break the app (disk full, etc.).
                pass
        return written

    def flush(self):
        with self._lock:
            self._original.flush()
            try:
                self._logfile.flush()
            except Exception:
                pass

    def flush_pending(self):
        # Called at interpreter exit: a crash's final partial line (no
        # trailing newline) would otherwise be lost from the file.
        with self._lock:
            try:
                if self._pending:
                    self._logfile.write(f"{time.strftime('%H:%M:%S')} {self._pending}\n")
                    self._pending = ""
                self._logfile.flush()
            except Exception:
                pass

    # Some libraries (uvicorn's log config, colorizers) probe the stream --
    # delegate anything we don't override to the real console stream.
    def __getattr__(self, name):
        return getattr(self._original, name)


def _prune_old_sessions() -> None:
    sessions = sorted(LOG_DIR.glob("session-*.log"))
    for old in sessions[:-KEEP_SESSIONS]:
        try:
            old.unlink()
        except OSError:
            pass


def init_session_log() -> None:
    # Idempotent: uvicorn --reload imports the app in both the reloader
    # parent and the worker child; each *process* gets its own file (the pid
    # in the name keeps them apart), but re-imports within one process must
    # not double-wrap the streams.
    global _initialized
    if _initialized:
        return
    _initialized = True

    LOG_DIR.mkdir(exist_ok=True)
    _prune_old_sessions()

    import os

    name = f"session-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
    path = LOG_DIR / name
    # Line-buffered so a crash loses at most the current line; errors are
    # replaced rather than raised (frames of Hebrew UI text, console noise).
    logfile = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    lock = threading.Lock()

    sys.stdout = _Tee(sys.stdout, logfile, lock)
    sys.stderr = _Tee(sys.stderr, logfile, lock)

    import atexit

    atexit.register(sys.stdout.flush_pending)
    atexit.register(sys.stderr.flush_pending)
    print(f"[SESSION] logging to {path}", flush=True)
