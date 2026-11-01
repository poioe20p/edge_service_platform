#!/usr/bin/env python3
"""RQ2 bottleneck-aware campaign: consolidated dataset + cross-cell comparison graphs.

Consumes the 18-run RQ2 campaign (3 policies x 2 episodes x 3 replicates) and
produces:

  1. <out>/campaign_dataset.csv   — one row per run, all derived metrics.
  2. <graphs_dir>/*.png           — the 15-graph cross-cell comparison suite
                                    defined in analysis_focus.md §5.

Two input modes:
  * folder mode (default): reads run folders under --metrics-dir plus the
    per-run analyzer rollups in --vm-per-run (bottleneck_validation,
    decision_analysis, relief_analysis, node_minutes) and recomputes the
    dataset. Requires the full run folders (latency/resource summaries +
    spawn metrics) — use it when the run folders are available.
  * dataset mode (--from-dataset): loads the committed campaign_dataset.csv
    (the retained evaluation record) and regenerates the graphs only. This is
    the recommended mode now that run folders are archived on the VM only.

Styling follows the RQ1 comparison graphs (rq1_delivery_comparison.py):
  - one colour per policy arm from the RQ1 palette
    (#4C72B0 cf / #DD8452 sf / #55A868 ba)
  - bars alpha 0.85, black error bars (lw=1, capsize=3)
  - per-replicate scatter dots in black (s=16, alpha=0.7), jittered
  - dotted y-grid (alpha 0.4), spines retained
  - two related series use the same arm colour at alpha 0.85 / 0.45
  - figsize (8,5) single / (12,5) double-panel, dpi 150

Usage:
    python -m source.scripts.testing.analysis.rq2.rq2_bottleneck_aware_campaign \
        --from-dataset docs/operation/testing/experiment/v2/rq2/analysis/campaign_dataset.csv \
        --graphs-dir docs/operation/testing/experiment/v2/rq2/graphs/comparison
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from ..client_status import is_completed, is_failure, is_timeout

# ---------------------------------------------------------------------------
# Constants / conventions
# ---------------------------------------------------------------------------

RUN_RE = re.compile(r"(\d{8}_\d{6})_rq2_(cf|sf|ba)_(cb|db)_([1-6])$")

POLICIES = ["cf", "sf", "ba"]
EPISODES = ["cb", "db"]
CELLS = [f"{p}_{e}" for e in EPISODES for p in POLICIES]  # cb first, then db

# RQ1 palette: one colour per policy arm (mirrors A/B/C arm colours in rq1).
POLICY_COLOR = {"cf": "#4C72B0", "sf": "#DD8452", "ba": "#55A868"}
POLICY_LABEL = {"cf": "fixed_compute_first", "sf": "fixed_storage_first",
                "ba": "bottleneck_aware"}
EPISODE_PHASE = {"cb": "compute_bound_episode", "db": "data_bound_episode"}

_EPISODE_SUBSTR = "episode"

# --- RQ1 styling constants ---
BAR_ALPHA = 0.85          # primary series
BAR_ALPHA_2 = 0.45        # secondary related series (RQ1 delivery_completeness)
ERROR_KW = dict(ecolor="black", lw=1, capsize=3)
DOT_COLOR = "black"
DOT_SIZE = 16
DOT_ALPHA = 0.7
GRID_KW = dict(axis="y", linestyle=":", alpha=0.4)
FIG_SINGLE = (10, 5.2)
FIG_DOUBLE = (15, 5.2)
DPI = 150
RIGHT_RESERVE = 0.74      # right margin reserved for outside legends
TOP_RESERVE = 0.86        # top margin reserved for two-panel legends


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_bottleneck(path: Path) -> dict:
    """bottleneck_validation.txt -> per-lan median signals + verdicts."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(",")
        if len(parts) == 8 and parts[0] in ("1", "2") and parts[1] in ("compute_bound", "data_bound"):
            out[f"lan{parts[0]}"] = {
                "proc_ms": float(parts[3]),
                "cpu_pct": float(parts[4]),
                "db_ms": float(parts[5]),
                "storage_cpu_pct": float(parts[6]),
                "verdict": parts[7].strip(),
            }
    return out


def parse_decision(path: Path) -> dict:
    """decision_analysis.txt -> rollups (action counts, budget, agreement, T9.8)."""
    out: dict = {"n_rows": None, "compute": None, "storage": None,
                 "agree_num": None, "agree_den": None, "t98": None,
                 "budget": {"lan1": {"c": 0, "s": 0}, "lan2": {"c": 0, "s": 0}}}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("scale_up rows"):
            m = re.search(r":\s*(\d+)", s)
            if m:
                out["n_rows"] = int(m.group(1))
        elif s.startswith("action counts"):
            m = re.search(r"compute=(\d+)\s+storage=(\d+)", s)
            if m:
                out["compute"], out["storage"] = int(m.group(1)), int(m.group(2))
        elif s.startswith("budget used per LAN"):
            for m in re.finditer(r"lan(\d) c=(\d+) s=(\d+)", s):
                out["budget"][f"lan{m.group(1)}"] = {"c": int(m.group(2)), "s": int(m.group(3))}
        elif s.startswith("classifier-vs-episode agree"):
            m = re.search(r"(\d+)/(\d+)", s)
            if m:
                out["agree_num"], out["agree_den"] = int(m.group(1)), int(m.group(2))
        elif s.startswith("T9.8"):
            out["t98"] = "OK" in s
    return out


def parse_relief(path: Path) -> dict:
    out: dict = {"actions": None, "recovered_num": None, "recovered_den": None,
                 "median_recovery_s": None}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("actions"):
            m = re.search(r":\s*(\d+)", s)
            if m:
                out["actions"] = int(m.group(1))
        elif s.startswith("recovered in-tier"):
            m = re.search(r"(\d+)/(\d+)", s)
            if m:
                out["recovered_num"], out["recovered_den"] = int(m.group(1)), int(m.group(2))
        elif s.startswith("median recovery_s"):
            m = re.search(r":\s*([\d.]+)", s)
            if m:
                out["median_recovery_s"] = float(m.group(1))
    return out


def parse_node_minutes(path: Path) -> dict:
    out: dict = {"compute_nm": None, "storage_nm": None,
                 "per1000_compute": None, "per1000_storage": None,
                 "recorded_requests": None}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("compute node-minutes"):
            m = re.search(r":\s*([\d.]+)", s)
            if m:
                out["compute_nm"] = float(m.group(1))
        elif s.startswith("storage node-minutes"):
            m = re.search(r":\s*([\d.]+)", s)
            if m:
                out["storage_nm"] = float(m.group(1))
        elif s.startswith("per 1000 recorded req"):
            m = re.search(r"compute=([\d.]+)\s+storage=([\d.]+)", s)
            if m:
                out["per1000_compute"] = float(m.group(1))
                out["per1000_storage"] = float(m.group(2))
            m2 = re.search(r"recorded_requests=(\d+)", s)
            if m2:
                out["recorded_requests"] = int(m2.group(1))
    return out


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _opt_float(v) -> float | None:
    """Blank/empty cells are missing values, not zero."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _load_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def episode_info(run_dir: Path, min_start: float) -> tuple[str | None, float, float]:
    """Episode label + [start, end] window-end boundaries (replicates the analyzer)."""
    snap_path = run_dir / "phases_snapshot.json"
    if not snap_path.exists():
        return None, 0.0, 0.0
    try:
        phases = json.loads(snap_path.read_text(encoding="utf-8")).get("phases", [])
    except (json.JSONDecodeError, OSError):
        return None, 0.0, 0.0
    episode = next((ph for ph in phases if _EPISODE_SUBSTR in ph.get("name", "")), None)
    if episode is None:
        return None, 0.0, 0.0
    elapsed = 0.0
    for ph in phases:
        if ph is episode:
            break
        elapsed += float(ph.get("duration_s", 0))
    start = min_start + elapsed
    end = start + float(episode.get("duration_s", 0))
    label = "compute_bound" if "compute" in episode.get("name", "") else "data_bound"
    return label, start, end


def episode_action_distribution(run_dir: Path) -> tuple[Counter, Counter]:
    """Count selected_action and reason over episode windows (both LANs)."""
    actions: Counter = Counter()
    reasons: Counter = Counter()
    min_start = float("inf")
    wends: dict[tuple[str, str], float] = {}
    for lan in (1, 2):
        for w in _load_jsonl(run_dir / f"window_log_lan{lan}.jsonl"):
            we = _as_float(w.get("window_end"))
            if we > 0:
                wends[(lan, w.get("window_id", ""))] = we
                min_start = min(min_start, we)
    if min_start == float("inf"):
        return actions, reasons
    label, estart, eend = episode_info(run_dir, min_start)
    if label is None:
        return actions, reasons
    for lan in (1, 2):
        path = run_dir / f"decision_log_lan{lan}.csv"
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                if row.get("action_type") != "scale_up":
                    continue
                we = wends.get((lan, row.get("window_id", "")), 0.0)
                if not (estart <= we <= eend):
                    continue
                sel = (row.get("selected_action") or "").strip() or "none"
                actions[sel] += 1
                reasons[(row.get("reason") or "").strip() or "none"] += 1
    return actions, reasons


def read_spawn_metrics(run_dir: Path) -> dict:
    """analysis/rq2_spawn_metrics.csv -> per-tier TTFT/TFR value lists."""
    out: dict = {"compute_ttft": [], "storage_ttft": [], "compute_tfr": []}
    path = run_dir / "analysis" / "rq2_spawn_metrics.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            tier = row.get("tier", "")
            ttft = _as_float(row.get("ttft_s"), -1.0)
            tfr = _as_float(row.get("tfr_s"), -1.0)
            if tier == "compute":
                if ttft >= 0:
                    out["compute_ttft"].append(ttft)
                if tfr >= 0:
                    out["compute_tfr"].append(tfr)
            elif tier == "storage" and ttft >= 0:
                out["storage_ttft"].append(ttft)
    return out


def read_latency_summary(run_dir: Path, episode_phase: str) -> dict:
    out: dict = {"p50": None, "p95": None, "p99": None}
    path = run_dir / "latency_summary.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if row.get("phase") == episode_phase and row.get("scenario") == "aggregate":
                out = {"p50": _opt_float(row.get("median")),
                       "p95": _opt_float(row.get("p95")),
                       "p99": _opt_float(row.get("p99"))}
    return out


def read_resource_summary(run_dir: Path, episode_phase: str) -> dict:
    out: dict = {"server_mean": None, "server_max": None,
                 "storage_mean": None, "storage_max": None}
    path = run_dir / "resource_summary.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if row.get("phase") != episode_phase:
                continue
            metric = row.get("metric", "")
            if metric == "server_count":
                out["server_mean"] = _opt_float(row.get("mean"))
                out["server_max"] = _opt_float(row.get("max"))
            elif metric == "storage_count":
                out["storage_mean"] = _opt_float(row.get("mean"))
                out["storage_max"] = _opt_float(row.get("max"))
    return out


def read_failure_rate(run_dir: Path, episode_phase: str) -> dict:
    """Episode-phase status-aware rates (v2 contract, analysis_focus.md §7.1).

    - timeout_rate = ``status=timeout`` / offered (primary degradation stat)
    - failure_rate = completed & ``http_status`` not in ("200","") / completed
    - dropped/canceled are counted in offered but excluded from latency/failure
    """
    out: dict = {"episode_pct": None, "lan1_pct": None, "lan2_pct": None,
                 "timeout_pct": None, "offered": 0, "completed": 0,
                 "timeout": 0}
    path = run_dir / "client_requests.csv"
    if not path.exists():
        return out
    totals: Counter = Counter()
    completed: Counter = Counter()
    fails: Counter = Counter()
    timeouts: Counter = Counter()
    with open(path, encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        for row in reader:
            if row.get("phase") != episode_phase:
                continue
            lan = row.get("client_lan", "")
            totals["all"] += 1
            totals[lan] += 1
            if is_completed(row, header):
                completed["all"] += 1
                completed[lan] += 1
                if is_failure(row, header):
                    fails["all"] += 1
                    fails[lan] += 1
            elif is_timeout(row, header):
                timeouts["all"] += 1
                timeouts[lan] += 1
    if totals["all"]:
        out["episode_pct"] = (100.0 * fails["all"] / completed["all"]
                               if completed["all"] else None)
        out["timeout_pct"] = 100.0 * timeouts["all"] / totals["all"]
        out["offered"] = totals["all"]
        out["completed"] = completed["all"]
        out["timeout"] = timeouts["all"]
    if totals["lan1"] and completed["lan1"]:
        out["lan1_pct"] = 100.0 * fails["lan1"] / completed["lan1"]
    if totals["lan2"] and completed["lan2"]:
        out["lan2_pct"] = 100.0 * fails["lan2"] / completed["lan2"]
    return out


# ---------------------------------------------------------------------------
# Run loading — folder mode
# ---------------------------------------------------------------------------

def discover_runs(metrics_dir: Path) -> list[Path]:
    runs = []
    for d in metrics_dir.iterdir():
        if d.is_dir() and RUN_RE.match(d.name):
            runs.append(d)
    return sorted(runs)


def load_run(run_dir: Path, vm_per_run: Path) -> dict:
    m = RUN_RE.match(run_dir.name)
    ts, policy, episode, repl = m.groups()
    base = vm_per_run / f"{run_dir.name}"
    bv = parse_bottleneck(Path(str(base) + "_bottleneck_validation.txt"))
    dec = parse_decision(Path(str(base) + "_decision_analysis.txt"))
    relief = parse_relief(Path(str(base) + "_relief_analysis.txt"))
    nm = parse_node_minutes(Path(str(base) + "_node_minutes.txt"))
    spawn = read_spawn_metrics(run_dir)
    episode_phase = EPISODE_PHASE[episode]
    lat = read_latency_summary(run_dir, episode_phase)
    res = read_resource_summary(run_dir, episode_phase)
    fail = read_failure_rate(run_dir, episode_phase)
    acts, reasons = episode_action_distribution(run_dir)

    verdicts = [v["verdict"] for v in bv.values()]
    g2 = all(v == "PASS" for v in verdicts) if verdicts else None

    return {
        "run_id": run_dir.name,
        "ts": ts,
        "label": f"rq2_{policy}_{episode}_{repl}",
        "policy": policy,
        "episode": episode,
        "replicate": int(repl),
        "cell": f"{policy}_{episode}",
        "g2": g2,
        "bv": bv,
        "dec": dec,
        "relief": relief,
        "nm": nm,
        "spawn": spawn,
        "lat": lat,
        "res": res,
        "fail": fail,
        "sel_actions": dict(acts),
        "sel_reasons": dict(reasons),
    }


def load_run_from_dataset(row: dict) -> dict:
    """Build a run dict from a campaign_dataset.csv row (graph-only mode)."""
    cell = row["cell"]
    policy, episode = cell.split("_")
    g2s = str(row.get("g2") or "").strip().lower()
    return {
        "run_id": row["run_id"],
        "label": row["label"],
        "policy": policy,
        "episode": episode,
        "replicate": _opt_int(row["replicate"]) or 0,
        "cell": cell,
        "g2": g2s == "true" if g2s else None,
        "bv": {
            "lan1": {"proc_ms": _opt_float(row.get("bv_lan1_proc_ms")),
                     "cpu_pct": _opt_float(row.get("bv_lan1_cpu_pct")),
                     "db_ms": _opt_float(row.get("bv_lan1_db_ms")),
                     "storage_cpu_pct": _opt_float(row.get("bv_lan1_storage_cpu_pct")),
                     "verdict": (row.get("bv_lan1_verdict") or "").strip()},
            "lan2": {"proc_ms": _opt_float(row.get("bv_lan2_proc_ms")),
                     "cpu_pct": _opt_float(row.get("bv_lan2_cpu_pct")),
                     "db_ms": _opt_float(row.get("bv_lan2_db_ms")),
                     "storage_cpu_pct": _opt_float(row.get("bv_lan2_storage_cpu_pct")),
                     "verdict": (row.get("bv_lan2_verdict") or "").strip()},
        },
        "dec": {
            "n_rows": _opt_int(row.get("dec_n_rows")),
            "compute": _opt_int(row.get("dec_compute_actions")),
            "storage": _opt_int(row.get("dec_storage_actions")),
            "budget": {"lan1": {"c": _opt_int(row.get("budget_lan1_c")) or 0,
                                "s": _opt_int(row.get("budget_lan1_s")) or 0},
                       "lan2": {"c": _opt_int(row.get("budget_lan2_c")) or 0,
                                "s": _opt_int(row.get("budget_lan2_s")) or 0}},
            "agree_num": _opt_int(row.get("agree_num")),
            "agree_den": _opt_int(row.get("agree_den")),
            "t98": str(row.get("t98_ok") or "").strip().lower() in ("true", "1", "ok"),
        },
        "relief": {"actions": _opt_int(row.get("relief_actions")),
                   "recovered_num": _opt_int(row.get("relief_recovered_num")),
                   "recovered_den": _opt_int(row.get("relief_recovered_den")),
                   "median_recovery_s": _opt_float(row.get("relief_recovery_median_s"))},
        "nm": {"compute_nm": _opt_float(row.get("nm_compute")),
               "storage_nm": _opt_float(row.get("nm_storage")),
               "per1000_compute": _opt_float(row.get("nm_per1000_compute")),
               "per1000_storage": _opt_float(row.get("nm_per1000_storage")),
               "recorded_requests": _opt_int(row.get("nm_recorded_requests"))},
        # Dataset holds per-run medians; a single-element list reproduces the
        # folder-mode median for each replicate (per-replicate dot).
        "spawn": {
            "compute_ttft": [v for v in [_opt_float(row.get("ttft_compute_median_s"))]
                             if v is not None],
            "storage_ttft": [v for v in [_opt_float(row.get("ttft_storage_median_s"))]
                             if v is not None],
            "compute_tfr": [v for v in [_opt_float(row.get("tfr_compute_median_s"))]
                            if v is not None]},
        "lat": {"p50": _opt_float(row.get("ep_p50_ms")),
                "p95": _opt_float(row.get("ep_p95_ms")),
                "p99": _opt_float(row.get("ep_p99_ms"))},
        "res": {"server_mean": _opt_float(row.get("ep_server_mean")),
                "server_max": _opt_float(row.get("ep_server_max")),
                "storage_mean": _opt_float(row.get("ep_storage_mean")),
                "storage_max": _opt_float(row.get("ep_storage_max"))},
        "fail": {"episode_pct": _opt_float(row.get("ep_failure_pct")),
                 "lan1_pct": _opt_float(row.get("ep_failure_lan1_pct")),
                 "lan2_pct": _opt_float(row.get("ep_failure_lan2_pct")),
                 "timeout_pct": _opt_float(row.get("ep_timeout_pct")),
                 "offered": _opt_int(row.get("ep_offered")),
                 "completed": _opt_int(row.get("ep_completed"))},
        "sel_actions": {"compute": _opt_int(row.get("sel_compute")) or 0,
                        "storage": _opt_int(row.get("sel_storage")) or 0,
                        "none": _opt_int(row.get("sel_none")) or 0},
        "sel_reasons": {"none": _opt_int(row.get("reason_none")) or 0,
                        "budget_exhausted": _opt_int(row.get("reason_budget_exhausted")) or 0,
                        "other": _opt_int(row.get("reason_other")) or 0},
    }


def write_dataset(runs: list[dict], out_path: Path) -> None:
    fields = [
        "run_id", "label", "policy", "episode", "replicate", "cell", "g2",
        "bv_lan1_verdict", "bv_lan1_proc_ms", "bv_lan1_cpu_pct", "bv_lan1_db_ms", "bv_lan1_storage_cpu_pct",
        "bv_lan2_verdict", "bv_lan2_proc_ms", "bv_lan2_cpu_pct", "bv_lan2_db_ms", "bv_lan2_storage_cpu_pct",
        "dec_n_rows", "dec_compute_actions", "dec_storage_actions",
        "budget_lan1_c", "budget_lan1_s", "budget_lan2_c", "budget_lan2_s",
        "agree_num", "agree_den", "agree_pct", "t98_ok",
        "relief_actions", "relief_recovered_num", "relief_recovered_den", "relief_recovery_median_s",
        "nm_compute", "nm_storage", "nm_per1000_compute", "nm_per1000_storage", "nm_recorded_requests",
        "ttft_compute_median_s", "ttft_storage_median_s", "tfr_compute_median_s",
        "ep_p50_ms", "ep_p95_ms", "ep_p99_ms",
        "ep_server_mean", "ep_server_max", "ep_storage_mean", "ep_storage_max",
        "ep_failure_pct", "ep_failure_lan1_pct", "ep_failure_lan2_pct",
        "ep_timeout_pct", "ep_offered", "ep_completed",
        "sel_compute", "sel_storage", "sel_none",
        "reason_none", "reason_budget_exhausted", "reason_other",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in runs:
            bv1 = r["bv"].get("lan1", {})
            bv2 = r["bv"].get("lan2", {})
            dec = r["dec"]
            rel = r["relief"]
            nm = r["nm"]
            sp = r["spawn"]
            agree_pct = (100.0 * dec["agree_num"] / dec["agree_den"]
                         if dec["agree_num"] is not None and dec["agree_den"] else None)
            row = {
                "run_id": r["run_id"], "label": r["label"], "policy": r["policy"],
                "episode": r["episode"], "replicate": r["replicate"], "cell": r["cell"], "g2": r["g2"],
                "bv_lan1_verdict": bv1.get("verdict"), "bv_lan1_proc_ms": bv1.get("proc_ms"),
                "bv_lan1_cpu_pct": bv1.get("cpu_pct"), "bv_lan1_db_ms": bv1.get("db_ms"),
                "bv_lan1_storage_cpu_pct": bv1.get("storage_cpu_pct"),
                "bv_lan2_verdict": bv2.get("verdict"), "bv_lan2_proc_ms": bv2.get("proc_ms"),
                "bv_lan2_cpu_pct": bv2.get("cpu_pct"), "bv_lan2_db_ms": bv2.get("db_ms"),
                "bv_lan2_storage_cpu_pct": bv2.get("storage_cpu_pct"),
                "dec_n_rows": dec["n_rows"], "dec_compute_actions": dec["compute"],
                "dec_storage_actions": dec["storage"],
                "budget_lan1_c": dec["budget"]["lan1"]["c"], "budget_lan1_s": dec["budget"]["lan1"]["s"],
                "budget_lan2_c": dec["budget"]["lan2"]["c"], "budget_lan2_s": dec["budget"]["lan2"]["s"],
                "agree_num": dec["agree_num"], "agree_den": dec["agree_den"], "agree_pct": agree_pct,
                "t98_ok": dec["t98"],
                "relief_actions": rel["actions"], "relief_recovered_num": rel["recovered_num"],
                "relief_recovered_den": rel["recovered_den"], "relief_recovery_median_s": rel["median_recovery_s"],
                "nm_compute": nm["compute_nm"], "nm_storage": nm["storage_nm"],
                "nm_per1000_compute": nm["per1000_compute"], "nm_per1000_storage": nm["per1000_storage"],
                "nm_recorded_requests": nm["recorded_requests"],
                "ttft_compute_median_s": (statistics.median(sp["compute_ttft"]) if sp["compute_ttft"] else None),
                "ttft_storage_median_s": (statistics.median(sp["storage_ttft"]) if sp["storage_ttft"] else None),
                "tfr_compute_median_s": (statistics.median(sp["compute_tfr"]) if sp["compute_tfr"] else None),
                "ep_p50_ms": r["lat"]["p50"], "ep_p95_ms": r["lat"]["p95"], "ep_p99_ms": r["lat"]["p99"],
                "ep_server_mean": r["res"]["server_mean"], "ep_server_max": r["res"]["server_max"],
                "ep_storage_mean": r["res"]["storage_mean"], "ep_storage_max": r["res"]["storage_max"],
                "ep_failure_pct": r["fail"]["episode_pct"],
                "ep_failure_lan1_pct": r["fail"]["lan1_pct"], "ep_failure_lan2_pct": r["fail"]["lan2_pct"],
                "ep_timeout_pct": r["fail"].get("timeout_pct"),
                "ep_offered": r["fail"].get("offered"), "ep_completed": r["fail"].get("completed"),
                "sel_compute": r["sel_actions"].get("compute", 0), "sel_storage": r["sel_actions"].get("storage", 0),
                "sel_none": r["sel_actions"].get("none", 0),
                "reason_none": r["sel_reasons"].get("none", 0),
                "reason_budget_exhausted": r["sel_reasons"].get("budget_exhausted", 0),
                "reason_other": sum(v for k, v in r["sel_reasons"].items()
                                    if k not in ("none", "budget_exhausted")),
            }
            w.writerow(row)
    print(f"Wrote {len(runs)} rows -> {out_path}")


# ---------------------------------------------------------------------------
# Plotting helpers (RQ1 styling: palette, alpha bars, black dots, dotted grid)
# ---------------------------------------------------------------------------

def _style_ax(ax):
    ax.grid(**GRID_KW)
    ax.set_axisbelow(True)


def _finish(ax, cells, ylabel, title, cap=None, cap_label=None, ylim=None):
    ax.set_xticks(list(range(len(cells))))
    ax.set_xticklabels(cells, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=12)
    ax.set_title(title, fontsize=13, fontweight="bold")
    if cap is not None:
        ax.axhline(cap, color="grey", linestyle="--", lw=1)
        if cap_label:
            ax.text(len(cells) - 0.45, cap, cap_label, ha="right", va="bottom",
                    fontsize=9, color="grey")
    if ylim:
        ax.set_ylim(*ylim)
    _style_ax(ax)


def _plot_grouped(ax, cells, series, cap=None, cap_label=None, ylim=None):
    """Grouped bars by cell, coloured per policy (RQ1 arm colours).

    series: list of dicts {"label", "get": run->float|None, "offset": float,
                           "alpha": float (default BAR_ALPHA)}.
    """
    x = np.arange(len(cells))
    n = len(series)
    width = 0.8 / n
    for si, s in enumerate(series):
        offset = (si - (n - 1) / 2) * width
        alpha = s.get("alpha", BAR_ALPHA)
        for ci, cell in enumerate(cells):
            color = POLICY_COLOR[cell.split("_")[0]]
            vals = [s["get"](r) for r in RUNS_BY_CELL[cell] if s["get"](r) is not None]
            label = s["label"] if ci == 0 else None
            if vals:
                mean = float(np.mean(vals))
                std = float(np.std(vals)) if len(vals) > 1 else 0.0
                ax.bar(x[ci] + offset, mean, width, yerr=std, color=color, alpha=alpha,
                       error_kw=ERROR_KW, label=label)
                xs = [x[ci] + offset + (j - (len(vals) - 1) / 2) * 0.05
                      for j in range(len(vals))]
                ax.scatter(xs, vals, color=DOT_COLOR, s=DOT_SIZE, zorder=3, alpha=DOT_ALPHA)
            else:
                ax.bar(x[ci] + offset, 0.0, width, color=color, alpha=alpha, label=label)
    _finish(ax, cells, "", "", cap=cap, cap_label=cap_label, ylim=ylim)
    return x


def _policy_patch(p: str) -> Patch:
    """Legend swatch for a policy arm colour."""
    return Patch(facecolor=POLICY_COLOR[p], alpha=BAR_ALPHA)


def _policy_handles_labels():
    """Handles/labels mapping the bar colours to the policy arms."""
    handles = [_policy_patch(p) for p in POLICIES]
    labels = [f"{POLICY_LABEL[p]} ({p})" for p in POLICIES]
    return handles, labels


def _series_handles_labels(first_label, second_label):
    """Policy colours + the solid/translucent tier-fill convention."""
    handles, labels = _policy_handles_labels()
    handles += [Patch(facecolor="#333333", alpha=1.0),
                Patch(facecolor="#333333", alpha=BAR_ALPHA_2)]
    labels += [f"{first_label} (solid)", f"{second_label} (translucent)"]
    return handles, labels


def _legend_outside(ax, handles, labels, ncol=1):
    """Legend in the reserved right margin — never overlaps the bars."""
    ax.legend(handles, labels, fontsize=7.5, frameon=False,
              loc="upper left", bbox_to_anchor=(1.02, 1.0), ncol=ncol)


# ---------------------------------------------------------------------------
# Graphs (analysis_focus.md §5 inventory)
# ---------------------------------------------------------------------------

def graph_bottleneck_validation(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    series = [
        {"label": "compute CPU %", "get":
         lambda r: ((r["bv"].get("lan1", {}).get("cpu_pct") or 0)
                    + (r["bv"].get("lan2", {}).get("cpu_pct") or 0)) / 2.0
         if r["bv"].get("lan1", {}).get("cpu_pct") is not None
         and r["bv"].get("lan2", {}).get("cpu_pct") is not None else None,
         "offset": -0.5},
        {"label": "storage CPU %", "get":
         lambda r: ((r["bv"].get("lan1", {}).get("storage_cpu_pct") or 0)
                    + (r["bv"].get("lan2", {}).get("storage_cpu_pct") or 0)) / 2.0
         if r["bv"].get("lan1", {}).get("storage_cpu_pct") is not None
         and r["bv"].get("lan2", {}).get("storage_cpu_pct") is not None else None,
         "offset": 0.5, "alpha": BAR_ALPHA_2},
    ]
    _plot_grouped(ax, CELLS, series)
    ax.axvline(2.5, color="black", linestyle=":", linewidth=1, alpha=0.5)
    ax.text(2.5, ax.get_ylim()[1] * 0.9, "compute-bound", ha="center", fontsize=10, alpha=0.7)
    ax.set_ylabel("episode median tier CPU %", fontsize=12)
    ax.set_title("G2 episode induction: tier CPU during episode (median, per LAN averaged)",
                 fontsize=13, fontweight="bold")
    _legend_outside(ax, *_series_handles_labels("compute CPU", "storage CPU"))
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "bottleneck_validation.png", dpi=DPI)
    plt.close(fig)


def graph_classifier_agreement(runs, out_dir):
    cells = [c for c in CELLS if c.startswith("ba_")]
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    x = np.arange(len(cells))
    for ci, cell in enumerate(cells):
        vals = []
        for r in RUNS_BY_CELL[cell]:
            d = r["dec"]
            if d["agree_num"] is not None and d["agree_den"]:
                vals.append(100.0 * d["agree_num"] / d["agree_den"])
        if vals:
            mean = float(np.mean(vals))
            std = float(np.std(vals)) if len(vals) > 1 else 0.0
            ax.bar(x[ci], mean, 0.55, yerr=std, color=POLICY_COLOR["ba"],
                   alpha=BAR_ALPHA, error_kw=ERROR_KW)
            xs = [x[ci] + (j - (len(vals) - 1) / 2) * 0.05 for j in range(len(vals))]
            ax.scatter(xs, vals, color=DOT_COLOR, s=DOT_SIZE, zorder=3, alpha=DOT_ALPHA)
        else:
            ax.bar(x[ci], 0.0, 0.55, color=POLICY_COLOR["ba"], alpha=BAR_ALPHA)
    ax.axhline(50, color="grey", linestyle="--", lw=1)
    ax.text(len(cells) - 0.35, 50, "chance (50%)", ha="right", va="bottom", fontsize=9, color="grey")
    ax.axhline(100, color="grey", linestyle="--", lw=0.8, alpha=0.5)
    _finish(ax, cells, "classifier-vs-episode agreement %",
            "H3: bottleneck classifier agreement (ba cells, both-eligible episode windows)",
            ylim=(0, 105))
    ax.legend([Patch(facecolor=POLICY_COLOR["ba"], alpha=BAR_ALPHA)],
              ["bottleneck_aware (ba)"], fontsize=7.5, frameon=False,
              loc="upper left", bbox_to_anchor=(1.02, 1.0))
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "classifier_agreement.png", dpi=DPI)
    plt.close(fig)


def graph_selected_action(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    x = np.arange(len(CELLS))
    tiers = ["compute", "storage", "none"]
    colors = {"compute": "#4C72B0", "storage": "#DD8452", "none": "#BDBDBD"}
    bottom = np.zeros(len(CELLS))
    for tier in tiers:
        fracs, dots = [], []
        for ci, cell in enumerate(CELLS):
            vals = []
            for r in RUNS_BY_CELL[cell]:
                total = sum(r["sel_actions"].values())
                if total:
                    vals.append(100.0 * r["sel_actions"].get(tier, 0) / total)
            if vals:
                fracs.append(float(np.mean(vals)))
                dots.append([(ci, bottom[ci] + v) for v in vals])
            else:
                fracs.append(0.0); dots.append([])
        ax.bar(x, fracs, 0.8, bottom=bottom, color=colors[tier], alpha=0.9,
               edgecolor="black", linewidth=0.4, label=tier)
        for i, d in enumerate(dots):
            if d:
                xs, ys = zip(*d)
                ax.scatter(list(xs), list(ys), s=DOT_SIZE, color=DOT_COLOR, zorder=3,
                           alpha=DOT_ALPHA * 0.6)
        bottom += np.array(fracs)
    _finish(ax, CELLS, "episode windows % by selected action",
            "Selected action distribution over episode windows (both LANs)", ylim=(0, 105))
    _legend_outside(ax, [Patch(facecolor=colors[t], alpha=0.9) for t in tiers], tiers)
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "selected_action.png", dpi=DPI)
    plt.close(fig)


def graph_action_counts(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _plot_grouped(ax, CELLS, [
        {"label": "compute spawns", "get": lambda r: r["dec"]["compute"], "offset": -0.5},
        {"label": "storage spawns", "get": lambda r: r["dec"]["storage"],
         "offset": 0.5, "alpha": BAR_ALPHA_2},
    ], cap=4, cap_label="budget 4/tier/LAN")
    ax.set_ylabel("scale-up spawns (both LANs)", fontsize=12)
    ax.set_title("Scale actions per cell (spawns = 2 LANs x budget/tier)", fontsize=13, fontweight="bold")
    _legend_outside(ax, *_series_handles_labels("compute spawns", "storage spawns"))
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "action_counts.png", dpi=DPI)
    plt.close(fig)


def graph_budget_usage(runs, out_dir):
    fig, axes = plt.subplots(1, 2, figsize=FIG_DOUBLE)
    ax = axes[0]
    _plot_grouped(ax, CELLS, [
        {"label": "compute budget used", "get":
         lambda r: max(r["dec"]["budget"]["lan1"]["c"], r["dec"]["budget"]["lan2"]["c"]),
         "offset": -0.5},
        {"label": "storage budget used", "get":
         lambda r: max(r["dec"]["budget"]["lan1"]["s"], r["dec"]["budget"]["lan2"]["s"]),
         "offset": 0.5, "alpha": BAR_ALPHA_2},
    ], cap=4, cap_label="budget cap 4")
    ax.set_ylabel("budget used (max over LANs)", fontsize=12)
    ax.set_title("Per-tier action budget usage vs cap", fontsize=13, fontweight="bold")

    ax = axes[1]
    x = np.arange(len(CELLS))
    reasons = ["none", "budget_exhausted", "other"]
    rcolors = {"none": "#BDBDBD", "budget_exhausted": "#DD8452", "other": "#4C72B0"}
    bottom = np.zeros(len(CELLS))
    for reason in reasons:
        fracs = []
        for cell in CELLS:
            vals = []
            for r in RUNS_BY_CELL[cell]:
                total = sum(r["sel_reasons"].values())
                if total:
                    vals.append(100.0 * r["sel_reasons"].get(reason, 0) / total)
            fracs.append(float(np.mean(vals)) if vals else 0.0)
        ax.bar(x, fracs, 0.8, bottom=bottom, color=rcolors[reason], alpha=0.9,
               edgecolor="black", linewidth=0.4, label=reason)
        bottom += np.array(fracs)
    _finish(ax, CELLS, "episode windows % by reason",
            "Decision reason distribution (episode windows)", ylim=(0, 105))
    hs, ls = _series_handles_labels("compute budget", "storage budget")
    leg1 = fig.legend(hs, ls, loc="upper center", bbox_to_anchor=(0.25, 0.99),
                      ncol=5, fontsize=8, frameon=False)
    fig.add_artist(leg1)
    rh = [Patch(facecolor=rcolors[r], alpha=0.9) for r in reasons]
    fig.legend(rh, reasons, loc="upper center", bbox_to_anchor=(0.75, 0.99),
               ncol=3, fontsize=8, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, TOP_RESERVE))
    fig.savefig(out_dir / "budget_usage.png", dpi=DPI)
    plt.close(fig)


def graph_time_to_recover(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _plot_grouped(ax, CELLS, [
        {"label": "median recovery (s)", "get":
         lambda r: r["relief"]["median_recovery_s"] if r["relief"]["recovered_num"] else None,
         "offset": 0.0},
    ])
    ax.set_ylabel("median recovery time (s)", fontsize=12)
    ax.set_title("Time to recover targeted-tier pressure (relief tool)", fontsize=13, fontweight="bold")
    _legend_outside(ax, *_policy_handles_labels())
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "time_to_recover.png", dpi=DPI)
    plt.close(fig)


def graph_time_to_usable_capacity(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _plot_grouped(ax, CELLS, [
        {"label": "compute TTFT (median)", "get":
         lambda r: statistics.median(r["spawn"]["compute_ttft"]) if r["spawn"]["compute_ttft"] else None,
         "offset": -0.5},
        {"label": "storage TTFT (median)", "get":
         lambda r: statistics.median(r["spawn"]["storage_ttft"]) if r["spawn"]["storage_ttft"] else None,
         "offset": 0.5, "alpha": BAR_ALPHA_2},
    ])
    ax.set_ylabel("time to usable capacity (s, per-run median)", fontsize=12)
    ax.set_title("Time-to-usable-capacity (action ts -> first serving; extract_spawn_metrics)",
                 fontsize=13, fontweight="bold")
    _legend_outside(ax, *_series_handles_labels("compute TTFT", "storage TTFT"))
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "time_to_usable_capacity.png", dpi=DPI)
    plt.close(fig)


def _latency_graph(runs, out_dir, key, pct, fname):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _plot_grouped(ax, CELLS, [
        {"label": f"episode p{pct}", "get": lambda r, k=key: r["lat"][k], "offset": 0.0},
    ])
    ax.set_ylabel(f"episode p{pct} latency (ms)", fontsize=12)
    ax.set_title(f"Episode-phase p{pct} latency per cell", fontsize=13, fontweight="bold")
    _legend_outside(ax, *_policy_handles_labels())
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / fname, dpi=DPI)
    plt.close(fig)


def graph_failure_rate(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _plot_grouped(ax, CELLS, [
        {"label": "episode failure %", "get": lambda r: r["fail"]["episode_pct"], "offset": 0.0},
    ])
    ax.set_ylabel("episode failure rate %", fontsize=12)
    ax.set_title("Episode-phase request failure rate per cell", fontsize=13, fontweight="bold")
    _legend_outside(ax, *_policy_handles_labels())
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "failure_rate.png", dpi=DPI)
    plt.close(fig)


def graph_node_minutes(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _plot_grouped(ax, CELLS, [
        {"label": "compute node-min/1000 req", "get": lambda r: r["nm"]["per1000_compute"],
         "offset": -0.5},
        {"label": "storage node-min/1000 req", "get": lambda r: r["nm"]["per1000_storage"],
         "offset": 0.5, "alpha": BAR_ALPHA_2},
    ])
    ax.set_ylabel("node-minutes per 1000 recorded requests", fontsize=12)
    ax.set_title("Resource efficiency: node-minutes normalised per 1000 requests (criterion 9)",
                 fontsize=13, fontweight="bold")
    _legend_outside(ax, *_series_handles_labels("compute node-min", "storage node-min"))
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "node_minutes.png", dpi=DPI)
    plt.close(fig)


def graph_relief_targeted_tier(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    series = [{"label": "actions recovered in-tier %",
               "get": lambda r: (100.0 * r["relief"]["recovered_num"] / r["relief"]["recovered_den"])
               if r["relief"]["recovered_den"] else None,
               "offset": 0.0}]
    _plot_grouped(ax, CELLS, series, ylim=(0, 105))
    ax.set_ylabel("actions that recovered in targeted tier %", fontsize=12)
    ax.set_title("Relief in the targeted tier (criterion 6) — share of actions recovering",
                 fontsize=13, fontweight="bold")
    _legend_outside(ax, *_policy_handles_labels())
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "relief_targeted_tier.png", dpi=DPI)
    plt.close(fig)


def graph_cross_over(runs, out_dir):
    """Headline: per episode, time-to-recover + p95 across the 3 policies."""
    fig, axes = plt.subplots(1, 2, figsize=FIG_DOUBLE)
    for ep_idx, episode in enumerate(("cb", "db")):
        ax = axes[ep_idx]
        cells = [f"{p}_{episode}" for p in POLICIES]
        x = np.arange(len(cells))
        ttr_means, ttr_std, ttr_dots = [], [], []
        for ci, cell in enumerate(cells):
            vals = [r["relief"]["median_recovery_s"] for r in RUNS_BY_CELL[cell]
                    if r["relief"]["median_recovery_s"] is not None and r["relief"]["recovered_num"]]
            if vals:
                ttr_means.append(float(np.mean(vals)))
                ttr_std.append(float(np.std(vals)) if len(vals) > 1 else 0.0)
                ttr_dots.append([(ci, v) for v in vals])
            else:
                ttr_means.append(0.0); ttr_std.append(0.0); ttr_dots.append([])
        ax2 = ax.twinx()
        for ci, cell in enumerate(cells):
            color = POLICY_COLOR[cell.split("_")[0]]
            ax.bar(x[ci] - 0.2, ttr_means[ci], 0.4, yerr=ttr_std[ci],
                   color=color, alpha=BAR_ALPHA, error_kw=ERROR_KW,
                   label="time-to-recover (s)" if ci == 0 else None)
            if ttr_dots[ci]:
                xs = [x[ci] - 0.2 + (j - (len(ttr_dots[ci]) - 1) / 2) * 0.05
                      for j in range(len(ttr_dots[ci]))]
                ax.scatter(xs, [d for _, d in ttr_dots[ci]], color=DOT_COLOR,
                           s=DOT_SIZE, zorder=3, alpha=DOT_ALPHA)
        ax.set_ylabel("median recovery time (s)", fontsize=11, color="#4C72B0")
        for ci, cell in enumerate(cells):
            color = POLICY_COLOR[cell.split("_")[0]]
            vals = [r["lat"]["p95"] for r in RUNS_BY_CELL[cell] if r["lat"]["p95"] is not None]
            if vals:
                mean = float(np.mean(vals))
                std = float(np.std(vals)) if len(vals) > 1 else 0.0
                ax2.bar(x[ci] + 0.2, mean, 0.4, yerr=std, color=color, alpha=BAR_ALPHA_2,
                        error_kw=ERROR_KW, label="episode p95 (ms)" if ci == 0 else None)
                xs = [x[ci] + 0.2 + (j - (len(vals) - 1) / 2) * 0.05 for j in range(len(vals))]
                ax2.scatter(xs, vals, color=DOT_COLOR, s=DOT_SIZE, zorder=3, alpha=DOT_ALPHA)
        ax2.set_ylabel("episode p95 latency (ms)", fontsize=11, color="#DD8452")
        ax.set_xticks(x)
        ax.set_xticklabels([f"{p} ({'compute-bound' if episode == 'cb' else 'data-bound'})"
                            for p in POLICIES], fontsize=11)
        ax.set_title(f"{'Compute-bound' if episode == 'cb' else 'Data-bound'} episode: recovery + p95 across policies",
                     fontsize=13, fontweight="bold")
        _style_ax(ax)
        _style_ax(ax2)
    hs, ls = _series_handles_labels("time-to-recover", "episode p95")
    fig.suptitle("Criterion 8 headline: cross-over — correctly-aligned arm + ba vs mis-aligned arm",
                 fontsize=14, fontweight="bold", y=0.99)
    fig.legend(hs, ls, loc="upper center", bbox_to_anchor=(0.5, 0.95),
               ncol=5, fontsize=8, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, TOP_RESERVE))
    fig.savefig(out_dir / "cross_over.png", dpi=DPI)
    plt.close(fig)


def graph_counterbalance(runs, out_dir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    y_labels = CELLS
    y_pos = {c: i for i, c in enumerate(y_labels)}
    for r in runs:
        cell = r["cell"]
        repl = r["replicate"]
        ax.scatter(repl, y_pos[cell], s=90, color=POLICY_COLOR[r["policy"]],
                   alpha=0.85, edgecolor="black", linewidth=0.5, zorder=5)
        ax.annotate(r["policy"], (repl, y_pos[cell]), textcoords="offset points",
                    xytext=(0, -13), ha="center", fontsize=7, color="black")
    ax.set_xticks([1, 2, 3, 4, 5, 6])
    ax.set_xticklabels([f"block {i}" for i in range(1, 7)], fontsize=11)
    ax.set_yticks(list(range(len(y_labels))))
    ax.set_yticklabels(y_labels, fontsize=10)
    ax.set_xlabel("replicate (block order)", fontsize=12)
    ax.set_title("Counterbalance: per-run cell across the 6 blocks (policy colors)",
                 fontsize=13, fontweight="bold")
    _style_ax(ax)
    _legend_outside(ax, *_policy_handles_labels())
    fig.tight_layout(rect=(0, 0, RIGHT_RESERVE, 1))
    fig.savefig(out_dir / "counterbalance.png", dpi=DPI)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics-dir", default="source/scripts/testing/metrics")
    ap.add_argument("--vm-per-run", default="docs/operation/testing/experiment/v2/rq2/analysis/vm_per_run")
    ap.add_argument("--out", default="docs/operation/testing/experiment/v2/rq2/analysis")
    ap.add_argument("--graphs-dir", default=None,
                    help="Graph output dir (default: <out>/graphs)")
    ap.add_argument("--from-dataset", default=None,
                    help="Regenerate graphs from a campaign_dataset.csv instead of run folders")
    ap.add_argument("--run-dirs", nargs="*", default=None,
                    help="Restrict to these run folder names (folder mode only)")
    args = ap.parse_args()

    graph_dir = Path(args.graphs_dir) if args.graphs_dir else (Path(args.out) / "graphs")

    global RUNS_BY_CELL
    if args.from_dataset:
        ds_path = Path(args.from_dataset)
        if not ds_path.exists():
            print(f"ERROR: dataset not found: {ds_path}")
            return 1
        with open(ds_path, encoding="utf-8", errors="replace") as fh:
            rows = list(csv.DictReader(fh))
        runs = [load_run_from_dataset(r) for r in rows]
        print(f"Loaded {len(runs)} runs from dataset {ds_path}")
    else:
        metrics_dir = Path(args.metrics_dir)
        vm_per_run = Path(args.vm_per_run)
        all_runs = discover_runs(metrics_dir)
        if args.run_dirs:
            wanted = set(args.run_dirs)
            all_runs = [d for d in all_runs if d.name in wanted]
        if not all_runs:
            print("No RQ2 main-campaign runs found (pattern: <ts>_rq2_<p>_<e>_<1..6>).")
            return 1
        runs = [load_run(d, vm_per_run) for d in all_runs]
        print(f"Loading {len(runs)} runs from {metrics_dir} ...")
        write_dataset(runs, Path(args.out) / "campaign_dataset.csv")

    RUNS_BY_CELL = defaultdict(list)
    for r in runs:
        RUNS_BY_CELL[r["cell"]].append(r)

    missing = [f"{c}:{len(RUNS_BY_CELL[c])}" for c in CELLS if len(RUNS_BY_CELL[c]) != 6]
    if missing:
        print(f"[WARN] replicate counts != 6: {', '.join(missing)}")
    for r in runs:
        if r["g2"] is None:
            print(f"[WARN] {r['run_id']}: no bottleneck-validation verdict")

    graph_dir.mkdir(parents=True, exist_ok=True)
    print(f"Generating 15 comparison graphs -> {graph_dir}")
    graph_bottleneck_validation(runs, graph_dir)
    graph_classifier_agreement(runs, graph_dir)
    graph_selected_action(runs, graph_dir)
    graph_action_counts(runs, graph_dir)
    graph_budget_usage(runs, graph_dir)
    graph_time_to_recover(runs, graph_dir)
    graph_time_to_usable_capacity(runs, graph_dir)
    _latency_graph(runs, graph_dir, "p50", 50, "latency_p50.png")
    _latency_graph(runs, graph_dir, "p95", 95, "latency_p95.png")
    _latency_graph(runs, graph_dir, "p99", 99, "latency_p99.png")
    graph_failure_rate(runs, graph_dir)
    graph_node_minutes(runs, graph_dir)
    graph_relief_targeted_tier(runs, graph_dir)
    graph_cross_over(runs, graph_dir)
    graph_counterbalance(runs, graph_dir)

    pngs = sorted(graph_dir.glob("*.png"))
    print(f"Wrote {len(pngs)} PNGs:")
    for p in pngs:
        print(f"  {p.name}")
    print("Done.")
    return 0


RUNS_BY_CELL: dict[str, list[dict]] = {}


if __name__ == "__main__":
    raise SystemExit(main())
