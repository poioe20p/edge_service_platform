#!/usr/bin/env python3
"""Extract spawn-to-service metrics from a single run folder.

Computes per-spawn-event metrics for RQ2 run folders:
  - TTFT        (time-to-first-traffic)
  - TFR         (time-to-first-response)
  - init_time   (TFR − TTFT, backend initialisation proxy)
  - initial_share (fraction of VIP traffic in first visible window)

Covers BOTH tiers (RQ2 v2 framing):
  - compute: [elasticity] compute: spawning <name> on LAN <n> (ip=... mac=...)
  - storage: [elasticity] data:  spawning <name> on LAN <n> (ip=... mac=...)

Action-time anchoring (RQ2 v2 framing):
  By default TTFT/TFR anchor on the controller-log "spawning" line (spawn
  start) — legacy v5 behaviour. Pass ``--anchor decision`` to anchor on the
  decision-log ``scale_up`` action rows (the RQ2 "action time", matched per
  LAN/tier in time order), matching rq2_preparation.md §5 "action time → first
  successful request". Storage nodes serve DB operations (they have no HTTP
  ``backend_id``), so their TFR is left empty; their usable-capacity signal is
  the first serving window (``ttft_s`` via per_node_stats request_count>0).
  Compute TFR comes from ``client_requests.csv`` ``backend_id``.

Usage:
    python -m source.scripts.testing.analysis.rq2.extract_spawn_metrics \\
        <run_dir> [--mode topology_host] [--tiers compute,storage]
        [--anchor decision] [--out analysis/rq2_spawn_metrics.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> float | None:
    """Parse ISO-8601 timestamp to Unix seconds."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Step 1 — Extract spawn events from controller logs
# ---------------------------------------------------------------------------

def extract_spawns(run_dir: Path, tiers=("compute",)) -> list[dict]:
    """Parse controller_lan*.log for scale-up spawn events.

    compute tier: lines containing "[elasticity] compute: spawning <name> on ..."
    storage tier: lines containing "[elasticity] data:  spawning <name> on ..."

    Returns list of dicts with keys:
      spawn_ts    — Unix timestamp of the spawn log line
      container   — container name (e.g. edge_server_lan1_dyn2)
      mac         — MAC address
      lan         — "lan1" or "lan2"
      tier        — "compute" | "storage"
    """
    tier_markers = {
        "compute": "compute:",
        "storage": "data:",
    }
    spawns: list[dict] = []
    for lan_label in ("lan1", "lan2"):
        log_path = run_dir / f"controller_{lan_label}.log"
        if not log_path.exists():
            continue
        # Controller logs can carry arbitrary bytes (e.g. UTF-8 escapes or
        # binary fragments); decode leniently so one bad line never aborts the run.
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # Log format: [elasticity] <tier>: spawning <name> on LAN <n> (ip=... mac=...)
                # compute -> "compute:" (elasticity.py), storage -> "data:" (elasticity.py)
                if "spawning" not in line:
                    continue
                tier = next((t for t, m in tier_markers.items() if m in line), None)
                if tier is None or tier not in tiers:
                    continue
                parts = line.split(" ")
                if len(parts) < 3:
                    continue
                # Preserve the millisecond fraction: log time is "HH:MM:SS,mmm"
                # (comma separator). Dropping it truncates spawn_ts to whole
                # seconds, which puts spawn_ts *before* the ms-precision
                # decision action ts and breaks `action_ts <= spawn_ts` pairing.
                # Tag the timestamp as UTC: controller logs are written in UTC
                # on the VM and the decision-log ts is epoch-UTC; without the
                # tag fromisoformat()/timestamp() resolves the naive datetime
                # in the *local* timezone, breaking pairing on any non-UTC host.
                iso_ts = parts[0] + "T" + parts[1].replace(",", ".") + "+00:00"
                unix_ts = _parse_ts(iso_ts)
                if unix_ts is None:
                    continue

                # Container name: between "spawning" and "on"
                container_match = re.search(r"spawning\s+(\S+)\s+on", line)
                container = container_match.group(1) if container_match else None

                # MAC: mac=00:00:00:00:02:09
                mac_match = re.search(r"mac=([0-9a-f:]+)", line)
                mac = mac_match.group(1) if mac_match else None

                if unix_ts and container:
                    spawns.append({
                        "spawn_ts": unix_ts,
                        "container": container,
                        "mac": mac,
                        "lan": lan_label,
                        "tier": tier,
                    })
    return spawns


# ---------------------------------------------------------------------------
# Step 1b — Decision-log action anchoring (RQ2 v2 framing)
# ---------------------------------------------------------------------------

def load_action_rows(run_dir: Path) -> list[dict]:
    """Load RQ2 scale_up action rows from decision_log_lan{1,2}.csv.

    Only for runs whose decision log carries the RQ2 columns (SCALEUP_POLICY is
    one of the RQ2 arms). Returns one dict per submitted scale-up action:
      ts        — decision time (unix seconds)
      lan       — "lan1" | "lan2"
      tier      — "compute" | "storage"
      window_id — the window the action was taken on
      action    — "ComputeAlert" | "DataAlert"

    A row is a submitted action iff action_type == "scale_up" and
    selected_action in {"compute", "storage"}; deduped by (window_id, action_type).
    """
    rows: list[dict] = []
    for lan_label in ("lan1", "lan2"):
        path = run_dir / f"decision_log_{lan_label}.csv"
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if "selected_action" not in (reader.fieldnames or []):
                continue  # legacy/dual format — no RQ2 columns
            seen: set[tuple[str, str]] = set()
            for row in reader:
                if row.get("action_type") != "scale_up":
                    continue
                key = (row.get("window_id") or "", row.get("action_type") or "")
                if key in seen:
                    continue
                seen.add(key)
                sel = (row.get("selected_action") or "").strip()
                if sel not in ("compute", "storage"):
                    continue
                ts = _safe_float(row.get("ts"))
                if ts <= 0:
                    continue
                rows.append({
                    "ts": ts,
                    "lan": lan_label,
                    "tier": sel,
                    "window_id": row.get("window_id", ""),
                    "action": row.get("action", ""),
                })
    rows.sort(key=lambda a: (a["lan"], a["tier"], a["ts"]))
    return rows


def pair_actions_to_spawns(spawns: list[dict], actions: list[dict]) -> dict[int, dict | None]:
    """Greedily pair decision-log action rows to spawn events per (lan, tier).

    Within each (lan, tier) both lists are time-ordered; each spawn takes the
    earliest unpaired action whose ts <= spawn_ts (a decision precedes its
    spawn). Returns {spawn_index: action_or_None}.
    """
    if not actions:
        return {i: None for i in range(len(spawns))}
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in actions:
        by_key[(a["lan"], a["tier"])].append(a)
    ptr = {k: 0 for k in by_key}
    paired: dict[int, dict | None] = {}
    for i, sp in enumerate(spawns):
        k = (sp["lan"], sp["tier"])
        acts = by_key.get(k, [])
        if ptr[k] < len(acts) and acts[ptr[k]]["ts"] <= sp["spawn_ts"]:
            paired[i] = acts[ptr[k]]
            ptr[k] += 1
        else:
            paired[i] = None
    return paired


# ---------------------------------------------------------------------------
# Step 2 — Compute TTFT from per_node_stats.csv
# ---------------------------------------------------------------------------

def compute_ttft(spawns: list[dict], run_dir: Path) -> dict[int, float | None]:
    """Match each spawn to its first telemetry window with request_count > 0.

    Returns dict mapping spawn index → TTFT in seconds, or None if unmatched.

    Anchored on ``sp['anchor_ts']`` when present (decision-log action time),
    else ``spawn_ts``. v4 fix: matches first window *after the anchor* for the
    MAC, not first-ever window — correctly handles MAC reuse across lifetimes.
    """
    pns_path = run_dir / "per_node_stats.csv"
    if not pns_path.exists():
        return {i: None for i in range(len(spawns))}

    # Collect ALL (window_end, request_count) per MAC
    mac_windows: dict[str, list[tuple[float, int]]] = defaultdict(list)
    with open(pns_path, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            mac = row.get("server_id", "").strip()
            rc = _safe_int(row.get("request_count"))
            we = _safe_float(row.get("window_end"))
            if mac and we > 0:
                mac_windows[mac].append((we, rc))

    ttft: dict[int, float | None] = {}
    for i, sp in enumerate(spawns):
        mac = sp["mac"]
        anchor = sp.get("anchor_ts", sp["spawn_ts"])
        if mac and mac in mac_windows:
            # Find first window_end >= anchor with request_count > 0
            for we, rc in sorted(mac_windows[mac]):
                if we >= anchor and rc > 0:
                    ttft_val = we - anchor
                    ttft[i] = ttft_val if 0 <= ttft_val <= 600 else None
                    break
            else:
                ttft[i] = None
        else:
            ttft[i] = None
    return ttft


# ---------------------------------------------------------------------------
# Step 3 — Compute TFR from client_requests.csv
# ---------------------------------------------------------------------------

def compute_tfr(spawns: list[dict], run_dir: Path) -> dict[int, float | None]:
    """Match each spawn to its first HTTP response via the backend_id column.

    Returns dict mapping spawn index → TFR in seconds, or None if unmatched.
    Storage nodes serve DB ops (no HTTP backend_id), so they always return None.
    Anchored on ``sp['anchor_ts']`` when present, else ``spawn_ts``.
    """
    cr_path = run_dir / "client_requests.csv"
    if not cr_path.exists():
        return {i: None for i in range(len(spawns))}

    # Read all rows, grouping completed_at by backend_id
    backend_timestamps: dict[str, list[float]] = defaultdict(list)
    with open(cr_path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if "backend_id" not in (reader.fieldnames or []):
            print(f"  [WARN] client_requests.csv has no 'backend_id' column - TFR unavailable")
            return {i: None for i in range(len(spawns))}
        for row in reader:
            bid = row.get("backend_id", "").strip()
            completed = _parse_ts(row.get("completed_at", ""))
            if bid and completed is not None and completed > 0:
                backend_timestamps[bid].append(completed)

    tfr: dict[int, float | None] = {}
    for i, sp in enumerate(spawns):
        container = sp["container"]
        anchor = sp.get("anchor_ts", sp["spawn_ts"])
        if container and container in backend_timestamps:
            # First response after the anchor
            candidates = [ts for ts in backend_timestamps[container]
                          if ts >= anchor]
            if candidates:
                tfr_val = min(candidates) - anchor
                tfr[i] = tfr_val if 0 <= tfr_val <= 600 else None
            else:
                tfr[i] = None
        else:
            tfr[i] = None
    return tfr


# ---------------------------------------------------------------------------
# Step 4 — Initial load share (from per_node_stats first window)
# ---------------------------------------------------------------------------

def compute_initial_share(spawns: list[dict], run_dir: Path) -> tuple[dict[int, float | None], dict[int, int | None]]:
    """Compute the fraction of VIP traffic captured by the new backend in its
    first visible telemetry window, and the pool size (active backends) in that window.

    Uses per_node_stats.csv: for the first window where request_count > 0 for
    the spawn MAC, compute request_count / total_requests_in_that_window
    (summed across all backends reporting in the same window).
    """
    pns_path = run_dir / "per_node_stats.csv"
    if not pns_path.exists():
        return {i: None for i in range(len(spawns))}, {i: None for i in range(len(spawns))}

    # First pass: build window_totals, per-window node counts, and per-MAC first-window data
    window_totals: dict[float, int] = {}
    window_node_counts: dict[float, set] = {}  # window_end -> set of MACs
    first_window_data: dict[str, tuple[float, int]] = {}  # mac -> (window_end, request_count)
    with open(pns_path, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            mac = row.get("server_id", "").strip()
            rc = _safe_int(row.get("request_count"))
            we = _safe_float(row.get("window_end"))
            if not mac or we <= 0:
                continue
            window_totals[we] = window_totals.get(we, 0) + rc
            if we not in window_node_counts:
                window_node_counts[we] = set()
            if rc > 0:
                window_node_counts[we].add(mac)
            if rc > 0 and mac not in first_window_data:
                first_window_data[mac] = (we, rc)

    window_pool_sizes: dict[float, int] = {we: len(macs) for we, macs in window_node_counts.items()}

    init_share: dict[int, float | None] = {}
    pool_size: dict[int, int | None] = {}
    for i, sp in enumerate(spawns):
        mac = sp["mac"]
        if mac and mac in first_window_data:
            we, rc = first_window_data[mac]
            total = window_totals.get(we, 0)
            init_share[i] = rc / total if total > 0 else None
            pool_size[i] = window_pool_sizes.get(we)
        else:
            init_share[i] = None
            pool_size[i] = None
    return init_share, pool_size


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract RQ2 spawn metrics from a run folder")
    parser.add_argument("run_dir", type=Path, help="Path to run folder")
    parser.add_argument("--mode", default="unknown", help="Routing mode label")
    parser.add_argument("--tiers", default="compute",
                        help="Comma-separated spawn tiers to extract: compute,storage "
                             "(default: compute — legacy v5 behaviour). "
                             "RQ2 v2 runs should pass compute,storage.")
    parser.add_argument("--anchor", choices=("spawn", "decision"), default="spawn",
                        help="TTFT/TFR anchor: 'spawn' (controller-log spawning line, "
                             "legacy) or 'decision' (RQ2 decision-log scale_up action "
                             "row, rq2_preparation.md §5 'action time').")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output CSV path (default: <run_dir>/analysis/rq2_spawn_metrics.csv)")
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.is_dir():
        print(f"ERROR: {run_dir} is not a directory")
        return 1

    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())
    print(f"Extracting spawn metrics from {run_dir.name} "
          f"(tiers={','.join(tiers)}, anchor={args.anchor})")

    spawns = extract_spawns(run_dir, tiers)
    print(f"  Scale-up spawn events: {len(spawns)}")
    if not spawns:
        print(f"  No scale-up spawns found for tier(s) {','.join(tiers)} - nothing to do.")
        return 0

    # Action-time anchoring (RQ2 v2 framing): pair decision-log scale_up rows to
    # spawn events per (lan, tier) in time order; anchor TTFT/TFR on the action
    # time instead of the controller-log spawn line.
    action_rows = load_action_rows(run_dir) if args.anchor == "decision" else []
    paired = pair_actions_to_spawns(spawns, action_rows) if action_rows else {}
    n_anchored = 0
    for i, sp in enumerate(spawns):
        act = paired.get(i)
        if act is not None:
            sp["action_ts"] = act["ts"]
            sp["action_to_spawn_s"] = sp["spawn_ts"] - act["ts"]
            sp["anchor_ts"] = act["ts"]
            n_anchored += 1
        else:
            sp["action_ts"] = ""
            sp["action_to_spawn_s"] = ""
            sp["anchor_ts"] = sp["spawn_ts"]

    ttft = compute_ttft(spawns, run_dir)
    tfr = compute_tfr(spawns, run_dir)
    init_share, pool_sizes = compute_initial_share(spawns, run_dir)

    # Assemble rows
    rows: list[dict] = []
    n_ttft = n_tfr = 0
    for i, sp in enumerate(spawns):
        ttft_val = ttft.get(i)
        tfr_val = tfr.get(i)
        init_val = init_share.get(i)
        ps_val = pool_sizes.get(i)
        if ttft_val is not None:
            n_ttft += 1
        if tfr_val is not None:
            n_tfr += 1
        rows.append({
            "spawn_ts": sp["spawn_ts"],
            "container": sp["container"],
            "mac": sp.get("mac", ""),
            "lan": sp["lan"],
            "tier": sp["tier"],
            "mode": args.mode,
            "action_ts": sp["action_ts"],
            "action_to_spawn_s": (f"{sp['action_to_spawn_s']:.2f}"
                                  if isinstance(sp["action_to_spawn_s"], float) else ""),
            "ttft_s": f"{ttft_val:.1f}" if ttft_val is not None else "",
            "tfr_s": f"{tfr_val:.1f}" if tfr_val is not None else "",
            "init_time_s": f"{(tfr_val - ttft_val):.1f}" if (ttft_val is not None and tfr_val is not None) else "",
            "initial_share": f"{init_val:.4f}" if init_val is not None else "",
            "pool_size": f"{ps_val}" if ps_val is not None else "",
        })

    if action_rows:
        print(f"  Decision-log actions: {len(action_rows)}; "
              f"anchored spawns: {n_anchored}/{len(spawns)}")
    print(f"  TTFT matched:  {n_ttft}/{len(spawns)}")
    print(f"  TFR matched:   {n_tfr}/{len(spawns)}")

    # Write output
    out_path = args.out or (run_dir / "analysis" / "rq2_spawn_metrics.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["spawn_ts", "container", "mac", "lan", "tier", "mode",
                  "action_ts", "action_to_spawn_s",
                  "ttft_s", "tfr_s", "init_time_s", "initial_share", "pool_size"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote {len(rows)} rows -> {out_path}")

    # Quick summary
    for metric_name, col in [("TTFT", "ttft_s"), ("TFR", "tfr_s"),
                              ("Init Time", "init_time_s"),
                              ("Initial Share", "initial_share")]:
        vals = [float(r[col]) for r in rows if r[col]]
        if vals:
            print(f"  {metric_name}: n={len(vals)}  "
                  f"median={np.median(vals):.1f}  "
                  f"mean={np.mean(vals):.1f}  "
                  f"p95={np.percentile(vals, 95):.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
