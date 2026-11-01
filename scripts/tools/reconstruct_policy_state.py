#!/usr/bin/env python3
"""
reconstruct_policy_state.py — Post-run policy state reconstruction.

Generates ``policy_state.csv`` from existing run artifacts WITHOUT requiring
any new controller-side publisher.  The reconstruction is anchored to the
10-second telemetry windows in ``resource_stats.csv`` and uses:

1. ``resource_stats.csv`` — canonical window grid + raw scale inputs
2. ``resource_stats_debug.csv`` — broad diagnosis fields when needed
3. ``controller_env_snapshot.env`` — exact thresholds, floors, spans, weights,
   cooldowns, and caps used in that run
4. ``per_node_stats.csv`` — dynamic compute and storage counts per window
5. ``container_events.csv`` — fallback for spawn/drain boundaries
6. ``elasticity_events.csv`` — controller log events parsed by
   parse_elasticity_logs.py
7. ``controller_lan1.log`` / ``controller_lan2.log`` — raw logs for annotation
   of busy skips, cooldown skips, trigger decisions, candidate selection, and
   no-candidate clears

Result: one row per LAN per telemetry window.

Usage:
    python3 reconstruct_policy_state.py \\
        --resource-stats       metrics/run/resource_stats.csv \\
        --resource-stats-debug metrics/run/resource_stats_debug.csv \\
        --per-node-stats       metrics/run/per_node_stats.csv \\
        --container-events     metrics/run/container_events.csv \\
        --controller-env       metrics/run/controller_env_snapshot.env \\
        --elasticity-events    metrics/run/elasticity_events.csv \\
        --controller-log-lan1  metrics/run/controller_lan1.log \\
        --controller-log-lan2  metrics/run/controller_lan2.log \\
        --output               metrics/run/policy_state.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

POLICY_FIELDNAMES = [
    "timestamp",
    "phase",
    "network_id",
    "window_end",
    "dynamic_compute_count",
    "dynamic_storage_count",
    "compute_score",
    "compute_base_threshold",
    "compute_effective_threshold",
    "compute_above_threshold",
    "compute_window_hits",
    "compute_window_size",
    "compute_scaleup_cooldown_remaining_s",
    "compute_scaledown_below_threshold",
    "compute_scaledown_hits",
    "compute_scaledown_armed",
    "compute_scaledown_cooldown_remaining_s",
    "storage_score",
    "storage_base_threshold",
    "storage_effective_threshold",
    "storage_above_threshold",
    "storage_window_hits",
    "storage_window_size",
    "storage_latency_signal_ms",
    "storage_scaleup_cooldown_remaining_s",
    "storage_scaledown_below_threshold",
    "storage_scaledown_hits",
    "storage_scaledown_armed",
    "storage_scaledown_cooldown_remaining_s",
    "elasticity_busy",
    "compute_blocked",
    "storage_blocked",
    "compute_triggered",
    "storage_triggered",
    "compute_candidate_selected",
    "storage_candidate_selected",
    "notes",
]



@dataclass
class PolicyWindow:
    """One reconstructed policy-state row for a single (LAN, window_end)."""
    timestamp: str = ""
    phase: str = ""
    network_id: str = ""
    window_end: float = 0.0

    dynamic_compute_count: Optional[int] = None
    dynamic_storage_count: Optional[int] = None

    compute_score: Optional[float] = None
    compute_base_threshold: Optional[float] = None
    compute_effective_threshold: Optional[float] = None
    compute_above_threshold: Optional[bool] = None
    compute_window_hits: Optional[int] = None
    compute_window_size: Optional[int] = None
    compute_scaleup_cooldown_remaining_s: Optional[float] = None

    compute_scaledown_below_threshold: Optional[bool] = None
    compute_scaledown_hits: Optional[int] = None
    compute_scaledown_armed: Optional[bool] = None
    compute_scaledown_cooldown_remaining_s: Optional[float] = None

    storage_score: Optional[float] = None
    storage_base_threshold: Optional[float] = None
    storage_effective_threshold: Optional[float] = None
    storage_above_threshold: Optional[bool] = None
    storage_window_hits: Optional[int] = None
    storage_window_size: Optional[int] = None
    storage_latency_signal_ms: Optional[float] = None
    storage_scaleup_cooldown_remaining_s: Optional[float] = None

    storage_scaledown_below_threshold: Optional[bool] = None
    storage_scaledown_hits: Optional[int] = None
    storage_scaledown_armed: Optional[bool] = None
    storage_scaledown_cooldown_remaining_s: Optional[float] = None

    elasticity_busy: Optional[bool] = None
    compute_blocked: Optional[bool] = None
    storage_blocked: Optional[bool] = None
    compute_triggered: Optional[bool] = None
    storage_triggered: Optional[bool] = None
    compute_candidate_selected: Optional[bool] = None
    storage_candidate_selected: Optional[bool] = None

    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "phase": self.phase,
            "network_id": self.network_id,
            "window_end": self.window_end,
            "dynamic_compute_count": _opt(self.dynamic_compute_count),
            "dynamic_storage_count": _opt(self.dynamic_storage_count),
            "compute_score": _opt(self.compute_score),
            "compute_base_threshold": _opt(self.compute_base_threshold),
            "compute_effective_threshold": _opt(self.compute_effective_threshold),
            "compute_above_threshold": _opt_bool(self.compute_above_threshold),
            "compute_window_hits": _opt(self.compute_window_hits),
            "compute_window_size": _opt(self.compute_window_size),
            "compute_scaleup_cooldown_remaining_s": _opt(self.compute_scaleup_cooldown_remaining_s),
            "compute_scaledown_below_threshold": _opt_bool(self.compute_scaledown_below_threshold),
            "compute_scaledown_hits": _opt(self.compute_scaledown_hits),
            "compute_scaledown_armed": _opt_bool(self.compute_scaledown_armed),
            "compute_scaledown_cooldown_remaining_s": _opt(self.compute_scaledown_cooldown_remaining_s),
            "storage_score": _opt(self.storage_score),
            "storage_base_threshold": _opt(self.storage_base_threshold),
            "storage_effective_threshold": _opt(self.storage_effective_threshold),
            "storage_above_threshold": _opt_bool(self.storage_above_threshold),
            "storage_window_hits": _opt(self.storage_window_hits),
            "storage_window_size": _opt(self.storage_window_size),
            "storage_latency_signal_ms": _opt(self.storage_latency_signal_ms),
            "storage_scaleup_cooldown_remaining_s": _opt(self.storage_scaleup_cooldown_remaining_s),
            "storage_scaledown_below_threshold": _opt_bool(self.storage_scaledown_below_threshold),
            "storage_scaledown_hits": _opt(self.storage_scaledown_hits),
            "storage_scaledown_armed": _opt_bool(self.storage_scaledown_armed),
            "storage_scaledown_cooldown_remaining_s": _opt(self.storage_scaledown_cooldown_remaining_s),
            "elasticity_busy": _opt_bool(self.elasticity_busy),
            "compute_blocked": _opt_bool(self.compute_blocked),
            "storage_blocked": _opt_bool(self.storage_blocked),
            "compute_triggered": _opt_bool(self.compute_triggered),
            "storage_triggered": _opt_bool(self.storage_triggered),
            "compute_candidate_selected": _opt_bool(self.compute_candidate_selected),
            "storage_candidate_selected": _opt_bool(self.storage_candidate_selected),
            "notes": "; ".join(self.notes) if self.notes else "",
        }


def _opt(v) -> str:
    if v is None:
        return ""
    return str(v)


def _opt_bool(v) -> str:
    if v is None:
        return ""
    return "true" if v else "false"




def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default




def load_controller_env(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file into a dict."""
    env: dict[str, str] = {}
    if not path.exists():
        print(f"WARNING: controller env not found: {path}", file=sys.stderr)
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(env.get(key, str(default))))
    except (ValueError, TypeError):
        return default




def _normalise(value: float, floor: float, span: float) -> float:
    """Clamp to [0, 1] using the configured floor and span."""
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - floor) / span))


def reconstruct_compute_score(
    cpu_pct: float,
    t_proc_ms: float,
    env: dict[str, str],
) -> float:
    """Reconstruct compute scale-up score from metrics + controller env."""
    w_cpu = _env_float(env, "SCALEUP_W_CPU", 0.40)
    w_t_proc = _env_float(env, "SCALEUP_W_T_PROC", 0.60)
    cpu_floor = _env_float(env, "SCALEUP_CPU_FLOOR", 4.5)
    cpu_span = _env_float(env, "SCALEUP_CPU_SPAN", 5)
    t_proc_floor = _env_float(env, "SCALEUP_T_PROC_FLOOR", 20)
    t_proc_span = _env_float(env, "SCALEUP_T_PROC_SPAN", 50)

    cpu_norm = _normalise(cpu_pct, cpu_floor, cpu_span)
    t_proc_norm = _normalise(t_proc_ms, t_proc_floor, t_proc_span)
    return w_cpu * cpu_norm + w_t_proc * t_proc_norm


def reconstruct_storage_score(
    storage_cpu_pct: float,
    t_db_ms: float,
    env: dict[str, str],
) -> float:
    """Reconstruct storage scale-up score from metrics + controller env."""
    w_st_cpu = _env_float(env, "SCALEUP_W_STORAGE_CPU", 0.60)
    w_t_db = _env_float(env, "SCALEUP_W_T_DB", 0.40)
    st_cpu_floor = _env_float(env, "SCALEUP_STORAGE_CPU_FLOOR", 3)
    st_cpu_span = _env_float(env, "SCALEUP_STORAGE_CPU_SPAN", 5)
    t_db_floor = _env_float(env, "SCALEUP_T_DB_FLOOR", 120)
    t_db_span = _env_float(env, "SCALEUP_T_DB_SPAN", 250)

    st_cpu_norm = _normalise(storage_cpu_pct, st_cpu_floor, st_cpu_span)
    t_db_norm = _normalise(t_db_ms, t_db_floor, t_db_span)
    return w_st_cpu * st_cpu_norm + w_t_db * t_db_norm


def compute_effective_threshold(
    base: float,
    increment: float,
    dyn_count: int,
    max_threshold: float,
) -> float:
    """Effective threshold = base + dyn_count × increment, capped."""
    return min(base + dyn_count * increment, max_threshold)


# Dynamic node counts from per_node_stats.csv


def count_dynamic_nodes(
    per_node_rows: list[dict],
    network_id: str,
    window_end: float,
) -> tuple[int, int]:
    """Count dynamic compute and storage nodes active in a given window.

    Dynamic nodes are those whose server_id starts with ``edge_server_``
    (compute) or ``edge_storage_`` (storage).  We count nodes whose
    ``window_end`` matches within a small tolerance.
    """
    compute = 0
    storage = 0
    for row in per_node_rows:
        nid = row.get("network_id", "")
        we = _safe_float(row.get("window_end", 0))
        role = row.get("role", "")
        sid = row.get("server_id", "")

        if nid != network_id:
            continue
        if abs(we - window_end) > 0.5:  # tolerance for float imprecision
            continue

        if role == "compute" and sid.startswith("edge_server_"):
            compute += 1
        elif role == "storage" and sid.startswith("edge_storage_"):
            storage += 1

    return compute, storage


# Container events fallback for dynamic counts


def count_dynamic_from_events(
    container_rows: list[dict],
    network_id: str,
    window_end: float,
) -> tuple[int, int]:
    """Fallback: count currently-alive dynamic containers near a window_end.

    Infer network_id from container name: edge_server_<lan>_<n> or
    edge_storage_<lan>_<n>.  A container is "alive" if spawned before
    window_end and not yet removed.
    """
    compute = 0
    storage = 0
    for row in container_rows:
        name = row.get("container_name", "")
        event = row.get("event", "")
        ts = _safe_float(row.get("timestamp", 0))

        if "_lan1_" in name or name.endswith("_1"):
            lan = "lan1"
        elif "_lan2_" in name or name.endswith("_2"):
            lan = "lan2"
        else:
            continue

        if lan != network_id.replace("lan", ""):
            # network_id is "lan1" or "lan2"; extract "1"/"2"
            target = network_id.replace("lan", "")
            if lan != target:
                continue

        if ts > window_end:
            continue

        if event == "start":
            if name.startswith("edge_server_"):
                compute += 1
            elif name.startswith("edge_storage_"):
                storage += 1
        elif event == "stop":
            if name.startswith("edge_server_"):
                compute = max(0, compute - 1)
            elif name.startswith("edge_storage_"):
                storage = max(0, storage - 1)

    return compute, storage


# Controller log annotation helpers (imported from parse_elasticity_logs.py)

from parse_elasticity_logs import parse_policy_annotations, PolicyAnnotation




def sliding_window_hits(
    scores: list[float],
    thresholds: list[float],
    window_size: int,
    required: int,
) -> list[int]:
    """Reconstruct sliding-window hit counts from score and threshold series."""
    hits: list[int] = []
    window: list[bool] = []
    for score, thresh in zip(scores, thresholds):
        window.append(score >= thresh)
        if len(window) > window_size:
            window = window[-window_size:]
        hits.append(sum(window))
    return hits




def reconstruct_scale_down(
    resource_rows: list[dict],
    env: dict[str, str],
) -> dict[str, list[dict]]:
    """Build per-LAN per-window scale-down predicate series."""
    tau_cpu_down = _env_float(env, "TAU_CPU_DOWN", 65.0)
    tau_proc_down = _env_float(env, "TAU_PROC_DOWN_MS", 5.0)
    tau_st_cpu_down = _env_float(env, "TAU_STORAGE_CPU_DOWN", 65.0)
    tau_db_down = _env_float(env, "TAU_DB_DOWN_MS", 100.0)
    proc_ceiling = _env_float(env, "SCALE_DOWN_PROC_TIMEOUT_CEILING_MS", 5000.0)
    db_ceiling = _env_float(env, "SCALE_DOWN_DB_TIMEOUT_CEILING_MS", 5000.0)
    comp_window = _env_int(env, "SCALE_DOWN_COMPUTE_WINDOW_SIZE", 5)
    stor_window = _env_int(env, "SCALE_DOWN_STORAGE_WINDOW_SIZE", 15)
    comp_required = _env_int(env, "SCALE_DOWN_COMPUTE_REQUIRED", 3)
    stor_required = _env_int(env, "SCALE_DOWN_STORAGE_REQUIRED", 9)

    result: dict[str, list[dict]] = defaultdict(list)
    comp_deque: list[bool] = []
    stor_deque: list[bool] = []

    for row in resource_rows:
        nid = row.get("network_id", "")
        cpu = _safe_float(row.get("average_cpu_percent", row.get("median_cpu_percent", 0)))
        proc = _safe_float(row.get("avg_time_proc_ms", row.get("median_time_proc_ms", 0)))
        st_cpu = _safe_float(row.get("avg_storage_cpu_percent", row.get("median_storage_cpu_percent", 0)))
        tdb = _safe_float(row.get("avg_time_db_ms", row.get("median_time_db_ms", 0)))

        comp_ceiling_skip = proc > proc_ceiling
        stor_ceiling_skip = tdb > db_ceiling

        comp_below = (not comp_ceiling_skip and cpu < tau_cpu_down and proc < tau_proc_down)
        stor_below = (not stor_ceiling_skip and st_cpu < tau_st_cpu_down and tdb < tau_db_down)

        if not comp_ceiling_skip:
            comp_deque.append(comp_below)
        if len(comp_deque) > comp_window:
            comp_deque = comp_deque[-comp_window:]
        comp_hits = sum(comp_deque)

        if not stor_ceiling_skip:
            stor_deque.append(stor_below)
        if len(stor_deque) > stor_window:
            stor_deque = stor_deque[-stor_window:]
        stor_hits = sum(stor_deque)

        result[nid].append({
            "window_end": _safe_float(row.get("window_end", 0)),
            "compute_below": comp_below,
            "compute_hits": comp_hits,
            "compute_armed": comp_hits >= comp_required and len(comp_deque) >= comp_required,
            "storage_below": stor_below,
            "storage_hits": stor_hits,
            "storage_armed": stor_hits >= stor_required and len(stor_deque) >= stor_required,
        })

    return dict(result)




def reconstruct_policy_state(
    resource_rows: list[dict],
    debug_rows: list[dict],
    per_node_rows: list[dict],
    container_event_rows: list[dict],
    elasticity_event_rows: list[dict],
    controller_logs: dict[str, Path],
    controller_env: dict[str, str],
) -> list[PolicyWindow]:
    """Build one policy-state row per LAN per telemetry window."""

    if not resource_rows:
        print("WARNING: resource_stats.csv is empty — nothing to reconstruct",
              file=sys.stderr)
        return []

    comp_base = _env_float(controller_env, "SCALEUP_COMPUTE_BASE_THRESHOLD", 0.25)
    comp_inc = _env_float(controller_env, "SCALEUP_COMPUTE_THRESHOLD_INCREMENT", 0.10)
    comp_max = _env_float(controller_env, "SCALEUP_COMPUTE_MAX_THRESHOLD", 0.85)
    comp_cooldown = _env_float(controller_env, "SCALEUP_COMPUTE_COOLDOWN_S", 45)
    stor_base = _env_float(controller_env, "SCALEUP_STORAGE_BASE_THRESHOLD", 0.30)
    stor_inc = _env_float(controller_env, "SCALEUP_STORAGE_THRESHOLD_INCREMENT", 0.10)
    stor_max = _env_float(controller_env, "SCALEUP_STORAGE_MAX_THRESHOLD", 0.55)
    stor_cooldown = _env_float(controller_env, "SCALEUP_STORAGE_COOLDOWN_S", 45)
    comp_sw_size = _env_int(controller_env, "SCALEUP_WINDOW_SIZE", 5)
    comp_sw_required = _env_int(controller_env, "SCALEUP_REQUIRED", 3)
    stor_sw_size = _env_int(controller_env, "SCALEUP_STORAGE_WINDOW_SIZE", 5)
    stor_sw_required = _env_int(controller_env, "SCALEUP_STORAGE_REQUIRED", 2)
    comp_sd_cooldown = _env_float(controller_env, "SCALEDOWN_COMPUTE_COOLDOWN_S", 40)
    stor_sd_cooldown = _env_float(controller_env, "SCALEDOWN_STORAGE_COOLDOWN_S", 120)

    max_dyn_comp = _env_int(controller_env, "MAX_DYNAMIC_COMPUTE", 2)
    max_dyn_stor = _env_int(controller_env, "MAX_DYNAMIC_STORAGE", 5)

    sd_pred = reconstruct_scale_down(resource_rows, controller_env)

    all_annotations: dict[str, list[PolicyAnnotation]] = {}
    for lan_name, log_path in controller_logs.items():
        all_annotations[lan_name] = parse_policy_annotations(log_path, lan_name)

    el_events_by_lan: dict[str, list[dict]] = defaultdict(list)
    for ev in elasticity_event_rows:
        ctrl = ev.get("controller", "")
        if ctrl in ("lan1", "lan2"):
            el_events_by_lan[ctrl].append(ev)

    lan_rows: dict[str, list[dict]] = defaultdict(list)
    for row in resource_rows:
        nid = row.get("network_id", "")
        if nid in ("lan1", "lan2"):
            lan_rows[nid].append(row)

    lan_scores: dict[str, list[float]] = {}
    lan_thresholds: dict[str, list[float]] = {}
    for nid, rows in lan_rows.items():
        comp_scores = []
        comp_thresholds = []
        stor_scores = []
        stor_thresholds = []
        for row in rows:
            cpu = _safe_float(row.get("average_cpu_percent", row.get("median_cpu_percent", 0)))
            proc = _safe_float(row.get("avg_time_proc_ms", row.get("median_time_proc_ms", 0)))
            st_cpu = _safe_float(row.get("avg_storage_cpu_percent", row.get("median_storage_cpu_percent", 0)))
            tdb = _safe_float(row.get("avg_time_db_ms", row.get("median_time_db_ms", 0)))

            cs = reconstruct_compute_score(cpu, proc, controller_env)
            ss = reconstruct_storage_score(st_cpu, tdb, controller_env)

            dyn_comp, dyn_stor = count_dynamic_nodes(
                per_node_rows, nid, _safe_float(row.get("window_end", 0)))
            if dyn_comp == 0 and dyn_stor == 0:
                we = _safe_float(row.get("window_end", 0))
                dyn_comp, dyn_stor = count_dynamic_from_events(
                    container_event_rows, nid, we)

            ct = compute_effective_threshold(comp_base, comp_inc, dyn_comp, comp_max)
            st = compute_effective_threshold(stor_base, stor_inc, dyn_stor, stor_max)

            comp_scores.append(cs)
            comp_thresholds.append(ct)
            stor_scores.append(ss)
            stor_thresholds.append(st)

        lan_scores[nid + "_compute"] = comp_scores
        lan_scores[nid + "_storage"] = stor_scores
        lan_thresholds[nid + "_compute"] = comp_thresholds
        lan_thresholds[nid + "_storage"] = stor_thresholds

    lan_hits: dict[str, list[int]] = {}
    for nid in ("lan1", "lan2"):
        lan_hits[nid + "_compute"] = sliding_window_hits(
            lan_scores.get(nid + "_compute", []),
            lan_thresholds.get(nid + "_compute", []),
            comp_sw_size, comp_sw_required,
        )
        lan_hits[nid + "_storage"] = sliding_window_hits(
            lan_scores.get(nid + "_storage", []),
            lan_thresholds.get(nid + "_storage", []),
            stor_sw_size, stor_sw_required,
        )

    windows: list[PolicyWindow] = []

    for row in resource_rows:
        nid = row.get("network_id", "")
        if nid not in ("lan1", "lan2"):
            continue

        we = _safe_float(row.get("window_end", 0))
        phase = row.get("phase", "")
        ts = row.get("timestamp", "")

        pw = PolicyWindow(
            timestamp=ts,
            phase=phase,
            network_id=nid,
            window_end=we,
        )

        # Dynamic counts from per_node_stats (primary) or container_events (fallback)
        dyn_comp, dyn_stor = count_dynamic_nodes(per_node_rows, nid, we)
        if dyn_comp == 0 and dyn_stor == 0:
            dyn_comp2, dyn_stor2 = count_dynamic_from_events(
                container_event_rows, nid, we)
            if dyn_comp == 0:
                dyn_comp = dyn_comp2
            if dyn_stor == 0:
                dyn_stor = dyn_stor2
        pw.dynamic_compute_count = dyn_comp
        pw.dynamic_storage_count = dyn_stor

        cpu = _safe_float(row.get("average_cpu_percent", row.get("median_cpu_percent", 0)))
        proc = _safe_float(row.get("avg_time_proc_ms", row.get("median_time_proc_ms", 0)))
        st_cpu = _safe_float(row.get("avg_storage_cpu_percent", row.get("median_storage_cpu_percent", 0)))
        tdb = _safe_float(row.get("avg_time_db_ms", row.get("median_time_db_ms", 0)))

        cs = reconstruct_compute_score(cpu, proc, controller_env)
        ct = compute_effective_threshold(comp_base, comp_inc, dyn_comp, comp_max)
        pw.compute_score = cs
        pw.compute_base_threshold = comp_base
        pw.compute_effective_threshold = ct
        pw.compute_above_threshold = cs >= ct

        ss = reconstruct_storage_score(st_cpu, tdb, controller_env)
        st = compute_effective_threshold(stor_base, stor_inc, dyn_stor, stor_max)
        pw.storage_score = ss
        pw.storage_base_threshold = stor_base
        pw.storage_effective_threshold = st
        pw.storage_above_threshold = ss >= st
        pw.storage_latency_signal_ms = _safe_float(
            row.get("storage_latency_signal_ms", row.get("median_time_db_ms", 0)))

        sd_list = sd_pred.get(nid, [])
        sd_idx = None
        for i, sd in enumerate(sd_list):
            if abs(sd["window_end"] - we) < 0.5:
                sd_idx = i
                break
        if sd_idx is not None:
            sd = sd_list[sd_idx]
            pw.compute_scaledown_below_threshold = sd["compute_below"]
            pw.compute_scaledown_hits = sd["compute_hits"]
            pw.compute_scaledown_armed = sd["compute_armed"]
            pw.storage_scaledown_below_threshold = sd["storage_below"]
            pw.storage_scaledown_hits = sd["storage_hits"]
            pw.storage_scaledown_armed = sd["storage_armed"]

        _apply_annotations(pw, all_annotations.get(nid, []), we)

        windows.append(pw)

    for nid in ("lan1", "lan2"):
        lan_win = [w for w in windows if w.network_id == nid]
        comp_hit_series = lan_hits.get(nid + "_compute", [])
        stor_hit_series = lan_hits.get(nid + "_storage", [])
        for i, w in enumerate(lan_win):
            if i < len(comp_hit_series):
                w.compute_window_hits = comp_hit_series[i]
                w.compute_window_size = comp_sw_size
            if i < len(stor_hit_series):
                w.storage_window_hits = stor_hit_series[i]
                w.storage_window_size = stor_sw_size

    return windows


def _apply_annotations(
    pw: PolicyWindow,
    annotations: list[PolicyAnnotation],
    window_end: float,
    window_span: float = 10.0,
) -> None:
    """Map log annotations that fall inside this window onto the PolicyWindow."""
    window_start = window_end - window_span

    for ann in annotations:
        if ann.ts < window_start or ann.ts > window_end:
            continue

        if ann.kind == "busy":
            pw.elasticity_busy = True
        elif ann.kind == "cooldown_compute":
            pw.compute_scaledown_cooldown_remaining_s = _safe_float(ann.detail.replace("s", ""))
        elif ann.kind == "cooldown_storage":
            pw.storage_scaledown_cooldown_remaining_s = _safe_float(ann.detail.replace("s", ""))
        elif ann.kind == "blocked_compute":
            pw.compute_blocked = True
        elif ann.kind == "blocked_storage":
            pw.storage_blocked = True
        elif ann.kind == "triggered_compute":
            pw.compute_triggered = True
        elif ann.kind == "triggered_storage":
            pw.storage_triggered = True
        elif ann.kind == "candidate_compute":
            pw.compute_candidate_selected = True
        elif ann.kind == "candidate_storage":
            pw.storage_candidate_selected = True
        elif ann.kind == "scaleup_cooldown_compute":
            pw.compute_scaleup_cooldown_remaining_s = _safe_float(ann.detail.replace("s", ""))
        elif ann.kind == "scaleup_cooldown_storage":
            pw.storage_scaleup_cooldown_remaining_s = _safe_float(ann.detail.replace("s", ""))
        elif ann.kind == "no_candidate_compute":
            pw.compute_candidate_selected = False
            pw.notes.append("compute: no candidate")
        elif ann.kind == "no_candidate_storage":
            pw.storage_candidate_selected = False
            pw.notes.append("storage: no candidate")




def main():
    parser = argparse.ArgumentParser(
        description="Reconstruct policy_state.csv from run artifacts"
    )
    parser.add_argument("--resource-stats", required=True, type=Path,
                        help="Trimmed resource_stats.csv")
    parser.add_argument("--resource-stats-debug", type=Path,
                        help="Broad resource_stats_debug.csv (optional)")
    parser.add_argument("--per-node-stats", type=Path,
                        help="per_node_stats.csv (optional)")
    parser.add_argument("--container-events", type=Path,
                        help="container_events.csv (optional)")
    parser.add_argument("--controller-env", required=True, type=Path,
                        help="controller_env_snapshot.env")
    parser.add_argument("--elasticity-events", type=Path,
                        help="elasticity_events.csv (optional)")
    parser.add_argument("--controller-log-lan1", type=Path,
                        help="Controller log for LAN1")
    parser.add_argument("--controller-log-lan2", type=Path,
                        help="Controller log for LAN2")
    parser.add_argument("--output", required=True, type=Path,
                        help="Output policy_state.csv path")
    args = parser.parse_args()

    resource_rows = _read_csv(args.resource_stats)
    if not resource_rows:
        print(f"ERROR: resource_stats.csv is empty or missing: {args.resource_stats}",
              file=sys.stderr)
        sys.exit(1)

    debug_rows = _read_csv(args.resource_stats_debug) if args.resource_stats_debug else []
    per_node_rows = _read_csv(args.per_node_stats) if args.per_node_stats else []
    container_rows = _read_csv(args.container_events) if args.container_events else []
    el_events = _read_csv(args.elasticity_events) if args.elasticity_events else []
    env = load_controller_env(args.controller_env)

    controller_logs: dict[str, Path] = {}
    if args.controller_log_lan1:
        controller_logs["lan1"] = args.controller_log_lan1
    if args.controller_log_lan2:
        controller_logs["lan2"] = args.controller_log_lan2

    windows = reconstruct_policy_state(
        resource_rows=resource_rows,
        debug_rows=debug_rows,
        per_node_rows=per_node_rows,
        container_event_rows=container_rows,
        elasticity_event_rows=el_events,
        controller_logs=controller_logs,
        controller_env=env,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=POLICY_FIELDNAMES)
        writer.writeheader()
        for w in windows:
            writer.writerow(w.to_row())

    n_lan1 = sum(1 for w in windows if w.network_id == "lan1")
    n_lan2 = sum(1 for w in windows if w.network_id == "lan2")
    print(f"Wrote {len(windows)} policy-state rows "
          f"(lan1={n_lan1}, lan2={n_lan2}) → {args.output}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
