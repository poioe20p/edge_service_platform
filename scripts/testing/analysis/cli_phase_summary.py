"""cli_phase_summary — per-phase aggregated charts for a run directory.

Produces <run_dir>/analysis/phase_summary.png with three panels:
  - latency percentiles by phase (mean, p50, p95, p99 grouped bars)
  - node counts by type per phase (compute, storage, selective grouped bars)
  - per-LAN p95 latency by phase (LAN1 vs LAN2 side-by-side bars)

Usage:
    python -m source.scripts.testing.analysis.cli_phase_summary --run-dir <dir>
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _percentile(sorted_data: list[float], p: float) -> float:
    """Linear interpolation percentile."""
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_data[0]
    rank = p / 100.0 * (n - 1)
    lo = int(rank)
    hi = lo + 1
    frac = rank - lo
    if hi >= n:
        return sorted_data[-1]
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def run(run_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[cli_phase_summary] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .simple_metrics import container_role, is_failure

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    phase_order = [p.name for p in r.phases]
    if not phase_order:
        print("[cli_phase_summary] no phases found — skipping")
        return

    from .client_status import is_completed

    # ── Build per-phase latency percentiles ──────────────────────────────
    latencies_by_phase: dict[str, list[float]] = defaultdict(list)
    for row in r.all_client_rows:
        if not is_completed(row):
            continue
        phase = str(row.get("phase", "unknown"))
        lat_s = _safe_float(row.get("latency_s"), 0.0)
        latencies_by_phase[phase].append(lat_s * 1000.0)

    # ── Build per-phase LAN latency ──────────────────────────────────────
    latencies_by_phase_lan: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in r.all_client_rows:
        if not is_completed(row):
            continue
        phase = str(row.get("phase", "unknown"))
        lan = str(row.get("client_lan", "unknown"))
        lat_s = _safe_float(row.get("latency_s"), 0.0)
        latencies_by_phase_lan[phase][lan].append(lat_s * 1000.0)

    # ── Build per-phase max node counts by type ──────────────────────────
    # From container_events.csv: track max concurrent per phase
    node_max_by_phase: dict[str, dict[str, int]] = defaultdict(lambda: {"compute": 0, "storage": 0, "selective": 0})
    from .simple_metrics import parse_iso_ts

    # For each phase, find which container_events fall within its time window
    origin_ts = r.t0
    phase_windows: dict[str, tuple[float, float]] = {}
    t_current = origin_ts
    for phase_spec in r.phases:
        phase_windows[phase_spec.name] = (t_current, t_current + phase_spec.duration_s)
        t_current += phase_spec.duration_s

    for phase_name, (p_start, p_end) in phase_windows.items():
        running: set[str] = set()
        role_of: dict[str, str] = {}

        ordered = sorted(
            r.container_event_rows,
            key=lambda row: (
                parse_iso_ts(row.get("timestamp_iso")),
                _safe_float(row.get("monotonic_s"), 0.0),
            ),
        )

        # Walk events up to phase end, tracking running set
        for row in ordered:
            ts = parse_iso_ts(row.get("timestamp_iso"))
            mono = _safe_float(row.get("monotonic_s"), 0.0)
            # Resolve event time relative to origin
            if ts > 0:
                t = ts
            else:
                t = origin_ts + mono
            if t > p_end:
                break

            name = str(row.get("container", ""))
            role = container_role(name)
            if role is None:
                continue

            event = str(row.get("event", "")).lower()
            state = str(row.get("state", "")).lower()

            if event == "removed" or state != "running":
                running.discard(name)
                role_of.pop(name, None)
            else:
                running.add(name)
                role_of[name] = role

            if p_start <= t <= p_end:
                counts = {"compute": 0, "storage": 0, "selective": 0}
                for cname in running:
                    crole = role_of.get(cname)
                    if crole:
                        counts[crole] += 1
                current_max = node_max_by_phase[phase_name]
                current_max["compute"] = max(current_max["compute"], counts["compute"])
                current_max["storage"] = max(current_max["storage"], counts["storage"])
                current_max["selective"] = max(current_max["selective"], counts["selective"])

    # ── Plot ────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 3, figsize=(20, 7))
    fig.suptitle(f"Phase summary — {run_dir.name}", fontsize=13)

    # Consistent phase ordering
    phases_present = [p for p in phase_order if p in latencies_by_phase]
    x = np.arange(len(phases_present))
    bar_width = 0.18

    # ── Panel 0: Latency percentiles ────────────────────────────────────
    ax = axes[0]
    for i, (label, color, offset) in enumerate([
        ("mean", "#1a7abf", -1.5),
        ("p50", "#4da6d9", -0.5),
        ("p95", "#bf5a1a", 0.5),
        ("p99", "#8b1a8b", 1.5),
    ]):
        values = []
        for pname in phases_present:
            lats = sorted(latencies_by_phase.get(pname, []))
            if label == "mean":
                val = sum(lats) / len(lats) if lats else 0.0
            elif label == "p50":
                val = _percentile(lats, 50)
            elif label == "p95":
                val = _percentile(lats, 95)
            else:  # p99
                val = _percentile(lats, 99)
            values.append(val)
        ax.bar(x + offset * bar_width, values, bar_width, color=color, label=label, alpha=0.85, edgecolor="black", linewidth=0.5)

    ax.set_ylabel("ms")
    ax.set_title("Request latency percentiles by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phases_present, rotation=45, ha="right", fontsize=8)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    # ── Panel 1: Node counts by type per phase ──────────────────────────
    ax = axes[1]
    for i, (ntype, color, offset) in enumerate([
        ("compute", "#1a7abf", -1.0),
        ("storage", "#bf5a1a", 0.0),
        ("selective", "#1abf4a", 1.0),
    ]):
        values = [node_max_by_phase.get(p, {}).get(ntype, 0) for p in phases_present]
        ax.bar(x + offset * bar_width, values, bar_width, color=color, label=ntype, alpha=0.85, edgecolor="black", linewidth=0.5)

    ax.set_ylabel("nodes")
    ax.set_title("Nodes by type per phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phases_present, rotation=45, ha="right", fontsize=8)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    # ── Panel 2: Per-LAN p95 latency by phase ───────────────────────────
    ax = axes[2]
    lans_seen: set[str] = set()
    for lan_map in latencies_by_phase_lan.values():
        lans_seen.update(lan_map.keys())
    lans_sorted = sorted(lans_seen)
    lan_colors = {"lan1": "#1a7abf", "lan2": "#bf5a1a"}
    lan_offsets = {lan: (i - (len(lans_sorted) - 1) / 2.0) * bar_width for i, lan in enumerate(lans_sorted)}

    for lan in lans_sorted:
        values = []
        for pname in phases_present:
            lats = sorted(latencies_by_phase_lan.get(pname, {}).get(lan, []))
            val = _percentile(lats, 95)
            values.append(val)
        color = lan_colors.get(lan, "#888888")
        ax.bar(x + lan_offsets[lan], values, bar_width, color=color, label=lan.upper(), alpha=0.85, edgecolor="black", linewidth=0.5)

    ax.set_ylabel("p95 ms")
    ax.set_title("Per-LAN p95 latency by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phases_present, rotation=45, ha="right", fontsize=8)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    out_path = out_dir / "phase_summary.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_phase_summary] wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
