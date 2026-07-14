"""Record the live camera stream to a video file for detector training.

Stream-copies the camera's native H.264 straight into an .mp4 -- no
re-encoding, so it costs almost no CPU (safe to run alongside the server on
this 2-core machine) and preserves the original full quality, which is
exactly what you want for extracting training frames on another computer.

Uses the same NVR/RTSP configuration as the server (backend/.env). Output
goes to backend/training_data/video-<timestamp>.mp4.

Run from backend/:
    python scripts/record_training_video.py --seconds 120

Stop early with Ctrl+C -- the file is finalized cleanly and kept.

Extracting frames on the training computer (ffmpeg, 2 frames/second):
    ffmpeg -i video-....mp4 -vf fps=2 frames/frame-%05d.jpg
"""
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.onvif_client import get_onvif_client  # noqa: E402


def _find_ffmpeg() -> str:
    # Prefer the static binary bundled by the imageio-ffmpeg package (this
    # machine has no system ffmpeg); fall back to PATH if one appears later.
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    sys.exit(
        "No ffmpeg available. Install it into the venv with:\n"
        "    python -m pip install imageio-ffmpeg"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds", type=float, default=120.0,
        help="recording duration (default: 120)",
    )
    args = parser.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "training_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / datetime.now().strftime("video-%Y%m%d-%H%M%S.mp4")

    stream_url = get_onvif_client().get_stream_uri()
    ffmpeg = _find_ffmpeg()

    cmd = [
        ffmpeg,
        "-hide_banner", "-loglevel", "warning",
        # TCP transport for the same reason as the server's capture: UDP RTP
        # drops packets through Windows Firewall, which would corrupt the
        # recording.
        "-rtsp_transport", "tcp",
        "-i", stream_url,
        "-c", "copy",  # stream copy: no re-encoding, no quality loss, ~0 CPU
        # The NVR stream carries a pcm_mulaw audio track that the MP4
        # container refuses in copy mode -- and training has no use for
        # audio anyway.
        "-an",
        "-t", str(args.seconds),
        # Fragmented MP4: the file stays playable even if recording is
        # interrupted uncleanly (power loss, kill) -- a normal MP4 written
        # halfway is unreadable.
        "-movflags", "frag_keyframe+empty_moov",
        str(out_path),
    ]

    print(f"Recording {args.seconds:.0f}s to {out_path}")
    print("Ctrl+C stops early and finalizes the file.")
    # Note: cmd contains the stream URL with credentials -- deliberately not
    # printed.
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        proc.wait()
    except KeyboardInterrupt:
        # 'q' on stdin asks ffmpeg to finish writing and exit cleanly.
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
        print("\nStopped by user.")

    if out_path.exists() and out_path.stat().st_size > 0:
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"Done: {out_path} ({size_mb:.1f} MB)")
    else:
        sys.exit("Recording failed -- no output file was written.")


if __name__ == "__main__":
    main()
