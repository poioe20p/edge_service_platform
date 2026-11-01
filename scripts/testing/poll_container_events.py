#!/usr/bin/env python3
"""
poll_container_events.py — Diff-based ``docker ps`` poller.

Authoritative ground truth for *what is actually running* during an experiment,
independent of telemetry / heartbeats / controller logs.  Each tick we run
``docker ps -a`` and diff the result against the previous tick:

* container appeared          -> ``added``       (its first observation; lifetime begins)
* container disappeared       -> ``removed``     (lifetime ends)
* container state transition  -> ``state_change`` (e.g. running -> exited)

To make ``added`` / ``removed`` self-describing without replaying the whole
file, an ``initial`` snapshot row is emitted for every container present on
the first tick, and a ``final`` snapshot is emitted on shutdown.

Output CSV columns
------------------
timestamp_iso, monotonic_s, phase, event, container, image, state, status,
prev_state, container_id

The ``phase`` column is read from the shared ``current_phase.txt`` file
written by the traffic generator (same mechanism as collect_resource_stats.py).

Usage
-----
    python3 poll_container_events.py \
        --interval 1.0 \
        --filter-regex '^(edge_|sel_sync_|nat-router|osken|local_state_)' \
        --phase-file metrics/<run>/current_phase.txt \
        --output     metrics/<run>/container_events.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

_DOCKER_PS_FORMAT = "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t{{.ID}}"

CSV_FIELDS = [
    "timestamp_iso",
    "monotonic_s",
    "phase",
    "event",
    "container",
    "image",
    "state",
    "status",
    "prev_state",
    "container_id",
]


def _read_phase(phase_file: str) -> str:
    """Read current phase from the shared phase file.

    Mirrors the helper in ``collect_resource_stats.py`` so both files agree.
    """
    try:
        with open(phase_file, "r") as f:
            phase = f.read().strip()
        return phase if phase else "transition"
    except (FileNotFoundError, OSError):
        return "transition"


def _docker_ps() -> dict[str, dict[str, str]]:
    """Return ``{container_name: {image, state, status, id}}`` for all containers.

    Uses ``docker ps -a`` so exited / created containers are visible too — this
    is what allows us to detect a crash transition rather than a silent vanish.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format", _DOCKER_PS_FORMAT],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        sys.stderr.write("ERROR: 'docker' CLI not found in PATH\n")
        return {}
    except subprocess.TimeoutExpired:
        sys.stderr.write("WARN: 'docker ps' timed out, skipping tick\n")
        return {}
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"WARN: 'docker ps' failed: {exc.stderr.strip()}\n")
        return {}

    out: dict[str, dict[str, str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, image, state, status, cid = parts[0], parts[1], parts[2], parts[3], parts[4]
        out[name] = {
            "image": image,
            "state": state,
            "status": status,
            "id": cid,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff-based docker ps poller — emits one CSV row per add/remove/state_change."
    )
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="Polling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--filter-regex",
        # Workload + supporting nodes so run state reconstructs from one CSV: edge_*
        # (compute/storage), sel_sync_* (Tier 1), nat-router/osken* (net/control).
        default=r"^(edge_|sel_sync_|nat-router|osken|local_state_)",
        help="Only track containers whose name matches this regex.",
    )
    parser.add_argument(
        "--phase-file", default=None,
        help="Path to current_phase.txt written by traffic_generator. "
             "Default: <output dir>/current_phase.txt",
    )
    parser.add_argument(
        "--output", required=True,
        help="Output CSV path. Created (with header) if missing; appended to otherwise.",
    )
    args = parser.parse_args()

    output_dir = os.path.dirname(os.path.abspath(args.output))
    if args.phase_file is None:
        args.phase_file = os.path.join(output_dir, "current_phase.txt")

    name_re = re.compile(args.filter_regex)

    # Keep file handle open for the lifetime of the process so flushes are cheap.
    new_file = not os.path.exists(args.output)
    fout = open(args.output, "a", newline="")
    writer = csv.DictWriter(fout, fieldnames=CSV_FIELDS)
    if new_file:
        writer.writeheader()
        fout.flush()

    sys.stderr.write(
        f"poll_container_events: interval={args.interval}s "
        f"filter='{args.filter_regex}' phase_file={args.phase_file} -> {args.output}\n"
    )

    stop_requested = False

    def _on_signal(signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    previous: dict[str, dict[str, str]] = {}
    first_tick = True
    t0 = time.monotonic()

    def _now_row(event: str, name: str, info: dict[str, str], prev_state: str = "") -> dict:
        return {
            "timestamp_iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "monotonic_s": f"{time.monotonic() - t0:.3f}",
            "phase": _read_phase(args.phase_file),
            "event": event,
            "container": name,
            "image": info.get("image", ""),
            "state": info.get("state", ""),
            "status": info.get("status", ""),
            "prev_state": prev_state,
            "container_id": info.get("id", ""),
        }

    while not stop_requested:
        tick_started = time.monotonic()
        current = {n: i for n, i in _docker_ps().items() if name_re.search(n)}

        rows: list[dict] = []
        if first_tick:
            for name, info in sorted(current.items()):
                rows.append(_now_row("initial", name, info))
            first_tick = False
        else:
            for name in sorted(current.keys() - previous.keys()):
                rows.append(_now_row("added", name, current[name]))
            for name in sorted(previous.keys() - current.keys()):
                # Carry image/id from last seen so the row still identifies it.
                rows.append(_now_row(
                    "removed", name, previous[name],
                    prev_state=previous[name].get("state", ""),
                ))
            for name in sorted(current.keys() & previous.keys()):
                if current[name].get("state") != previous[name].get("state"):
                    rows.append(_now_row(
                        "state_change", name, current[name],
                        prev_state=previous[name].get("state", ""),
                    ))

        if rows:
            writer.writerows(rows)
            fout.flush()

        previous = current

        elapsed = time.monotonic() - tick_started
        sleep_for = max(0.0, args.interval - elapsed)
        chunk = 0.2
        while sleep_for > 0 and not stop_requested:
            time.sleep(min(chunk, sleep_for))
            sleep_for -= chunk

    sys.stderr.write("poll_container_events: shutdown — emitting final snapshot\n")
    final = {n: i for n, i in _docker_ps().items() if name_re.search(n)}
    final_rows = [_now_row("final", name, info) for name, info in sorted(final.items())]
    if final_rows:
        writer.writerows(final_rows)
        fout.flush()

    fout.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
