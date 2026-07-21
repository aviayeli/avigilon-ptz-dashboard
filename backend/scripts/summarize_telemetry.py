#!/usr/bin/env python3
"""Summarize a backend/logs/session-*.log file into a tuning report.

Parses the telemetry lines emitted by print() calls in autonomy.py,
video_stream.py and detection.py (captured verbatim, with an "HH:MM:SS "
prefix, by app/session_log.py's stdout/stderr tee) and prints a compact,
human-readable report: inference latency, scan/waypoint travel time,
investigation outcomes, tracking cadence/corrections, and false-positive
filtering -- plus a short list of data-driven tuning hints.

Stdlib only. Never crashes on a dirty/partial log -- unrecognized or
malformed lines are simply skipped.

Usage:
    python3 backend/scripts/summarize_telemetry.py [path-to-log]

With no argument, the newest backend/logs/session-*.log is used.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

# Every line in the log file is "HH:MM:SS <raw print output>". Lines without
# that prefix (e.g. a traceback's continuation lines, or corrupted output)
# are not telemetry and are skipped for the purposes of stat extraction, but
# a leading traceback line right after "[AUTONOMY] loop error, stopping
# camera:" is still counted as part of that error.
LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}) (.*)$")

RE_INFERENCE = re.compile(r"^\[DETECT\] inference took ([0-9.]+)s$")
RE_REJECTED = re.compile(r"^\[DETECT\] rejected drone candidate \(confidence=([0-9.]+)\)")

RE_SCAN_WAYPOINT = re.compile(
    r"^\[AUTONOMY\] scan -> waypoint (\d+) pan=(-?[0-9.]+) tilt=(-?[0-9.]+) speed=([0-9.]+)$"
)
RE_WAYPOINT_REACHED = re.compile(
    r"^\[AUTONOMY\] waypoint (\d+) reached after ([0-9.]+)s \(arrived=(True|False)\)$"
)
RE_INVESTIGATING = re.compile(r"^\[AUTONOMY\] investigating \((.+)\)$")
# 2026-07-21: lock-on rewrite replaced the stationary zoom-pulse investigation
# with a follow-and-identify loop -- there is no "investigate zoom in/out"
# line anymore (corrections during a lock-on reuse the same "[TRACK]
# offset_x=..." line as confirmed tracking; see the note on section 5 below).
# "zoom_attempts=" became "corrections=" in the end-of-investigation line.
RE_INVESTIGATION_ENDED = re.compile(
    r"^\[AUTONOMY\] investigation ended \((.+)\) after ([0-9.]+)s, corrections=(\d+)$"
)
RE_LOOP_ERROR = re.compile(r"^\[AUTONOMY\] loop error, stopping camera:$")

RE_TRACK_BOX = re.compile(
    r"^\[TRACK\] box=\(([-0-9]+), ([-0-9]+), ([-0-9]+), ([-0-9]+)\) "
    r"confidence=([0-9.]+) gap_since_previous=([0-9.]+)s$"
)
RE_TRACK_OFFSET = re.compile(
    r"^\[TRACK\] offset_x=(-?[0-9.]+) offset_y=(-?[0-9.]+) box_ratio=([0-9.]+) "
    r"\(target=([0-9.]+)\) -> pan=(-?[0-9.]+) tilt=(-?[0-9.]+) zoom=(-?[0-9.]+)$"
)
RE_TARGET_LOST = re.compile(
    r"^\[TRACK\] target lost -- no qualifying detection for ([0-9.]+)s \(timeout=([0-9.]+)s\)$"
)

RE_VIDEO_RECONNECT_READ = re.compile(r"^\[VIDEO\] cap\.read\(\) failed, reconnecting\.\.\.$")
RE_VIDEO_OPENING = re.compile(r"^\[VIDEO\] opening capture: ")
RE_VIDEO_DETECT_FAILED = re.compile(r"^\[VIDEO\] detection failed: ")

RE_ALARM = re.compile(r"^\[ALARM\] ")


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    # Nearest-rank method (spec permits this).
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    rank = max(1, min(n, int(round(pct / 100.0 * n)) or 1))
    # int(round(...)) can be 0 for very small n/pct; clamp to at least 1.
    if pct / 100.0 * n > 0 and rank < 1:
        rank = 1
    return sorted_values[rank - 1]


def _median(sorted_values: list[float]) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


def stat_line(label: str, values: list[float], unit: str = "") -> str:
    if not values:
        return f"  {label:<22} n=0"
    s = sorted(values)
    n = len(s)
    mn = min(s)
    mx = max(s)
    med = _median(s)
    mean = sum(s) / n
    p95 = _percentile(s, 95)
    u = unit
    return (
        f"  {label:<22} n={n:<5} "
        f"min={mn:.2f}{u} median={med:.2f}{u} mean={mean:.2f}{u} "
        f"p95={p95:.2f}{u} max={mx:.2f}{u}"
    )


def histogram(counter: dict, total: int | None = None) -> list[str]:
    lines = []
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    for key, count in items:
        if total:
            pct = 100.0 * count / total
            lines.append(f"    {key:<40} {count:>5}  ({pct:5.1f}%)")
        else:
            lines.append(f"    {key:<40} {count:>5}")
    return lines


def hms_to_seconds(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


class SessionStats:
    def __init__(self) -> None:
        self.first_ts: str | None = None
        self.last_ts: str | None = None
        self.line_count = 0
        self.parsed_count = 0

        self.loop_errors = 0
        self.video_opens = 0
        self.video_read_failures = 0

        self.inference_seconds: list[float] = []

        self.waypoint_travel: list[float] = []
        self.waypoint_travel_by_index: dict[int, list[float]] = {}
        self.waypoint_arrived_true = 0
        self.waypoint_arrived_false = 0

        self.investigations_started = 0
        self.investigation_trigger_hist: dict[str, int] = {}
        self.investigation_end_hist: dict[str, int] = {}
        self.investigation_durations: list[float] = []
        self.investigation_corrections_final: list[int] = []

        self.track_gaps: list[float] = []
        self.track_corrections = 0
        self.track_deadband_hits = 0
        self.track_offset_magnitudes: list[float] = []
        self.track_box_ratios: list[float] = []
        self.target_lost_count = 0

        self.rejected_candidates = 0

        self._first_video_opening_seen = False

    def note_timestamp(self, ts: str) -> None:
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts


def parse_log(path: Path) -> SessionStats:
    stats = SessionStats()
    pending_loop_error = False

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            stats.line_count += 1
            line = raw_line.rstrip("\n")
            m = LINE_RE.match(line)
            if not m:
                # Not a telemetry-prefixed line (e.g. a traceback continuation
                # line, or garbage). Skip -- never crash on a dirty log.
                pending_loop_error = False
                continue
            ts, body = m.group(1), m.group(2)
            stats.note_timestamp(ts)

            try:
                if pending_loop_error:
                    # This is the first line of a traceback following a
                    # "[AUTONOMY] loop error" line; consume it but it doesn't
                    # need further parsing. Only swallow one line's worth of
                    # "not telemetry" state -- subsequent traceback lines will
                    # just fall through as unrecognized (harmless).
                    pending_loop_error = False

                if RE_INFERENCE.match(body):
                    mm = RE_INFERENCE.match(body)
                    stats.inference_seconds.append(float(mm.group(1)))
                    stats.parsed_count += 1
                    continue

                if RE_REJECTED.match(body):
                    stats.rejected_candidates += 1
                    stats.parsed_count += 1
                    continue

                if RE_SCAN_WAYPOINT.match(body):
                    stats.parsed_count += 1
                    continue

                mm = RE_WAYPOINT_REACHED.match(body)
                if mm:
                    idx = int(mm.group(1))
                    travel = float(mm.group(2))
                    arrived = mm.group(3) == "True"
                    stats.waypoint_travel.append(travel)
                    stats.waypoint_travel_by_index.setdefault(idx, []).append(travel)
                    if arrived:
                        stats.waypoint_arrived_true += 1
                    else:
                        stats.waypoint_arrived_false += 1
                    stats.parsed_count += 1
                    continue

                mm = RE_INVESTIGATING.match(body)
                if mm:
                    trigger_raw = mm.group(1)
                    trigger = "motion" if trigger_raw == "motion" else "candidate"
                    stats.investigations_started += 1
                    stats.investigation_trigger_hist[trigger] = (
                        stats.investigation_trigger_hist.get(trigger, 0) + 1
                    )
                    stats.parsed_count += 1
                    continue

                mm = RE_INVESTIGATION_ENDED.match(body)
                if mm:
                    reason = mm.group(1)
                    duration = float(mm.group(2))
                    corrections = int(mm.group(3))
                    stats.investigation_end_hist[reason] = (
                        stats.investigation_end_hist.get(reason, 0) + 1
                    )
                    stats.investigation_durations.append(duration)
                    stats.investigation_corrections_final.append(corrections)
                    stats.parsed_count += 1
                    continue

                if RE_LOOP_ERROR.match(body):
                    stats.loop_errors += 1
                    pending_loop_error = True
                    stats.parsed_count += 1
                    continue

                mm = RE_TRACK_BOX.match(body)
                if mm:
                    gap = float(mm.group(6))
                    stats.track_gaps.append(gap)
                    stats.parsed_count += 1
                    continue

                mm = RE_TRACK_OFFSET.match(body)
                if mm:
                    offset_x = float(mm.group(1))
                    offset_y = float(mm.group(2))
                    box_ratio = float(mm.group(3))
                    pan = float(mm.group(5))
                    tilt = float(mm.group(6))
                    zoom = float(mm.group(7))
                    stats.track_corrections += 1
                    stats.track_box_ratios.append(box_ratio)
                    mag = (offset_x ** 2 + offset_y ** 2) ** 0.5
                    stats.track_offset_magnitudes.append(mag)
                    if pan == 0.0 and tilt == 0.0 and zoom == 0.0:
                        stats.track_deadband_hits += 1
                    stats.parsed_count += 1
                    continue

                if RE_TARGET_LOST.match(body):
                    stats.target_lost_count += 1
                    stats.parsed_count += 1
                    continue

                if RE_VIDEO_OPENING.match(body):
                    if stats._first_video_opening_seen:
                        stats.video_opens += 1
                    else:
                        stats._first_video_opening_seen = True
                    stats.parsed_count += 1
                    continue

                if RE_VIDEO_RECONNECT_READ.match(body):
                    stats.video_read_failures += 1
                    stats.parsed_count += 1
                    continue

                if RE_VIDEO_DETECT_FAILED.match(body):
                    stats.parsed_count += 1
                    continue

                if RE_ALARM.match(body):
                    stats.parsed_count += 1
                    continue

                # Unrecognized but well-formed (has a timestamp prefix) line
                # -- e.g. uvicorn logging, [SESSION] banner, [VIDEO] other
                # messages, traceback text. Not an error; just not counted.
            except (ValueError, IndexError):
                # Malformed numeric field inside an otherwise-matching line
                # (shouldn't happen given the regexes, but never crash).
                continue

    return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(path: Path, stats: SessionStats) -> str:
    lines: list[str] = []
    w = lines.append

    w("=" * 78)
    w(f"TELEMETRY SUMMARY  --  {path.name}")
    w("=" * 78)

    # 1. Session overview
    w("")
    w("1. SESSION OVERVIEW")
    w("-" * 78)
    if stats.first_ts and stats.last_ts:
        dur = hms_to_seconds(stats.last_ts) - hms_to_seconds(stats.first_ts)
        if dur < 0:
            dur += 24 * 3600  # crossed midnight
        w(f"  first timestamp        {stats.first_ts}")
        w(f"  last timestamp         {stats.last_ts}")
        w(f"  wall duration          {dur}s ({dur / 60.0:.1f} min)")
    else:
        w("  no timestamped lines found")
    w(f"  lines read / parsed    {stats.line_count} / {stats.parsed_count}")
    w(f"  loop errors            {stats.loop_errors}")
    reconnects = stats.video_read_failures + stats.video_opens
    w(f"  video reconnects       {reconnects}  "
      f"(cap.read() failures={stats.video_read_failures}, "
      f"re-opens after first={stats.video_opens})")

    # 2. Inference
    w("")
    w("2. INFERENCE (detector latency)")
    w("-" * 78)
    w(stat_line("inference time", stats.inference_seconds, "s"))

    # 3. Scanning
    w("")
    w("3. SCANNING (waypoint travel)")
    w("-" * 78)
    w(stat_line("travel time", stats.waypoint_travel, "s"))
    total_reached = stats.waypoint_arrived_true + stats.waypoint_arrived_false
    if total_reached:
        pct_timeout = 100.0 * stats.waypoint_arrived_false / total_reached
        w(f"  arrived=False (timeout) {stats.waypoint_arrived_false}/{total_reached}  ({pct_timeout:.1f}%)")
    else:
        w("  arrived=False (timeout) 0/0")
    if len(stats.waypoint_travel_by_index) > 1:
        w("  per-waypoint mean travel time:")
        for idx in sorted(stats.waypoint_travel_by_index):
            vals = stats.waypoint_travel_by_index[idx]
            mean = sum(vals) / len(vals)
            w(f"    waypoint {idx:<3} n={len(vals):<4} mean={mean:.2f}s")

    # 4. Investigations
    w("")
    w("4. INVESTIGATIONS")
    w("-" * 78)
    ended = sum(stats.investigation_end_hist.values())
    # Footnote-documented proxy: an investigation that started but has no
    # matching "investigation ended" line was, by elimination, confirmed
    # (the loop transitioned it into TRACKING instead of ending it).
    confirmed = max(0, stats.investigations_started - ended)
    w(f"  total started           {stats.investigations_started}")
    w(f"  confirmed (proxy)       {confirmed}   (see footnote)")
    w(f"  ended (explicit reason) {ended}")
    w("  trigger breakdown:")
    for l in histogram(stats.investigation_trigger_hist, stats.investigations_started):
        w(l)
    w("  end-reason breakdown:")
    end_hist_with_confirmed = dict(stats.investigation_end_hist)
    if confirmed:
        end_hist_with_confirmed["confirmed (proxy)"] = confirmed
    for l in histogram(end_hist_with_confirmed, stats.investigations_started):
        w(l)
    w(stat_line("duration", stats.investigation_durations, "s"))
    corrections_hist: dict[int, int] = {}
    for c in stats.investigation_corrections_final:
        corrections_hist[c] = corrections_hist.get(c, 0) + 1
    w("  corrections-at-end histogram (follow-and-identify lock-on):")
    for l in histogram(corrections_hist):
        w(l)
    w("  footnote: 'confirmed' is a proxy = investigations_started - count of")
    w("  explicit 'investigation ended (...)' lines. An investigation with no")
    w("  ended line transitioned to TRACKING instead (confirmed), rather than")
    w("  being scanned for a distinct 'confirmed' log line (none exists).")

    # 5. Tracking
    w("")
    w("5. TRACKING")
    w("-" * 78)
    w("  NOTE: since the 2026-07-21 lock-on rewrite, corrections issued while")
    w("  following an unconfirmed target (INVESTIGATING) use the same")
    w("  '[TRACK] offset_x=...' log line as confirmed tracking, so the")
    w("  correction/deadband/box_ratio stats below are a mix of both -- only")
    w("  gap_since_previous and target-lost-count are TRACKING-exclusive.")
    w(stat_line("gap_since_previous", stats.track_gaps, "s"))
    w("  (design target ~1s measurement cadence)")
    w(f"  corrections issued      {stats.track_corrections}")
    if stats.track_corrections:
        pct_deadband = 100.0 * stats.track_deadband_hits / stats.track_corrections
        w(f"  deadband hits           {stats.track_deadband_hits}/{stats.track_corrections}  ({pct_deadband:.1f}%)")
    else:
        w("  deadband hits           0/0")
    w(stat_line("offset magnitude", stats.track_offset_magnitudes))
    w(stat_line("box_ratio", stats.track_box_ratios) + "  (target=0.08)")
    w(f"  target-lost events      {stats.target_lost_count}")

    # 6. False-positive filter
    w("")
    w("6. FALSE-POSITIVE FILTER")
    w("-" * 78)
    w(f"  rejected drone candidates (overlap w/ stationary object)  {stats.rejected_candidates}")

    # 7. Tuning hints
    w("")
    w("7. TUNING HINTS")
    w("-" * 78)
    hints: list[str] = []

    if stats.inference_seconds:
        p95_inf = _percentile(sorted(stats.inference_seconds), 95)
        if p95_inf > 0.5:
            hints.append(
                f"- inference p95={p95_inf:.2f}s > 0.5s -> consider prioritizing OpenVINO "
                f"(or a lighter model) for the detector."
            )
    else:
        p95_inf = None

    if stats.waypoint_arrived_false > 0:
        hints.append(
            f"- {stats.waypoint_arrived_false} waypoint move(s) hit arrived=False (timeout) "
            f"-> consider raising PTZ_MOVE_TIMEOUT_SECONDS (currently 8s) or lowering scan speed."
        )

    if stats.track_gaps and p95_inf is not None:
        p95_gap = _percentile(sorted(stats.track_gaps), 95)
        threshold = p95_inf + 1.0
        if p95_gap > threshold:
            hints.append(
                f"- tracking gap p95={p95_gap:.2f}s >> inference p95={p95_inf:.2f}s + 1.0s "
                f"({threshold:.2f}s) -> detection cadence is starving the tracking loop."
            )

    if stats.track_corrections:
        pct_deadband = 100.0 * stats.track_deadband_hits / stats.track_corrections
        if pct_deadband > 80.0:
            hints.append(
                f"- deadband hit ratio={pct_deadband:.1f}% (>80%) -> TRACK_DEADBAND_RATIO may be too wide, "
                f"camera is rarely correcting."
            )

    if stats.loop_errors > 0:
        hints.append(
            f"- {stats.loop_errors} loop error(s) logged -- inspect the traceback in the log; "
            f"the autonomy loop stopped the camera."
        )

    reconnects = stats.video_read_failures + stats.video_opens
    if reconnects > 0:
        hints.append(
            f"- {reconnects} video reconnect event(s) -- check stream stability / cabling for tomorrow's test."
        )

    if not hints:
        w("  (none -- no measured values crossed a tuning threshold)")
    else:
        for h in hints:
            w(h)

    w("")
    w("=" * 78)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def find_newest_log(logs_dir: Path) -> Path | None:
    if not logs_dir.is_dir():
        return None
    candidates = sorted(logs_dir.glob("session-*.log"))
    if not candidates:
        return None
    return candidates[-1]


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path = Path(argv[1])
        if not path.is_file():
            print(f"error: log file not found: {path}", file=sys.stderr)
            return 1
    else:
        repo_backend = Path(__file__).resolve().parent.parent
        logs_dir = repo_backend / "logs"
        newest = find_newest_log(logs_dir)
        if newest is None:
            print(
                f"No session logs found in {logs_dir} "
                "(run the server at least once, or pass a log path explicitly).",
                file=sys.stderr,
            )
            return 1
        path = newest

    stats = parse_log(path)
    report = build_report(path, stats)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
