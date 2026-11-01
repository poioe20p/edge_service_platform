#!/usr/bin/env python3
"""Phase-aware fault injector for targeted experiment runs.

The injector waits for configured phase windows, then applies the configured
action: resolve the most recent normal VIP_DATA backend and hard-stop its
storage container (docker_stop), or restart a LAN controller container
(docker_restart, RQ3 robustness restart cell). Results are recorded to a CSV
artifact consumed by the offline analysis CLIs.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAST_NORMAL_RE = re.compile(
    r"vip_data\((?P<domain>n[12])\): client=(?P<client>\S+) -> vip=(?P<vip>\S+) -> real=(?P<real>\S+) recovery=False"
)
STORAGE_NAME_RE = re.compile(r"^edge_storage_(?:server_n[12]|lan[12]_dyn\d+(?:_cs)?)$")

STOP_REQUESTED = False


@dataclass
class FaultAction:
    name: str
    phase: str
    after_s: float
    domain: str
    controller_lan: str
    selector_mode: str
    action_type: str
    timeout_s: float = 10.0


def _handle_signal(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_phase(phase_file: Path) -> str:
    try:
        return phase_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_plan(plan_path: Path) -> list[FaultAction]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    actions: list[FaultAction] = []
    for entry in data.get("actions", []):
        selector = entry.get("selector", {})
        action = entry.get("action", {})
        actions.append(
            FaultAction(
                name=str(entry["name"]),
                phase=str(entry["phase"]),
                after_s=float(entry.get("after_s", 0.0)),
                domain=str(selector["domain"]),
                controller_lan=str(selector.get("controller_lan", "")),
                selector_mode=str(selector["mode"]),
                action_type=str(action["type"]),
                timeout_s=float(action.get("timeout_s", 10.0)),
            )
        )
    return actions


def resolve_controller_log_path(action: FaultAction, lan1_log: Path, lan2_log: Path) -> Path:
    if action.controller_lan == "lan1":
        return lan1_log
    if action.controller_lan == "lan2":
        return lan2_log
    if action.domain == "n1":
        return lan1_log
    if action.domain == "n2":
        return lan2_log
    raise ValueError(
        f"unsupported selector controller_lan/domain: {action.controller_lan}/{action.domain}"
    )


def resolve_last_normal_backend(log_path: Path, domain: str) -> str:
    last_real_ip: str | None = None
    try:
        with log_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = LAST_NORMAL_RE.search(line)
                if match and match.group("domain") == domain:
                    last_real_ip = match.group("real")
    except OSError as exc:
        raise RuntimeError(f"failed to read controller log {log_path}: {exc}") from exc

    if last_real_ip is None:
        raise RuntimeError(
            f"no normal VIP_DATA backend observed yet in {log_path.name} for {domain}"
        )
    return last_real_ip


def docker_cmd() -> str:
    docker_path = shutil.which("docker")
    if docker_path is None:
        raise RuntimeError("docker command not found")
    return docker_path


def run_command(command: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_ip_from_output(output: str) -> str | None:
    match = re.search(r"inet (?P<ip>\d+\.\d+\.\d+\.\d+)/", output)
    return match.group("ip") if match else None


def resolve_storage_container_for_ip(backend_ip: str) -> str:
    docker = docker_cmd()
    result = run_command([docker, "ps", "--format", "{{.Names}}"])
    for container_name in result.stdout.splitlines():
        if not STORAGE_NAME_RE.match(container_name):
            continue
        inspect = run_command(
            [docker, "exec", container_name, "sh", "-lc", "ip -4 -o addr show eth0 2>/dev/null || true"]
        )
        if parse_ip_from_output(inspect.stdout) == backend_ip:
            return container_name
    raise RuntimeError(f"no running storage container found for backend IP {backend_ip}")


def restart_container_name(action: FaultAction) -> str:
    """Map a docker_restart action to the LAN controller container."""
    if action.controller_lan == "lan2" or action.domain == "n2":
        return "osken_2"
    return "osken"


def execute_action(action: FaultAction, container_name: str) -> str:
    docker = docker_cmd()
    if action.action_type == "docker_restart":
        result = run_command(
            [docker, "restart", container_name],
            timeout=max(action.timeout_s + 5.0, 20.0),
        )
        return result.stdout.strip() or container_name
    if action.action_type != "docker_stop":
        raise RuntimeError(f"unsupported action type: {action.action_type}")
    result = run_command(
        [docker, "stop", "-t", str(int(action.timeout_s)), container_name],
        timeout=max(action.timeout_s + 5.0, 15.0),
    )
    return result.stdout.strip() or container_name


def wait_for_action_window(action: FaultAction, phase_file: Path, poll_interval_s: float) -> None:
    phase_started_at: float | None = None
    while not STOP_REQUESTED:
        current_phase = read_phase(phase_file)
        now = time.monotonic()
        if current_phase == action.phase:
            if phase_started_at is None:
                phase_started_at = now
            if now - phase_started_at >= action.after_s:
                return
        else:
            phase_started_at = None
        time.sleep(poll_interval_s)
    raise KeyboardInterrupt()


def write_event(
    writer: csv.DictWriter,
    *,
    action: FaultAction,
    status: str,
    message: str,
    backend_ip: str = "",
    container_name: str = "",
) -> None:
    writer.writerow(
        {
            "timestamp": now_iso(),
            "action_name": action.name,
            "phase": action.phase,
            "domain": action.domain,
            "selector_mode": action.selector_mode,
            "action_type": action.action_type,
            "backend_ip": backend_ip,
            "container_name": container_name,
            "status": status,
            "message": message,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, metavar="FILE")
    parser.add_argument("--phase-file", required=True, metavar="FILE")
    parser.add_argument("--controller-log-lan1", required=True, metavar="FILE")
    parser.add_argument("--controller-log-lan2", required=True, metavar="FILE")
    parser.add_argument("--output", required=True, metavar="FILE")
    parser.add_argument("--poll-interval-s", type=float, default=1.0, metavar="SECONDS")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    actions = load_plan(Path(args.plan))
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "action_name",
                "phase",
                "domain",
                "selector_mode",
                "action_type",
                "backend_ip",
                "container_name",
                "status",
                "message",
            ],
        )
        writer.writeheader()
        handle.flush()

        for action in actions:
            try:
                wait_for_action_window(action, Path(args.phase_file), args.poll_interval_s)
                if action.action_type == "docker_restart":
                    container_name = restart_container_name(action)
                    result = execute_action(action, container_name)
                    write_event(
                        writer,
                        action=action,
                        status="executed",
                        message=result,
                        container_name=container_name,
                    )
                    handle.flush()
                    continue
                if action.selector_mode != "last_normal_backend_from_controller_log":
                    raise RuntimeError(
                        f"unsupported selector mode: {action.selector_mode}"
                    )
                controller_log = resolve_controller_log_path(
                    action,
                    Path(args.controller_log_lan1),
                    Path(args.controller_log_lan2),
                )
                backend_ip = resolve_last_normal_backend(controller_log, action.domain)
                container_name = resolve_storage_container_for_ip(backend_ip)
                result = execute_action(action, container_name)
                write_event(
                    writer,
                    action=action,
                    status="executed",
                    message=result,
                    backend_ip=backend_ip,
                    container_name=container_name,
                )
                handle.flush()
            except KeyboardInterrupt:
                write_event(
                    writer,
                    action=action,
                    status="cancelled",
                    message="fault injector stopped before action execution",
                )
                handle.flush()
                break
            except Exception as exc:  # noqa: BLE001 - persist the failure in the run artifact
                write_event(
                    writer,
                    action=action,
                    status="error",
                    message=str(exc),
                )
                handle.flush()
                break


if __name__ == "__main__":
    main()