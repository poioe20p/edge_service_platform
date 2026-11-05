#!/usr/bin/env python3
"""
parse_elasticity_logs.py — Extract elasticity scaling events from SDN controller logs.

Reads controller log files (captured via ``docker logs``) and outputs a CSV
with one row per scaling event, suitable for correlation with resource_stats.csv.

Usage:
    python parse_elasticity_logs.py controller_lan1.log controller_lan2.log -o elasticity_events.csv
    python parse_elasticity_logs.py controller_lan1.log --controller-names lan1 -o events.csv
    python parse_elasticity_logs.py controller_lan*.log -o elasticity_events.csv --timings-output node_lifecycle_timings.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Timestamp regex — matches the logging.conf format: "2026-04-11 19:48:13,456"

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"

# Event patterns — each tuple: (compiled regex, event builder function)
#
# Builder signature: (match, controller) -> dict with keys:
#   timestamp, controller, event_type, node_type, container, mac, ip, detail

PATTERNS: list[tuple[re.Pattern, callable]] = []


def _maybe_float(value: str):
    value = value.strip().lower()
    if value in {"n/a", "none", "null"}:
        return None
    return float(value)


def _pat(pattern: str):
    """Decorator that registers a regex pattern and its builder."""
    compiled = re.compile(pattern)

    def decorator(fn):
        PATTERNS.append((compiled, fn))
        return fn

    return decorator


def _row(ts, controller, event_type, node_type="", container="", mac="", ip="", detail=None):
    return {
        "timestamp": ts,
        "controller": controller,
        "event_type": event_type,
        "node_type": node_type,
        "container": container,
        "mac": mac,
        "ip": ip,
        "detail": json.dumps(detail) if detail else "",
    }



@_pat(_TS + r" (?:INFO|DEBUG) .+\[scale-up\] storage triggered: (\d+)/(\d+) windows ≥ ([\d.]+) "
      r"\(eff_τ=([\d.]+), dyn_nodes=(\d+), last score=([\d.]+), cpu_s=([\d.]+)%, T_db=([\d.]+)ms\) on (\S+)")
def _scaleup_storage_triggered(m, ctrl):
    return _row(m.group(1), ctrl, "scale_up_triggered", "storage", detail={
        "windows_hit": int(m.group(2)), "windows_total": int(m.group(3)),
        "threshold": float(m.group(4)), "effective_threshold": float(m.group(5)),
        "dyn_nodes": int(m.group(6)), "score": float(m.group(7)),
        "cpu_pct": float(m.group(8)), "t_db_ms": float(m.group(9)),
        "network": m.group(10),
    })


@_pat(_TS + r" (?:INFO|DEBUG) .+\[scale-up\] compute triggered: (\d+)/(\d+) windows ≥ ([\d.]+) "
      r"\(τ_eff=([\d.]+), τ_base=([\d.]+), peer_relief=([\d.]+), peer_score=([^,]+), dyn=(\d+), last score=([\d.]+), cpu=([\d.]+)%, T_proc=([\d.]+)ms\) on (\S+)")
def _scaleup_compute_triggered_peer_aware(m, ctrl):
    return _row(m.group(1), ctrl, "scale_up_triggered", "compute", detail={
        "windows_hit": int(m.group(2)), "windows_total": int(m.group(3)),
        "threshold": float(m.group(4)), "effective_threshold": float(m.group(5)),
        "base_threshold": float(m.group(6)), "peer_relief": float(m.group(7)),
        "peer_score": _maybe_float(m.group(8)), "dyn_nodes": int(m.group(9)),
        "score": float(m.group(10)), "cpu_pct": float(m.group(11)),
        "t_proc_ms": float(m.group(12)), "network": m.group(13),
    })


@_pat(_TS + r" (?:INFO|DEBUG) .+\[scale-up\] compute triggered: (\d+)/(\d+) windows ≥ ([\d.]+) "
      r"\(last score=([\d.]+), cpu=([\d.]+)%, T_proc=([\d.]+)ms\) on (\S+)")
def _scaleup_compute_triggered(m, ctrl):
    return _row(m.group(1), ctrl, "scale_up_triggered", "compute", detail={
        "windows_hit": int(m.group(2)), "windows_total": int(m.group(3)),
        "threshold": float(m.group(4)), "score": float(m.group(5)),
        "cpu_pct": float(m.group(6)), "t_proc_ms": float(m.group(7)),
        "network": m.group(8),
    })


# --- Scale-up score (window entry, not yet triggered) ---

@_pat(_TS + r" (?:INFO|DEBUG) .+\[scale-up\] storage score=([\d.]+) \(τ_eff=([\d.]+), base=([\d.]+) \+(\d+)×([\d.]+)\) "
      r"cpu_s=([\d.]+)% T_db=([\d.]+)ms  window=(\d+)/(\d+) on (\S+)")
def _scaleup_storage_score(m, ctrl):
    return _row(m.group(1), ctrl, "scale_up_score", "storage", detail={
        "score": float(m.group(2)), "effective_threshold": float(m.group(3)),
        "base_threshold": float(m.group(4)), "dyn_nodes": int(m.group(5)),
        "threshold_increment": float(m.group(6)),
        "cpu_pct": float(m.group(7)), "t_db_ms": float(m.group(8)),
        "window_pos": int(m.group(9)), "window_total": int(m.group(10)),
        "network": m.group(11),
    })


@_pat(_TS + r" (?:INFO|DEBUG) .+\[scale-up\] compute score=([\d.]+) "
      r"\(τ_eff=([\d.]+), τ_base=([\d.]+), peer_relief=([\d.]+), peer_score=([^,]+), dyn=(\d+)\) "
      r"cpu=([\d.]+)% T_proc=([\d.]+)ms window=(\d+)/(\d+) on (\S+)")
def _scaleup_compute_score_peer_aware(m, ctrl):
    return _row(m.group(1), ctrl, "scale_up_score", "compute", detail={
        "score": float(m.group(2)), "effective_threshold": float(m.group(3)),
        "base_threshold": float(m.group(4)), "peer_relief": float(m.group(5)),
        "peer_score": _maybe_float(m.group(6)), "dyn_nodes": int(m.group(7)),
        "cpu_pct": float(m.group(8)), "t_proc_ms": float(m.group(9)),
        "window_pos": int(m.group(10)), "window_total": int(m.group(11)),
        "network": m.group(12),
    })


@_pat(_TS + r" (?:INFO|DEBUG) .+\[scale-up\] compute score=([\d.]+) \(τ=([\d.]+)\) "
      r"cpu=([\d.]+)% T_proc=([\d.]+)ms  window=(\d+)/(\d+) on (\S+)")
def _scaleup_compute_score(m, ctrl):
    return _row(m.group(1), ctrl, "scale_up_score", "compute", detail={
        "score": float(m.group(2)), "threshold": float(m.group(3)),
        "cpu_pct": float(m.group(4)), "t_proc_ms": float(m.group(5)),
        "window_pos": int(m.group(6)), "window_total": int(m.group(7)),
        "network": m.group(8),
    })



@_pat(_TS + r" INFO .+\[elasticity\] compute: spawning (\S+) on LAN (\d+)")
def _spawning_compute(m, ctrl):
    return _row(m.group(1), ctrl, "node_spawning", "compute",
                container=m.group(2), detail={"lan": int(m.group(3))})


@_pat(_TS + r" INFO .+\[elasticity\] data: spawning (\S+) on LAN (\d+)")
def _spawning_data(m, ctrl):
    return _row(m.group(1), ctrl, "node_spawning", "storage",
                container=m.group(2), detail={"lan": int(m.group(3))})



@_pat(_TS + r" INFO .+\[elasticity\] compute: (\S+) online  ip=(\S+)  mac=(\S+)")
def _online_compute(m, ctrl):
    return _row(m.group(1), ctrl, "node_online", "compute",
                container=m.group(2), ip=m.group(3), mac=m.group(4))


@_pat(_TS + r" INFO .+\[elasticity\] data: (\S+) online  ip=(\S+)  mac=(\S+)")
def _online_data(m, ctrl):
    return _row(m.group(1), ctrl, "node_online", "storage",
                container=m.group(2), ip=m.group(3), mac=m.group(4))



@_pat(_TS + r" INFO .+\[node_add\] timing  container=(\S+)  docker_run=([\d.]+)s"
      r"  net_attach=([\d.]+)s  rs_join=([\d.]+)s  total=([\d.]+)s  state=(\S+)")
def _node_add_timing(m, ctrl):
    return _row(m.group(1), ctrl, "node_add_timing", container=m.group(2), detail={
        "docker_run_s": float(m.group(3)), "net_attach_s": float(m.group(4)),
        "rs_join_s": float(m.group(5)), "total_s": float(m.group(6)),
        "state": m.group(7),
    })



@_pat(_TS + r" INFO .+\[node_ready\] timing  container=(\S+)  node_type=(\S+)"
      r"  source=(\S+)  total=([\d.]+)s  state=(\S+)")
def _node_ready_timing(m, ctrl):
    return _row(m.group(1), ctrl, "node_ready_timing", m.group(3),
                container=m.group(2), detail={
                    "source": m.group(4),
                    "total_s": float(m.group(5)),
                    "state": m.group(6),
                })



@_pat(_TS + r" INFO .+\[scale-down\] compute underutilisation: (\d+)/(\d+) "
      r"windows below threshold — removing (\S+)")
def _scaledown_compute(m, ctrl):
    return _row(m.group(1), ctrl, "scale_down_triggered", "compute",
                container=m.group(4), detail={
                    "windows_hit": int(m.group(2)), "windows_total": int(m.group(3)),
                })


@_pat(_TS + r" INFO .+\[scale-down\] storage underutilisation: (\d+)/(\d+) "
      r"windows below threshold — removing (\S+)")
def _scaledown_storage(m, ctrl):
    return _row(m.group(1), ctrl, "scale_down_triggered", "storage",
                container=m.group(4), detail={
                    "windows_hit": int(m.group(2)), "windows_total": int(m.group(3)),
                })



@_pat(_TS + r" INFO .+\[elasticity\] scale_down_compute: removing (\S+) \(mac=(\S+)\)")
def _removing_compute(m, ctrl):
    return _row(m.group(1), ctrl, "node_removing", "compute",
                container=m.group(2), mac=m.group(3))


@_pat(_TS + r" INFO .+\[elasticity\] scale_down_data: removing (\S+) \(mac=(\S+)\)")
def _removing_data(m, ctrl):
    return _row(m.group(1), ctrl, "node_removing", "storage",
                container=m.group(2), mac=m.group(3))



@_pat(_TS + r" INFO .+\[scale-down\] drain_complete received for mac=(\S+)")
def _drain_complete(m, ctrl):
    return _row(m.group(1), ctrl, "drain_complete", mac=m.group(2))



@_pat(_TS + r" INFO .+\[elasticity\] cleanup_compute done: container=(\S+)")
def _cleanup_compute_done(m, ctrl):
    return _row(m.group(1), ctrl, "cleanup_done", "compute", container=m.group(2))


@_pat(_TS + r" INFO .+\[elasticity\] scale_down_data done: container=(\S+)")
def _cleanup_data_done(m, ctrl):
    return _row(m.group(1), ctrl, "cleanup_done", "storage", container=m.group(2))



@_pat(_TS + r" INFO .+\[node_remove\] timing  container=(\S+)  drain_signal=([\d.]+)s"
      r"  net_cleanup=([\d.]+)s  total=([\d.]+)s  state=(\S+)")
def _node_remove_timing(m, ctrl):
    return _row(m.group(1), ctrl, "node_remove_timing", container=m.group(2), detail={
        "drain_signal_s": float(m.group(3)), "net_cleanup_s": float(m.group(4)),
        "total_s": float(m.group(5)), "state": m.group(6),
    })



def parse_log(path: Path, controller: str) -> list[dict]:
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for regex, builder in PATTERNS:
                m = regex.search(line)
                if m:
                    events.append(builder(m, controller))
                    break
    return events


def infer_controller_name(path: Path) -> str:
    name = path.stem.lower()
    if "lan1" in name or "osken_1" in name or name == "osken":
        return "lan1"
    if "lan2" in name or "osken_2" in name:
        return "lan2"
    return name


# Policy-state annotation helpers


@dataclass
class PolicyAnnotation:
    """One controller-only annotation for a policy-state window."""
    ts: float
    lan: str  # "lan1" or "lan2"
    kind: str  # busy | cooldown_compute | cooldown_storage | blocked_compute
               # | blocked_storage | triggered_compute | triggered_storage
               # | candidate_compute | candidate_storage
               # | scaleup_cooldown_compute | scaleup_cooldown_storage
               # | no_candidate_compute | no_candidate_storage
    detail: str = ""


_RE_POL_BUSY = re.compile(
    _TS + r".*\[scale-down\] elasticity manager is busy"
)
_RE_POL_COOLDOWN = re.compile(
    _TS + r".*\[scale-down\] (compute|storage) within (\d+)s cooldown"
)
_RE_POL_BLOCKED = re.compile(
    _TS + r".*\[scale-up\] (compute|storage) blocked"
)
_RE_POL_TRIGGERED = re.compile(
    _TS + r".*\[scale-up\] (compute|storage) triggered:"
)
_RE_POL_CANDIDATE = re.compile(
    _TS + r".*\[scale-down\] (compute|storage) candidate selected: (\S+)"
)
_RE_POL_SCALEUP_COOLDOWN = re.compile(
    _TS + r".*\[scale-up\] (compute|storage) within (\d+)s cooldown"
)
_RE_POL_NO_CANDIDATE = re.compile(
    _TS + r".*\[scale-down\] (compute|storage) no candidate"
)


def parse_policy_annotations(log_path: Path, lan: str) -> list[PolicyAnnotation]:
    """Extract policy-state annotations from a controller log file.

    These annotations capture controller-only outcomes (busy, cooldown,
    blocked, triggered, candidate selection, no-candidate clears) that
    are not derivable from metrics alone.
    """
    import time as _time

    annotations: list[PolicyAnnotation] = []
    if not log_path.exists():
        return annotations

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return annotations

    for line in lines:
        m = _RE_POL_BUSY.search(line)
        if m:
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan, kind="busy"))
            continue

        m = _RE_POL_COOLDOWN.search(line)
        if m:
            tier = m.group(2)
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan,
                kind=f"cooldown_{tier}",
                detail=f"{m.group(3)}s"))
            continue

        m = _RE_POL_BLOCKED.search(line)
        if m:
            tier = m.group(2)
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan,
                kind=f"blocked_{tier}"))
            continue

        m = _RE_POL_TRIGGERED.search(line)
        if m:
            tier = m.group(2)
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan,
                kind=f"triggered_{tier}"))
            continue

        m = _RE_POL_CANDIDATE.search(line)
        if m:
            tier = m.group(2)
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan,
                kind=f"candidate_{tier}",
                detail=m.group(3)))
            continue

        m = _RE_POL_SCALEUP_COOLDOWN.search(line)
        if m:
            tier = m.group(2)
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan,
                kind=f"scaleup_cooldown_{tier}",
                detail=f"{m.group(3)}s"))
            continue

        m = _RE_POL_NO_CANDIDATE.search(line)
        if m:
            tier = m.group(2)
            annotations.append(PolicyAnnotation(
                ts=_parse_ts_to_epoch(m.group(1)), lan=lan,
                kind=f"no_candidate_{tier}"))
            continue

    return annotations


def _parse_ts_to_epoch(ts_str: str) -> float:
    """Parse a log timestamp string to Unix epoch float."""
    import time as _time
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S.%f",
                 "%Y-%m-%d %H:%M:%S"):
        try:
            return _time.mktime(_time.strptime(ts_str, fmt))
        except ValueError:
            continue
    return 0.0



CSV_COLUMNS = ["timestamp", "controller", "event_type", "node_type",
               "container", "mac", "ip", "detail"]

TIMING_COLUMNS = [
    "timestamp", "controller", "operation", "node_type", "container", "state",
    "ready_source",
    "docker_run_s", "net_attach_s", "rs_join_s", "drain_signal_s",
    "net_cleanup_s", "total_s",
]


def infer_node_type(container: str) -> str:
    if container.startswith("edge_server_"):
        return "compute"
    if container.startswith("edge_storage_"):
        return "storage"
    return ""


def node_lifecycle_timing_rows(events: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for event in events:
        event_type = event.get("event_type", "")
        if event_type not in {"node_add_timing", "node_ready_timing", "node_remove_timing"}:
            continue

        detail_raw = event.get("detail") or "{}"
        try:
            detail = json.loads(detail_raw)
        except json.JSONDecodeError:
            detail = {}

        container = event.get("container", "")
        if event_type == "node_add_timing":
            operation = "add"
        elif event_type == "node_ready_timing":
            operation = "ready"
        else:
            operation = "remove"
        rows.append({
            "timestamp": event.get("timestamp", ""),
            "controller": event.get("controller", ""),
            "operation": operation,
            "node_type": event.get("node_type") or infer_node_type(container),
            "container": container,
            "state": detail.get("state", ""),
            "ready_source": detail.get("source", ""),
            "docker_run_s": detail.get("docker_run_s", ""),
            "net_attach_s": detail.get("net_attach_s", ""),
            "rs_join_s": detail.get("rs_join_s", ""),
            "drain_signal_s": detail.get("drain_signal_s", ""),
            "net_cleanup_s": detail.get("net_cleanup_s", ""),
            "total_s": detail.get("total_s", ""),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Parse SDN controller logs into elasticity_events.csv")
    parser.add_argument("logs", nargs="+", type=Path, help="Controller log file(s)")
    parser.add_argument("-o", "--output", type=Path, default=Path("elasticity_events.csv"),
                        help="Output CSV path (default: elasticity_events.csv)")
    parser.add_argument("--timings-output", type=Path,
                        help="Optional flattened node add/ready/remove timing CSV path")
    parser.add_argument("--controller-names", nargs="*",
                        help="Controller name for each log file (inferred from filename if omitted)")
    args = parser.parse_args()

    if args.controller_names and len(args.controller_names) != len(args.logs):
        print("ERROR: --controller-names count must match number of log files", file=sys.stderr)
        sys.exit(1)

    all_events: list[dict] = []
    for i, log_path in enumerate(args.logs):
        if not log_path.exists():
            print(f"WARNING: {log_path} not found — skipping", file=sys.stderr)
            continue
        ctrl = args.controller_names[i] if args.controller_names else infer_controller_name(log_path)
        events = parse_log(log_path, ctrl)
        all_events.extend(events)
        print(f"  {log_path}: {len(events)} events (controller={ctrl})", file=sys.stderr)

    all_events.sort(key=lambda e: e["timestamp"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(all_events)

    print(f"Wrote {len(all_events)} events → {args.output}", file=sys.stderr)

    timings_output = args.timings_output
    if timings_output is None:
        timings_output = args.output.with_name("node_lifecycle_timings.csv")
    timing_rows = node_lifecycle_timing_rows(all_events)
    timings_output.parent.mkdir(parents=True, exist_ok=True)
    with open(timings_output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TIMING_COLUMNS)
        writer.writeheader()
        writer.writerows(timing_rows)
    print(f"Wrote {len(timing_rows)} node timing rows → {timings_output}", file=sys.stderr)


if __name__ == "__main__":
    main()
