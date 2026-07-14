"""Capture clean frames from the live camera as detector training data.

Saves full-resolution JPEG frames (no detection overlays) at a fixed
interval to backend/training_data/session-<timestamp>/, using the same
NVR/RTSP configuration as the server (backend/.env). Runs independently of
the dashboard server -- it opens its own stream connection, so it can run
in a second terminal while the server is up.

Collection tips for retraining best_merged.pt:
  * Fly the drone through the areas where detection currently struggles
    (near the parked cars, close to the camera, far away, against the sky
    and against cluttered backgrounds).
  * Also record some sequences with NO drone in view: frames of the bare
    scene (cars, people, tables) are valuable hard negatives -- label them
    as background images with no boxes.

Run from backend/:
    python scripts/record_training_frames.py --seconds 120 --interval 0.5

Stop early with Ctrl+C -- frames saved so far are kept.
"""
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same TCP-transport forcing as the server's capture (see video_stream.py):
# UDP RTP is prone to drops through Windows Firewall, which shows up as
# corrupted/partial frames -- useless as training data.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402

from app.onvif_client import get_onvif_client  # noqa: E402

JPEG_PARAMS = [cv2.IMWRITE_JPEG_QUALITY, 95]  # high quality for training data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds", type=float, default=60.0,
        help="total recording duration (default: 60)",
    )
    parser.add_argument(
        "--interval", type=float, default=0.5,
        help="seconds between saved frames (default: 0.5 = 2 fps)",
    )
    args = parser.parse_args()

    out_dir = (
        Path(__file__).resolve().parent.parent
        / "training_data"
        / datetime.now().strftime("session-%Y%m%d-%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    stream_url = get_onvif_client().get_stream_uri()
    cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        sys.exit("Could not open the camera stream -- is the NVR reachable?")

    print(f"Recording to {out_dir}")
    print(f"Duration {args.seconds:.0f}s, one frame every {args.interval:.2f}s. Ctrl+C stops early.")

    saved = 0
    started = time.monotonic()
    next_save_at = started
    try:
        while time.monotonic() - started < args.seconds:
            # Read every frame (decoding must keep pace with the stream or
            # frames back up in the TCP buffer and lag behind real time),
            # but only save one per interval.
            ok, frame = cap.read()
            if not ok:
                print("Frame read failed, retrying...", flush=True)
                time.sleep(0.5)
                continue
            now = time.monotonic()
            if now >= next_save_at:
                next_save_at = now + args.interval
                path = out_dir / f"frame-{saved:05d}.jpg"
                cv2.imwrite(str(path), frame, JPEG_PARAMS)
                saved += 1
                if saved % 20 == 0:
                    print(f"  {saved} frames saved...", flush=True)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        cap.release()

    print(f"Done: {saved} frames in {out_dir}")


if __name__ == "__main__":
    main()
