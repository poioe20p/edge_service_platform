"""Sample controller container CPU/RAM during experiments.

Runs as a background process alongside collect_resource_stats.py.
Periodically runs ``docker stats`` on the controller containers and writes
the results to ``controller_stats.csv`` for post-run overhead analysis.

Usage:
    python sample_controller_stats.py \
        --output metrics/controller_stats.csv \
        --phase-file /path/to/current_phase.txt \
        [--interval 5] \
        [--containers osken,osken_2]

Signal handling:
    SIGTERM/SIGINT cause a graceful flush-and-exit after the current
    sample completes.
"""
from __future__ import annotations

import argparse
import csv
import signal
import subprocess
import time
from pathlib import Path


_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


_FIELDNAMES = ["timestamp_iso", "timestamp", "phase",
               "container", "cpu_percent", "mem_usage_mb"]


def _read_phase(phase_file_path: str) -> str:
    """Read the current phase name from the phase file."""
    try:
        with open(phase_file_path, "r") as f:
            return f.readline().strip()
    except (OSError, IOError):
        return ""


def _sample_one(container: str, phase: str) -> dict | None:
    """Run ``docker stats --no-stream`` for one container.

    Returns a row dict or None on failure.
    """
    try:
        result = subprocess.run(
            [
                "docker", "stats", "--no-stream",
                "--format", "{{.Name}},{{.CPUPerc}},{{.MemUsage}}",
                container,
            ],
            capture_output=True, text=True, timeout=10, check=True,
        )
        line = result.stdout.strip()
        if not line:
            return None
        parts = line.split(",", 2)
        if len(parts) != 3:
            return None
        name, cpu_raw, mem_raw = parts
        cpu_percent = float(cpu_raw.replace("%", ""))
        mem_str = mem_raw.split("/")[0].strip().replace("MiB", "").replace("GiB", "")
        if "GiB" in mem_raw.split("/")[0]:
            mem_usage_mb = float(mem_str) * 1024
        else:
            mem_usage_mb = float(mem_str)
        return {
            "timestamp_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
            "timestamp":   time.time(),
            "phase":       phase,
            "container":   name,
            "cpu_percent": cpu_percent,
            "mem_usage_mb": round(mem_usage_mb, 1),
        }
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            ValueError, IndexError, OSError) as exc:
        # Suppress stderr noise during normal operation — docker stats may
        # race with container shutdown at experiment end.
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Sample controller container CPU/RAM during experiments",
    )
    parser.add_argument(
        "--output", default="metrics/controller_stats.csv", metavar="FILE",
    )
    parser.add_argument(
        "--phase-file", default="", metavar="FILE",
        help="Path to current_phase.txt (optional — phase column left empty if omitted)",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0, metavar="S",
        help="Seconds between samples (default: 5)",
    )
    parser.add_argument(
        "--containers", default="osken,osken_2",
        help="Comma-separated list of controller container names",
    )
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    containers = [c.strip() for c in args.containers.split(",") if c.strip()]
    interval_s = args.interval
    phase_file = args.phase_file

    print(f"[sample_controller_stats] writing to {out_path}")
    print(f"  containers: {containers}")
    print(f"  interval: {interval_s}s")
    if phase_file:
        print(f"  phase-file: {phase_file}")

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        f.flush()

        while _running:
            phase = _read_phase(phase_file) if phase_file else ""
            for container in containers:
                row = _sample_one(container, phase)
                if row:
                    writer.writerow(row)
                    f.flush()
            for _ in range(max(1, int(interval_s / 0.25))):
                if not _running:
                    break
                time.sleep(0.25)


if __name__ == "__main__":
    main()
