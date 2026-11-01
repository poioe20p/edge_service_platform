#!/usr/bin/env python3
"""RQ3 low-headroom readiness analysis.

This module is intentionally artifact-only: it never starts containers o
changes experiment state.  It supports the historical lock, outcome-blind
capacity screens, the two-pair preflight, and the event-source-absence cell.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WINDOW_S = 7.0
MIN_WINDOW_REQUESTS = 100
MIN_SUCCESS_REQUESTS = 100
PRACTICAL_FLOOR_S = 0.010
ANCHOR_LEAD_S = 30.0
FIRST_WAVE_S = 120.0
PRE_CPU_S = 30.0
RECOVERY_LAG_S = 60.0
RECOVERY_SPAN_S = 180.0
# Bridge CPU band: archived pre-anchor pooled range 50.3-56.5 percent with a
# fixed +/-10 percentage-point tolerance (2026-09-06 re-anchor; the original
# band compared a plateau-wide measure against a pre-anchor-window quantity).
BRIDGE_CPU_LOW = 40.3
BRIDGE_CPU_HIGH = 66.5
BRIDGE_SEPARATION_MIN_S = 5.0
PHASE = "compute_plateau"
DIRECT = "direct"
DISCOVERY = "discovery"
EVENT_ABSENT = "event_absent"


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def timestamp(value: Any) -> float | None:
    """Parse epoch or ISO-8601 timestamps used by run artifacts."""
    number = as_float(value)
    if number is not None:
        return number
    if not value:
        return None
    text = str(value).strip().strip('"')
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python <=3.10 fromisoformat accepts at most 6 fractional seconds digits;
    # container logs carry 9-digit fractions, so truncate before parsing.
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)(.*)", text
    )
    if match and len(match.group(2)) > 6:
        text = f"{match.group(1)}.{match.group(2)[:6]}{match.group(3)}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_phases(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "phases_snapshot.json"
    if not path.exists():
        raise ValueError(f"missing phases snapshot: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    phases = data.get("phases", data) if isinstance(data, dict) else data
    if not isinstance(phases, list):
        raise ValueError(f"invalid phases snapshot: {path}")
    return phases


def phase_bounds(run_dir: Path) -> dict[str, tuple[float, float]]:
    """Phase bounds from generator-labeled request `sent_at` values.

    `run_status.started_at` precedes setup and traffic launch, so it cannot
    anchor phase windows. The driver's own phase labels are authoritative:
    each phase's bounds are [min sent_at, max sent_at] over its rows.
    """
    by_phase: dict[str, list[float]] = {}
    for row in read_csv(run_dir / "client_requests.csv"):
        sent = timestamp(row.get("sent_at"))
        name = row.get("phase")
        if sent is not None and name:
            by_phase.setdefault(name, []).append(sent)
    if not by_phase:
        raise ValueError(f"no labeled phase requests: {run_dir}")
    return {name: (min(times), max(times)) for name, times in by_phase.items()}


def parse_ready_epoch(path: Path) -> float | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "app ready: MongoDB ping" not in line:
            continue
        first = line.split(maxsplit=1)[0] if line.split() else ""
        parsed = timestamp(first)
        if parsed is not None:
            return parsed
    return None


def request_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_csv(run_dir / "client_requests.csv"):
        sent = timestamp(row.get("sent_at"))
        if sent is None:
            continue
        row["_sent"] = sent
        row["_latency"] = as_float(row.get("latency_s"))
        rows.append(row)
    return rows


def spawned(run_dir: Path) -> list[dict[str, Any]]:
    """All admission-log spawn candidates, regardless of admission result.

    Anchor selection must not condition on admission (treatment-dependent),
    so candidates come from every admission-log row with a spawn timestamp.
    """
    result: list[dict[str, Any]] = []
    for lan in (1, 2):
        for row in read_csv(run_dir / f"admission_log_lan{lan}.csv"):
            spawn = timestamp(row.get("spawn_started_ts"))
            if spawn is None:
                continue
            item = dict(row)
            item["lan"] = lan
            item["_spawn"] = spawn
            item["_spawn_complete"] = timestamp(row.get("spawn_complete_ts")) or spawn
            item["_admitted"] = timestamp(row.get("admitted_ts"))
            log_path = run_dir / "service_logs" / f"{row.get('container', '')}.log"
            item["_log_missing"] = not log_path.exists()
            item["_true_ready"] = parse_ready_epoch(log_path)
            result.append(item)
    return result


def admissions(run_dir: Path) -> list[dict[str, Any]]:
    """Admitted spawn candidates only."""
    return [
        row for row in spawned(run_dir)
        if row.get("result") == "admitted" and row["_admitted"] is not None
    ]


def run_arm(run_dir: Path) -> str:
    env = parse_env(run_dir / "controller_env_snapshot.env")
    propagation = env.get("READINESS_PROPAGATION", "")
    if propagation == DISCOVERY:
        if env.get("EDGE_APP_READY_EVENT") == "0":
            raise ValueError(
                f"unsupported arm combination (discovery + event absence): {run_dir}")
        return DISCOVERY
    if env.get("EDGE_APP_READY_EVENT") == "0":
        return EVENT_ABSENT
    if propagation == DIRECT or env.get("EDGE_APP_READY_EVENT") == "1":
        return DIRECT
    raise ValueError(
        f"cannot classify arm from env snapshot ({propagation!r}, "
        f"EDGE_APP_READY_EVENT={env.get('EDGE_APP_READY_EVENT')!r}): {run_dir}")


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(value for value in values if value is not None and math.isfinite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def is_completed(row: dict[str, Any]) -> bool:
    return row.get("status") == "completed"


def is_success(row: dict[str, Any]) -> bool:
    status = str(row.get("http_status", ""))
    return is_completed(row) and status.startswith("2")


def window_metric(rows: list[dict[str, Any]], lan: int, start: float, end: float) -> dict[str, Any]:
    selected = [
        row for row in rows
        if row.get("client_lan") == f"lan{lan}"
        and row.get("phase") == PHASE
        and start <= row["_sent"] < end
    ]
    service_rows = [row for row in selected if row.get("status") in {"completed", "timeout"}]
    success_latencies = [row["_latency"] for row in selected if is_success(row) and row["_latency"] is not None]
    completed_latencies = [row["_latency"] for row in selected if is_completed(row) and row["_latency"] is not None]
    completed_count = sum(1 for row in selected if is_completed(row))
    timeout_count = sum(1 for row in selected if row.get("status") == "timeout")
    failure_count = sum(1 for row in selected if is_completed(row) and not is_success(row))
    if len(service_rows) < MIN_WINDOW_REQUESTS:
        raise ValueError(
            f"request floor failed for lan{lan}: {len(service_rows)} < {MIN_WINDOW_REQUESTS}"
        )
    if len(success_latencies) < MIN_SUCCESS_REQUESTS:
        raise ValueError(
            f"success floor failed for lan{lan}: {len(success_latencies)} < {MIN_SUCCESS_REQUESTS}"
        )
    return {
        "requests": len(service_rows),
        "successful": len(success_latencies),
        "p95_s": percentile(success_latencies, 0.95),
        "p95_all_s": percentile(completed_latencies, 0.95),
        "timeout_rate": timeout_count / len(service_rows) if service_rows else None,
        "failure_rate": failure_count / completed_count if completed_count else None,
    }


def labeled_plateau_bounds(rows: list[dict[str, Any]]) -> dict[int, tuple[float, float]]:
    """Per-LAN [start, end] of generator-labeled compute_plateau requests."""
    bounds: dict[int, tuple[float, float]] = {}
    for lan in (1, 2):
        times = [
            row["_sent"] for row in rows
            if row.get("phase") == PHASE and row.get("client_lan") == f"lan{lan}"
        ]
        if not times:
            raise ValueError(f"no labeled {PHASE} requests for lan{lan}")
        bounds[lan] = (min(times), max(times))
    return bounds


def first_anchors(run_dir: Path,
                  rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """First plateau-eligible dynamic true-ready backend per LAN.

    2026-09-06 amendment: the primary anchor is the earliest candidate whose
    passive true-ready falls in [plateau_lan_start + ANCHOR_LEAD_S,
    plateau_lan_end - WINDOW_S), with plateau bounds from the
    generator-labeled request timeline. Candidates come from the admission
    log regardless of result (no admission conditioning); a selected
    candidate that is not admitted fails the run rather than being skipped.
    """
    bounds = labeled_plateau_bounds(rows)
    candidates = spawned(run_dir)
    anchors: dict[int, dict[str, Any]] = {}
    for lan in (1, 2):
        start, end = bounds[lan]
        missing = [
            row for row in candidates
            if row["lan"] == lan and row.get("_log_missing")
            and row.get("result") == "admitted"
            and start <= row["_spawn"] <= end
        ]
        if missing:
            raise ValueError(
                f"missing service log for plateau-admitted candidate, lan{lan}: {run_dir}")
        eligible = [
            row for row in candidates
            if row["lan"] == lan and row["_true_ready"] is not None
            and start + ANCHOR_LEAD_S <= row["_true_ready"] < end - WINDOW_S
        ]
        if not eligible:
            raise ValueError(
                f"no plateau-eligible true-ready candidate for lan{lan}: {run_dir}")
        anchor = min(eligible, key=lambda row: row["_true_ready"])
        if anchor.get("result") != "admitted" or anchor["_admitted"] is None:
            raise ValueError(
                f"selected plateau-eligible anchor is not admitted for lan{lan}: {run_dir}")
        anchor["_anchor_rank"] = sum(
            1 for row in eligible if row["_true_ready"] <= anchor["_true_ready"])
        anchor["_n_eligible"] = len(eligible)
        anchors[lan] = anchor
    return anchors


def analyze_run(run_dir: Path, include_qoe: bool = True) -> dict[str, Any]:
    env = parse_env(run_dir / "controller_env_snapshot.env")
    rows = request_rows(run_dir)
    anchors = first_anchors(run_dir, rows)
    lan_bounds = labeled_plateau_bounds(rows)
    phase = phase_bounds(run_dir).get(PHASE)
    if phase is None:
        raise ValueError(f"missing {PHASE} phase: {run_dir}")
    status_path = run_dir / "run_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    output: dict[str, Any] = {
        "run": run_dir.name,
        "arm": run_arm(run_dir),
        "run_status": status.get("status"),
        "readiness_propagation": env.get("READINESS_PROPAGATION", ""),
        "event_fraction": None,
        "anchors": [],
    }
    all_admissions = admissions(run_dir)
    event_count = sum(1 for row in all_admissions if row.get("admit_source") == "event")
    output["event_fraction"] = event_count / len(all_admissions) if all_admissions else None
    deltas: list[float] = []
    for lan in (1, 2):
        anchor = anchors[lan]
        ready = anchor["_true_ready"]
        admission = anchor["_admitted"]
        lan_start, _ = lan_bounds[lan]
        item: dict[str, Any] = {
            "lan": lan,
            "container": anchor.get("container", ""),
            "true_ready_ts": ready,
            "admitted_ts": admission,
            "ready_to_admit_s": admission - ready,
            "plateau_start_ts": phase[0],
            "plateau_end_ts": phase[1],
            "anchor_lead_s": ready - lan_start,
            "anchor_rank": anchor.get("_anchor_rank"),
            "n_eligible": anchor.get("_n_eligible"),
        }
        if include_qoe:
            pre = window_metric(rows, lan, ready - WINDOW_S, ready)
            post = window_metric(rows, lan, ready, ready + WINDOW_S)
            item["pre"] = pre
            item["post"] = post
            item["delta_p95_s"] = (
                post["p95_s"] - pre["p95_s"]
                if post["p95_s"] is not None and pre["p95_s"] is not None else None
            )
            if item["delta_p95_s"] is not None:
                deltas.append(item["delta_p95_s"])
        output["anchors"].append(item)
    output["run_delta_p95_s"] = sum(deltas) / len(deltas) if len(deltas) == 2 else None
    output["phase_start_guard"] = all(
        item["anchor_lead_s"] >= ANCHOR_LEAD_S for item in output["anchors"]
    )
    return output


def load_window_log(run_dir: Path, lan: int) -> list[dict[str, Any]]:
    path = run_dir / f"window_log_lan{lan}.jsonl"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        end = as_float(row.get("window_end"))
        if end is not None:
            row["_window_end"] = end
            rows.append(row)
    return rows


def cpu_values(rows: list[dict[str, Any]], start: float, end: float,
               server_ids: set[str] | None = None) -> list[float]:
    values: list[float] = []
    for row in rows:
        if not start <= row["_window_end"] < end:
            continue
        for server_id, server in (row.get("servers") or {}).items():
            if server_ids is not None and server_id not in server_ids:
                continue
            value = as_float((server or {}).get("avg_cpu_percent"))
            if value is not None:
                values.append(value)
    return values


def status_rates(rows: list[dict[str, Any]], start: float, end: float) -> dict[str, Any]:
    selected = [row for row in rows if start <= row["_sent"] < end]
    offered = len(selected)
    canceled = sum(row.get("status") in {"canceled", "dropped"} for row in selected)
    service = [row for row in selected if row.get("status") in {"completed", "timeout"}]
    completed = sum(is_completed(row) for row in service)
    timeouts = sum(row.get("status") == "timeout" for row in service)
    failures = sum(is_completed(row) and not is_success(row) for row in service)
    return {
        "offered": offered,
        "canceled_dropped": canceled,
        "cancel_rate": canceled / offered if offered else None,
        "http000": sum(row.get("http_status") == "000" for row in selected),
        "timeouts": timeouts,
        "timeout_rate": timeouts / len(service) if service else None,
        "failure_rate": failures / completed if completed else None,
        "service_requests": len(service),
    }


def decision_times(run_dir: Path, lan: int) -> list[float]:
    """scale_up decision timestamps for a LAN from the decision log."""
    path = run_dir / f"decision_log_lan{lan}.csv"
    times = []
    for row in read_csv(path):
        if (row.get("action_type") or "").lower() != "scale_up":
            continue
        value = timestamp(row.get("ts"))
        if value is not None:
            times.append(value)
    return times


def capacity_summary(run_dir: Path, expected_quota: str | None = None) -> dict[str, Any]:
    rows = request_rows(run_dir)
    lan_bounds = labeled_plateau_bounds(rows)
    phase = phase_bounds(run_dir)[PHASE]
    plateau_start, plateau_end = phase
    all_admissions = admissions(run_dir)
    anchors = first_anchors(run_dir, rows)
    window_logs = {lan: load_window_log(run_dir, lan) for lan in (1, 2)}
    pre_cpu: dict[int, dict[str, Any]] = {}
    recovery_cpu: dict[int, dict[str, Any]] = {}
    first_wave: dict[int, list[dict[str, Any]]] = {}
    first_wave_decisions: dict[int, int] = {}
    first_decision: dict[int, float | None] = {}
    pre_offered: dict[int, int] = {}
    for lan in (1, 2):
        lan_start, lan_end = lan_bounds[lan]
        ready = anchors[lan]["_true_ready"]
        pre_rows = [row for row in window_logs[lan]
                    if ready - PRE_CPU_S <= row["_window_end"] < ready]
        old_ids = set()
        for row in pre_rows:
            old_ids.update((row.get("servers") or {}).keys())
        before_old = cpu_values(window_logs[lan], ready - PRE_CPU_S, ready, old_ids)
        pre_cpu[lan] = {
            "median": statistics.median(before_old) if before_old else None,
            "p95": percentile(before_old, 0.95), "n": len(before_old),
            "old_ids": sorted(old_ids),
        }
        pre_offered[lan] = sum(
            1 for row in rows
            if row.get("client_lan") == f"lan{lan}"
            and ready - PRE_CPU_S <= row["_sent"] < ready
        )
        fw = [
            row for row in all_admissions
            if row["lan"] == lan and row["_admitted"] is not None
            and lan_start <= row["_admitted"] < lan_start + FIRST_WAVE_S
        ]
        if not fw:
            raise ValueError(
                f"no first-wave admissions for lan{lan} in first {FIRST_WAVE_S:.0f} s: {run_dir}")
        first_wave[lan] = fw
        decisions = decision_times(run_dir, lan)
        first_decision[lan] = min(decisions) if decisions else None
        first_wave_decisions[lan] = sum(
            1 for t in decisions if lan_start <= t < lan_start + FIRST_WAVE_S)
        last = max(row["_admitted"] for row in fw)
        recovery_start = last + RECOVERY_LAG_S
        recovery_end = last + RECOVERY_LAG_S + RECOVERY_SPAN_S
        if recovery_end > lan_end:
            raise ValueError(f"recovery window spills past lan{lan} plateau: {run_dir}")
        values = cpu_values(window_logs[lan], recovery_start, recovery_end, old_ids)
        recovery_cpu[lan] = {"median": statistics.median(values) if values else None,
                             "n": len(values), "window": [recovery_start, recovery_end]}
    baseline_start, baseline_end = phase_bounds(run_dir)["baseline"]
    baseline = status_rates(rows, baseline_start, baseline_end)
    final = status_rates(rows, plateau_end - 120.0, plateau_end)
    offered_total = len(rows)
    canceled_total = sum(row.get("status") in {"canceled", "dropped"} for row in rows)
    ready_to_admit = [
        row["_admitted"] - row["_true_ready"] for row in all_admissions
        if row["_true_ready"] is not None
    ]
    scale_down = False
    for event in read_csv(run_dir / "elasticity_events.csv"):
        event_time = timestamp(event.get("timestamp_s") or event.get("timestamp"))
        kind = (event.get("event_type") or event.get("event") or "").lower()
        if event_time is not None and plateau_start <= event_time < plateau_end and "scale_down" in kind:
            scale_down = True
            break
    quota_snapshot = run_dir / "quota_snapshot.json"
    quota_ok = None
    if expected_quota is not None:
        quota_ok = False
        if quota_snapshot.exists():
            data = json.loads(quota_snapshot.read_text(encoding="utf-8"))
            quota_ok = bool(data.get("all_compute_containers_match")) and str(
                data.get("requested_edge_cpus")) == str(expected_quota)
    reliefs = []
    for lan in (1, 2):
        pre = pre_cpu[lan]["median"]
        post = recovery_cpu[lan]["median"]
        if pre is not None and post is not None:
            reliefs.append(pre - post)
    status_path = run_dir / "run_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    event_count = sum(1 for row in all_admissions if row.get("admit_source") == "event")
    return {
        "run": run_dir.name,
        "arm": run_arm(run_dir),
        "run_status": status.get("status"),
        "event_fraction": event_count / len(all_admissions) if all_admissions else None,
        "admissions_lan1": sum(row["lan"] == 1 for row in all_admissions),
        "admissions_lan2": sum(row["lan"] == 2 for row in all_admissions),
        "first_wave_lan1": len(first_wave[1]),
        "first_wave_lan2": len(first_wave[2]),
        "first_wave_decisions_lan1": first_wave_decisions[1],
        "first_wave_decisions_lan2": first_wave_decisions[2],
        "first_decision": first_decision,
        "pre_offered": pre_offered,
        "phase_start_guard": all(
            anchors[lan]["_true_ready"] >= lan_bounds[lan][0] + ANCHOR_LEAD_S
            for lan in (1, 2)
        ),
        "pre_cpu": pre_cpu,
        "recovery_cpu": recovery_cpu,
        "relief_pp": reliefs,
        "baseline": baseline,
        "final": final,
        "run_cancel_rate": canceled_total / offered_total if offered_total else None,
        "ready_to_admit_median": statistics.median(ready_to_admit) if ready_to_admit else None,
        "scale_down_in_plateau": scale_down,
        "quota_snapshot_ok": quota_ok,
    }


def label_key(run_name: str) -> str | None:
    match = re.search(r"(?:direct|disc(?:overy)?|event_absent)[_-](\d+)$", run_name)
    return match.group(1) if match else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_seed(run_dir: Path) -> int | None:
    """base_seed recorded in the run's open-loop schedule."""
    path = run_dir / "open_loop_schedule.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    value = as_float(data.get("base_seed"))
    return int(value) if value is not None else None


def artifact_hashes(run_dir: Path) -> dict[str, Any]:
    """Provenance hashes for the plan's lock-record artifact contract."""
    files = [
        "client_requests.csv", "admission_log_lan1.csv", "admission_log_lan2.csv",
        "phases_snapshot.json", "controller_env_snapshot.env", "run_status.json",
        "open_loop_schedule.json",
    ]
    out: dict[str, Any] = {}
    for name in files:
        path = run_dir / name
        out[name] = sha256(path) if path.exists() else "missing"
    logs_dir = run_dir / "service_logs"
    out["service_logs"] = (
        {path.name: sha256(path) for path in sorted(logs_dir.glob("*"))}
        if logs_dir.is_dir() else {}
    )
    return out


def historical_lock(args: argparse.Namespace) -> int:
    records = [analyze_run(Path(path), include_qoe=True) for path in args.run_dir]
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    seeds: dict[str, int] = {}
    seeds_by_run: dict[str, int] = {}
    for path, record in zip(args.run_dir, records):
        key = label_key(record["run"])
        if key is None:
            raise ValueError(f"cannot pair historical run label: {record['run']}")
        seed = run_seed(Path(path))
        if seed is None:
            raise ValueError(f"missing base_seed provenance: {path}")
        seeds[key] = seed
        seeds_by_run[record["run"]] = seed
        groups.setdefault(key, {})[record["arm"]] = record
    if set(groups) != {str(number) for number in range(1, 8)}:
        raise ValueError(f"expected historical pair keys 1..7, got {sorted(groups)}")
    pairs = []
    for key in sorted(groups, key=int):
        direct = groups[key].get(DIRECT)
        discovery = groups[key].get(DISCOVERY)
        if direct is None or discovery is None:
            raise ValueError(f"incomplete historical pair {key}")
        if seeds_by_run[direct["run"]] != seeds_by_run[discovery["run"]]:
            raise ValueError(f"seed mismatch within historical pair {key}")
        direct_lans = [anchor["delta_p95_s"] for anchor in direct["anchors"]]
        discovery_lans = [anchor["delta_p95_s"] for anchor in discovery["anchors"]]
        difference = discovery["run_delta_p95_s"] - direct["run_delta_p95_s"]
        pairs.append({
            "seed_key": key,
            "seed": seeds.get(key),
            "direct": direct["run_delta_p95_s"],
            "discovery": discovery["run_delta_p95_s"],
            "difference": difference,
            "direct_lan_deltas": direct_lans,
            "discovery_lan_deltas": discovery_lans,
        })
    envelope = max(PRACTICAL_FLOOR_S, max(abs(row["difference"]) for row in pairs))
    output = {
        "window_s": WINDOW_S,
        "min_window_requests": MIN_WINDOW_REQUESTS,
        "min_window_successes": MIN_SUCCESS_REQUESTS,
        "anchor_lead_s": ANCHOR_LEAD_S,
        "practical_floor_s": PRACTICAL_FLOOR_S,
        "H": envelope,
        "P": PRACTICAL_FLOOR_S,
        "pairs": pairs,
        "artifact_sha256": {str(path): artifact_hashes(Path(path))
                            for path in args.run_dir},
        "analyzer_sha256": sha256(Path(__file__)),
        "note": "exploratory endpoint development on archived runs; anchor eligibility amended 2026-09-06 (plateau-eligible first true-ready)",
    }
    write_json(Path(args.out), output)
    print(json.dumps({"H": envelope, "pairs": len(pairs),
                      "seeds": seeds}, indent=2))
    return 0


def bridge(args: argparse.Namespace) -> int:
    direct = analyze_run(Path(args.direct_run), include_qoe=True)
    discovery = analyze_run(Path(args.discovery_run), include_qoe=True)
    direct_summary = capacity_summary(Path(args.direct_run))
    discovery_summary = capacity_summary(Path(args.discovery_run))
    difference = discovery["run_delta_p95_s"] - direct["run_delta_p95_s"]
    lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
    envelope = float(lock.get("H", PRACTICAL_FLOOR_S))
    separation = []
    for lan in (1, 2):
        separation.append(discovery["anchors"][lan - 1]["ready_to_admit_s"]
                          - direct["anchors"][lan - 1]["ready_to_admit_s"])
    pooled_cpu = []
    for summary in (direct_summary, discovery_summary):
        values = [summary["pre_cpu"][lan]["median"] for lan in (1, 2)]
        pooled_cpu.append(sum(values) / len(values))
    separation_pass = (all(value > 0.0 for value in separation)
                       and (sum(separation) / len(separation)) >= BRIDGE_SEPARATION_MIN_S)
    cpu_pass = all(BRIDGE_CPU_LOW <= value <= BRIDGE_CPU_HIGH for value in pooled_cpu)
    mechanism_pass = (direct["event_fraction"] == 1.0
                      and discovery["event_fraction"] == 0.0
                      and direct["phase_start_guard"]
                      and discovery["phase_start_guard"]
                      and direct["run_status"] == "completed"
                      and discovery["run_status"] == "completed"
                      and all(summary["admissions_lan1"] >= 1
                              and summary["admissions_lan2"] >= 1
                              and not summary["scale_down_in_plateau"]
                              for summary in (direct_summary, discovery_summary)))
    result = {
        "direct": direct,
        "discovery": discovery,
        "bridge_delta_s": difference,
        "contextual_envelope_s": envelope,
        "separation_s": separation,
        "pooled_sub_max_cpu": pooled_cpu,
        "delta_pass": abs(difference) <= max(envelope, PRACTICAL_FLOOR_S),
        "separation_pass": separation_pass,
        "cpu_pass": cpu_pass,
        "mechanism_pass": mechanism_pass,
        "delta_is_gate": False,
        "pass": (separation_pass and cpu_pass and mechanism_pass),
    }
    write_json(Path(args.out), result)
    print(json.dumps({"bridge_delta_s": difference, "pass": result["pass"]}, indent=2))
    return 0 if result["pass"] else 2


def capacity_screen(args: argparse.Namespace) -> int:
    results = []
    for path in args.run_dir:
        result = capacity_summary(Path(path), args.quota)
        pre_values = [result["pre_cpu"][lan]["median"] for lan in (1, 2)]
        pre_p95 = [result["pre_cpu"][lan]["p95"] for lan in (1, 2)]
        relief = result["relief_pp"]
        baseline = result["baseline"]
        final = result["final"]
        result["gate_driver"] = (baseline["cancel_rate"] is not None
                                  and baseline["cancel_rate"] < 0.05
                                  and result["run_cancel_rate"] is not None
                                  and result["run_cancel_rate"] < 0.05)
        # Plan §6 specifies a single "canceled+dropped < 5%" requirement; both
        # the baseline-window and whole-run rates are checked defensively.
        result["gate_manipulation"] = (result["event_fraction"] == 1.0
                                        and result["ready_to_admit_median"] is not None
                                        and result["ready_to_admit_median"] <= 1.0)
        result["gate_baseline"] = (baseline["http000"] == 0
                                    and (baseline["timeout_rate"] or 0) <= 0.01
                                    and (baseline["failure_rate"] or 0) <= 0.01)
        result["gate_final"] = ((final["timeout_rate"] or 0) <= 0.01
                                 and (final["failure_rate"] or 0) <= 0.01)
        result["gate_cpu"] = (all(value is not None and 60.0 <= value <= 85.0
                                   for value in pre_values)
                              and all(value is not None and value <= 95.0
                                      for value in pre_p95))
        result["gate_relief"] = len(relief) == 2 and all(value >= 10.0 for value in relief)
        result["gate_mechanism"] = (result["admissions_lan1"] >= 1
                                     and result["admissions_lan2"] >= 1
                                     and result["first_wave_lan1"] >= 1
                                     and result["first_wave_lan2"] >= 1
                                     and result["first_wave_decisions_lan1"] >= 1
                                     and result["first_wave_decisions_lan2"] >= 1
                                     and result["phase_start_guard"]
                                     and not result["scale_down_in_plateau"]
                                     and result["run_status"] == "completed")
        result["gate_quota"] = result["quota_snapshot_ok"] is True
        result["qualifies"] = all(result[key] for key in (
            "gate_driver", "gate_baseline", "gate_final", "gate_cpu",
            "gate_relief", "gate_mechanism", "gate_manipulation", "gate_quota"))
        results.append(result)
    write_csv(Path(args.out), results)
    print(json.dumps({"runs": len(results), "qoe_metrics_emitted": False,
                      "qualifies": [row["qualifies"] for row in results]}, indent=2))
    return 0 if all(row["qualifies"] for row in results) else 2


def preflight(args: argparse.Namespace) -> int:
    lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
    records = [analyze_run(Path(path), include_qoe=True) for path in args.run_dir]
    name_to_path = {Path(path).name: Path(path) for path in args.run_dir}
    summaries = {name: capacity_summary(path) for name, path in name_to_path.items()}
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        key = label_key(record["run"])
        if key is not None:
            grouped.setdefault(key, {})[record["arm"]] = record
    pairs = []
    lan_contrasts = []
    parity = []
    manipulation_pass = True
    parity_pass = True
    for key in sorted(grouped):
        direct = grouped[key].get(DIRECT)
        discovery = grouped[key].get(DISCOVERY)
        if direct is None or discovery is None:
            continue
        pairs.append({"seed_key": key,
                      "D": discovery["run_delta_p95_s"] - direct["run_delta_p95_s"]})
        direct_summary = summaries[direct["run"]]
        discovery_summary = summaries[discovery["run"]]
        manipulation_pass &= (direct["run_status"] == "completed"
                              and discovery["run_status"] == "completed")
        for lan in (1, 2):
            direct_anchor = direct["anchors"][lan - 1]
            discovery_anchor = discovery["anchors"][lan - 1]
            manipulation_pass &= (direct["event_fraction"] == 1.0
                            and direct_anchor["ready_to_admit_s"] <= 1.0
                            and discovery["event_fraction"] == 0.0
                            and discovery_anchor["ready_to_admit_s"]
                            - direct_anchor["ready_to_admit_s"] >= 5.0)
            d = direct_anchor["delta_p95_s"]
            s = discovery_anchor["delta_p95_s"]
            lan_contrasts.append(s - d)
            offered_d = direct_summary["pre_offered"][lan]
            offered_s = discovery_summary["pre_offered"][lan]
            demand_ok = (offered_d > 0 and offered_s > 0
                         and 200 * abs(offered_d - offered_s) / (offered_d + offered_s) <= 2.0)
            cpu_d = direct_summary["pre_cpu"][lan]["median"]
            cpu_s = discovery_summary["pre_cpu"][lan]["median"]
            cpu_ok = cpu_d is not None and cpu_s is not None and abs(cpu_d - cpu_s) <= 10.0
            decision_d = direct_summary["first_decision"][lan]
            decision_s = discovery_summary["first_decision"][lan]
            decision_ok = (decision_d is not None and decision_s is not None
                           and abs(decision_d - decision_s) <= 10.0)
            add_ok = abs(direct_summary[f"admissions_lan{lan}"]
                         - discovery_summary[f"admissions_lan{lan}"]) <= 1
            entry = {"seed_key": key, "lan": lan,
                     "offered_direct": offered_d, "offered_discovery": offered_s,
                     "cpu_direct": cpu_d, "cpu_discovery": cpu_s,
                     "decision_direct": decision_d, "decision_discovery": decision_s,
                     "demand_ok": demand_ok, "cpu_ok": cpu_ok,
                     "decision_ok": decision_ok, "add_ok": add_ok}
            parity.append(entry)
            parity_pass &= demand_ok and cpu_ok and decision_ok and add_ok
    floor = float(lock.get("P", PRACTICAL_FLOOR_S))
    go = (len(pairs) == 2 and manipulation_pass and parity_pass
          and all(pair["D"] >= floor for pair in pairs)
          and sum(value > 0 for value in lan_contrasts) >= 3)
    output = {"P": floor, "H": lock.get("H"), "pairs": pairs,
              "lan_contrasts": lan_contrasts, "parity": parity,
              "manipulation_pass": manipulation_pass,
              "parity_pass": parity_pass,
              "go": go}
    write_json(Path(args.out), output)
    print(json.dumps(output, indent=2))
    return 0 if go else 2


def event_absence(args: argparse.Namespace) -> int:
    controls = {}
    for path in args.control_run or []:
        control = analyze_run(Path(path), include_qoe=True)
        key = label_key(control["run"])
        if key is not None:
            controls[key] = control
    results = []
    for path in args.run_dir:
        run_dir = Path(path)
        # §8 does not require the primary ±7 s window floors; tolerate thei
        # absence so a valid fallback-liveness run cannot be voided by them.
        # The fallback is recorded so "cost not measured" is distinguishable
        # from a measured null cost.
        qoe_fallback = False
        qoe_fallback_reason = None
        try:
            record = analyze_run(run_dir, include_qoe=True)
        except ValueError as exc:
            record = analyze_run(run_dir, include_qoe=False)
            qoe_fallback = True
            qoe_fallback_reason = str(exc)
        env = parse_env(run_dir / "controller_env_snapshot.env")
        admitted = admissions(run_dir)
        max_probe = as_float(env.get("READINESS_PROBE_MAX_S")) or 120.0
        timeout = as_float(env.get("READINESS_PROBE_TIMEOUT_S")) or 5.0
        liveness_budget = max_probe + timeout
        sources = {row.get("admit_source") for row in admitted}
        liveness = all(
            row["_admitted"] - row["_spawn_complete"] <= liveness_budget
            for row in admitted
        )
        observed_lag = [
            row["_admitted"] - max(row["_true_ready"], row["_spawn_complete"] +
                                    (as_float(env.get("READINESS_EVENT_FALLBACK_S")) or 20.0))
            for row in admitted if row["_true_ready"] is not None
        ]
        control = controls.get(label_key(record["run"]))
        results.append({"run": record["run"], "arm": record["arm"],
                        "event_enabled": env.get("EDGE_APP_READY_EVENT", "0"),
                        "admit_sources": sorted(sources),
                        "n_admitted": len(admitted),
                        "liveness_budget_s": liveness_budget,
                        "liveness_pass": liveness,
                        "qoe_fallback": qoe_fallback,
                        "qoe_fallback_reason": qoe_fallback_reason,
                        "fallback_lag_median_s": statistics.median(observed_lag)
                        if observed_lag else None,
                        "fallback_lag_max_s": max(observed_lag) if observed_lag else None})
        results[-1]["control_run"] = control["run"] if control else None
        results[-1]["control_delta_p95_s"] = control["run_delta_p95_s"] if control else None
        results[-1]["event_absence_cost_delta_s"] = (
            record["run_delta_p95_s"] - control["run_delta_p95_s"]
            if control and record["run_delta_p95_s"] is not None
            and control["run_delta_p95_s"] is not None else None
        )
    go = bool(results) and all(
        row["event_enabled"] == "0"
        and set(row["admit_sources"]) == {"probe_fallback"}
        and row["liveness_pass"] for row in results)
    write_json(Path(args.out), {"runs": results, "go": go})
    print(json.dumps({"runs": len(results), "go": go}, indent=2))
    return 0 if go else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    historical = sub.add_parser("historical-lock")
    historical.add_argument("--run-dir", nargs="+", required=True)
    historical.add_argument("--out", required=True)
    historical.set_defaults(func=historical_lock)
    screen = sub.add_parser("capacity-screen")
    screen.add_argument("--run-dir", nargs="+", required=True)
    screen.add_argument("--out", required=True)
    screen.add_argument("--quota", required=True, choices=("0.10", "0.12", "0.15"))
    screen.set_defaults(func=capacity_screen)
    campaign = sub.add_parser("preflight")
    campaign.add_argument("--run-dir", nargs="+", required=True)
    campaign.add_argument("--lock", required=True)
    campaign.add_argument("--out", required=True)
    campaign.set_defaults(func=preflight)
    bridge_parser = sub.add_parser("bridge")
    bridge_parser.add_argument("--direct-run", required=True)
    bridge_parser.add_argument("--discovery-run", required=True)
    bridge_parser.add_argument("--lock", required=True)
    bridge_parser.add_argument("--out", required=True)
    bridge_parser.set_defaults(func=bridge)
    absent = sub.add_parser("event-absence")
    absent.add_argument("--run-dir", nargs="+", required=True)
    absent.add_argument("--control-run", nargs="*", default=[])
    absent.add_argument("--out", required=True)
    absent.set_defaults(func=event_absence)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())