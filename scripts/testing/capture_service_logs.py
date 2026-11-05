#!/usr/bin/env python3
"""Capture edge and storage service logs for the duration of an experiment run.

This script polls ``docker ps -a`` and attaches ``docker logs -f`` followers to
matching containers as they appear. Each container writes to a dedicated log
file under the configured output directory so dynamic containers that come and
go during a run still leave behind useful service logs.
"""

from __future__ import annotations

import argparse
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


_DOCKER_PS_FORMAT = "{{.Names}}\t{{.ID}}"


@dataclass
class _Follower:
    container_id: str
    handle: TextIO
    process: subprocess.Popen[str]


def _docker_ps() -> dict[str, str]:
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
        stderr = (exc.stderr or "").strip()
        sys.stderr.write(f"WARN: 'docker ps' failed: {stderr}\n")
        return {}

    rows: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        rows[parts[0]] = parts[1]
    return rows


def _write_marker(handle: TextIO, message: str) -> None:
    handle.write(
        f"\n=== {datetime.now(timezone.utc).isoformat(timespec='seconds')} {message} ===\n"
    )
    handle.flush()


def _start_follower(container: str, container_id: str, output_dir: Path) -> _Follower:
    output_path = output_dir / f"{container}.log"
    handle = output_path.open("a", encoding="utf-8")
    _write_marker(handle, f"start container_id={container_id}")
    process = subprocess.Popen(
        ["docker", "logs", "--timestamps", "-f", container],
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return _Follower(container_id=container_id, handle=handle, process=process)


def _stop_follower(name: str, follower: _Follower) -> None:
    if follower.process.poll() is None:
        follower.process.terminate()
        try:
            follower.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            follower.process.kill()
            follower.process.wait(timeout=5)

    _write_marker(
        follower.handle,
        f"stop container={name} container_id={follower.container_id} exit_code={follower.process.returncode}",
    )
    follower.handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture edge/storage service logs for a run directory."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--filter-regex",
        default=r"^(edge_server_n[12]|edge_server_lan[12]_dyn\d+|edge_storage_server_n[12]|edge_storage_lan[12]_dyn\d+)$",
        help="Only capture containers whose names match this regex.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where per-container log files are written.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    name_re = re.compile(args.filter_regex)

    sys.stderr.write(
        f"capture_service_logs: interval={args.interval}s filter='{args.filter_regex}' -> {output_dir}\n"
    )

    stop_requested = False

    def _on_signal(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    followers: dict[str, _Follower] = {}

    while not stop_requested:
        current = {
            name: container_id
            for name, container_id in _docker_ps().items()
            if name_re.search(name)
        }

        for name, container_id in sorted(current.items()):
            follower = followers.get(name)
            if follower is None:
                followers[name] = _start_follower(name, container_id, output_dir)
                continue
            if follower.container_id != container_id:
                _stop_follower(name, follower)
                followers[name] = _start_follower(name, container_id, output_dir)

        for name in list(followers):
            follower = followers[name]
            if follower.process.poll() is not None and name not in current:
                _stop_follower(name, follower)
                followers.pop(name, None)

        sleep_for = args.interval
        while sleep_for > 0 and not stop_requested:
            chunk = min(0.2, sleep_for)
            time.sleep(chunk)
            sleep_for -= chunk

    for name, follower in list(followers.items()):
        _stop_follower(name, follower)
        followers.pop(name, None)

    return 0


if __name__ == "__main__":
    sys.exit(main())