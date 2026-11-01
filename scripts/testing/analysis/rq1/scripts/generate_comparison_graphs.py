"""Generate RQ1 mode-comparison bar charts.

Produces summary PNGs comparing Push, Poll-5s, Poll-12s, and Poll-30s across:
  - Reaction latency (mean, max, combined)
  - Controller CPU% and RSS
  - Information age (staleness)
  - Timeout rate (overall + per-phase)
  - Average failure rate (dedicated)
  - Decision quality (cross-mode per-phase table + CSV)

Usage:
    python -m source.scripts.testing.analysis.rq1.scripts.generate_comparison_graphs \
        --run-dirs-push <push_1> <push_2> <push_3> \
        --run-dirs-poll5 <poll5_1> <poll5_2> <poll5_3> \
        --run-dirs-poll12 <poll12_1> <poll12_2> <poll12_3> \
        --run-dirs-poll30 <poll30_1> <poll30_2> <poll30_3> \
        --output-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import numpy as np

from ...client_status import is_completed, is_timeout_legacy


def _safe_read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def collect_mode_data(run_dirs: list[Path]) -> dict:
    """Collect aggregated metrics for a set of replicate runs.

    Returns means, per-replicate lists (for scatter dots), std devs,
    and total request / event counts.
    """
    lats_all: list[float] = []          # all reaction events across replicates
    lats_per_run: list[float] = []      # per-run mean reaction latency
    lats_max_per_run: list[float] = []  # per-run max reaction latency
    cpus: list[float] = []              # per-run mean CPU
    rams: list[float] = []              # per-run mean RAM
    stales_per_run: list[float] = []    # per-run max staleness
    timeouts: list[float] = []          # per-run timeout rate (%)
    ep_means: list[float] = []          # per-run weighted mean endpoint latency (s)
    ep_p50s: list[float] = []           # per-run weighted p50 endpoint latency (s)
    ep_p95s: list[float] = []           # per-run weighted p95 endpoint latency (s)
    ep_per_phase_p95: dict[str, list[float]] = {}  # per-phase per-run p95
    ep_per_phase_p50: dict[str, list[float]] = {}  # per-phase per-run p50
    throughputs: list[int] = []          # per-run total request count
    tp_per_phase: dict[str, list[int]] = {}  # per-phase per-run request count
    total_requests: int = 0
    n_reaction_events: int = 0
    per_phase: dict[str, list[float]] = {}
    compute_cpus: list[float] = []    # per-run mean compute CPU%
    storage_cpus: list[float] = []    # per-run mean storage CPU%
    degraded_20s: list[float] = []    # per-run % requests with latency > 20s
    degraded_10s: list[float] = []    # per-run % requests with latency > 10s
    degraded_5s: list[float] = []     # per-run % requests with latency > 5s

    for run_dir in run_dirs:
        # Reaction latency — collect per-event and per-run aggregates
        run_lats: list[float] = []
        for row in _safe_read_csv(run_dir / "analysis" / "rq1_reaction_latency.csv"):
            v = float(row["total_reaction_s"])
            run_lats.append(v)
            lats_all.append(v)
        if run_lats:
            lats_per_run.append(np.mean(run_lats))
            lats_max_per_run.append(np.max(run_lats))
            n_reaction_events += len(run_lats)

        # Controller CPU/RAM — per-run means
        ctrl_rows = _safe_read_csv(run_dir / "controller_stats.csv")
        if ctrl_rows:
            cpus.append(np.mean([float(r.get("cpu_percent", 0) or 0) for r in ctrl_rows]))
            rams.append(np.mean([float(r.get("mem_usage_mb", 0) or 0) for r in ctrl_rows]))

        # Per-node CPU by role — per-run means
        node_rows = _safe_read_csv(run_dir / "per_node_stats.csv")
        if node_rows:
            comp_cpu = [float(r["cpu_percent"]) for r in node_rows if r.get("role", "") == "compute"]
            stor_cpu = [float(r["cpu_percent"]) for r in node_rows if r.get("role", "") == "storage"]
            if comp_cpu:
                compute_cpus.append(np.mean(comp_cpu))
            if stor_cpu:
                storage_cpus.append(np.mean(stor_cpu))

        # Staleness — per-run max
        run_stales: list[float] = []
        for row in _safe_read_csv(run_dir / "analysis" / "rq1_staleness.csv"):
            run_stales.append(float(row["staleness_s"]))
        if run_stales:
            stales_per_run.append(np.max(run_stales))

        # Endpoint latency (user-facing HTTP) — per-run weighted aggregates
        ep_rows = _safe_read_csv(run_dir / "analysis" / "rq1" / "rq1_endpoint_latency.csv")
        if ep_rows:
            total_count = sum(int(r["count"]) for r in ep_rows)
            if total_count > 0:
                ep_means.append(
                    sum(float(r["mean"]) * int(r["count"]) for r in ep_rows) / total_count
                )
                ep_p50s.append(
                    sum(float(r["p50"]) * int(r["count"]) for r in ep_rows) / total_count
                )
                ep_p95s.append(
                    sum(float(r["p95"]) * int(r["count"]) for r in ep_rows) / total_count
                )
            # Per-phase p50 and p95
            for r in ep_rows:
                ph = r["phase"]
                p50 = float(r["p50"])
                p95 = float(r["p95"])
                cnt = int(r["count"])
                if ph not in ep_per_phase_p50:
                    ep_per_phase_p50[ph] = []
                if ph not in ep_per_phase_p95:
                    ep_per_phase_p95[ph] = []
                ep_per_phase_p50[ph].append((p50, cnt))
                ep_per_phase_p95[ph].append((p95, cnt))

        # Timeout rate — per-run
        cr_rows = _safe_read_csv(run_dir / "client_requests.csv")
        if cr_rows:
            total = len(cr_rows)
            total_requests += total
            throughputs.append(total)
            failed = sum(1 for r in cr_rows if is_timeout_legacy(r))
            timeouts.append((failed / total) * 100 if total else 0)
            completed = [r for r in cr_rows if is_completed(r)]
            # Severely degraded: latency > 20s (completed only)
            deg20 = sum(1 for r in completed
                        if float(r.get("latency_s", 0) or 0) > 20)
            degraded_20s.append((deg20 / total) * 100 if total else 0)
            # Moderately degraded: latency > 10s
            deg10 = sum(1 for r in completed
                        if float(r.get("latency_s", 0) or 0) > 10)
            degraded_10s.append((deg10 / total) * 100 if total else 0)
            # Lightly degraded: latency > 5s
            deg5 = sum(1 for r in completed
                       if float(r.get("latency_s", 0) or 0) > 5)
            degraded_5s.append((deg5 / total) * 100 if total else 0)

            # Per-phase per-run
            phase_counts: dict[str, dict[str, int]] = {}
            for r in cr_rows:
                ph = r.get("phase", "unknown")
                if ph not in phase_counts:
                    phase_counts[ph] = {"total": 0, "fail": 0}
                phase_counts[ph]["total"] += 1
                if is_timeout_legacy(r):
                    phase_counts[ph]["fail"] += 1
            for ph, d in phase_counts.items():
                rate = (d["fail"] / d["total"]) * 100 if d["total"] else 0
                per_phase.setdefault(ph, []).append(rate)
                tp_per_phase.setdefault(ph, []).append(d["total"])

    return {
        # means (for bar heights)
        "latency_mean": np.mean(lats_all) if lats_all else 0,
        "latency_max": np.max(lats_all) if lats_all else 0,
        "cpu_mean": np.mean(cpus) if cpus else 0,
        "ram_mean": np.mean(rams) if rams else 0,
        "staleness_max": np.mean(stales_per_run) if stales_per_run else 0,
        "timeout_mean": np.mean(timeouts) if timeouts else 0,
        "degraded_20s_mean": np.mean(degraded_20s) if degraded_20s else 0,
        "degraded_10s_mean": np.mean(degraded_10s) if degraded_10s else 0,
        "degraded_5s_mean": np.mean(degraded_5s) if degraded_5s else 0,
        "ep_mean": np.mean(ep_means) if ep_means else 0,
        "ep_p50_mean": np.mean(ep_p50s) if ep_p50s else 0,
        "ep_p95_mean": np.mean(ep_p95s) if ep_p95s else 0,
        # std devs (for error bars)
        "latency_mean_std": np.std(lats_per_run) if len(lats_per_run) > 1 else 0,
        "latency_max_std": np.std(lats_max_per_run) if len(lats_max_per_run) > 1 else 0,
        "cpu_std": np.std(cpus) if len(cpus) > 1 else 0,
        "ram_std": np.std(rams) if len(rams) > 1 else 0,
        "staleness_max_std": np.std(stales_per_run) if len(stales_per_run) > 1 else 0,
        "timeout_std": np.std(timeouts) if len(timeouts) > 1 else 0,
        "ep_mean_std": np.std(ep_means) if len(ep_means) > 1 else 0,
        "ep_p50_std": np.std(ep_p50s) if len(ep_p50s) > 1 else 0,
        "ep_p95_std": np.std(ep_p95s) if len(ep_p95s) > 1 else 0,
        # per-replicate lists (for scatter dots)
        "latency_values": lats_per_run,
        "latency_max_values": lats_max_per_run,
        "cpu_values": cpus,
        "ram_values": rams,
        "compute_cpu_values": compute_cpus,
        "storage_cpu_values": storage_cpus,
        "staleness_values": stales_per_run,
        "timeout_values": timeouts,
        "throughput_values": throughputs,
        "degraded_20s_values": degraded_20s,
        "degraded_10s_values": degraded_10s,
        "degraded_5s_values": degraded_5s,
        "ep_mean_values": ep_means,
        "ep_p50_values": ep_p50s,
        "ep_p95_values": ep_p95s,
        # per-phase p95 (mean across replicates, weighted by request count)
        "ep_p95_per_phase": {
            ph: np.mean([sum(p95 * c for p95, c in vals) / sum(c for _, c in vals)
                         if sum(c for _, c in vals) > 0 else 0
                         for vals in [ep_per_phase_p95.get(ph, [(0, 1)])]])
            for ph in ep_per_phase_p95
        },
        "ep_p95_per_phase_std": {
            ph: np.std([sum(p95 * c for p95, c in vals) / sum(c for _, c in vals)
                        if sum(c for _, c in vals) > 0 else 0
                        for vals in [ep_per_phase_p95.get(ph, [(0, 1)])]])
            if len(ep_per_phase_p95.get(ph, [])) > 1 else 0
            for ph in ep_per_phase_p95
        },
        # per-phase p50
        "ep_p50_per_phase": {
            ph: np.mean([sum(p50 * c for p50, c in vals) / sum(c for _, c in vals)
                         if sum(c for _, c in vals) > 0 else 0
                         for vals in [ep_per_phase_p50.get(ph, [(0, 1)])]])
            for ph in ep_per_phase_p50
        },
        "ep_p50_per_phase_std": {
            ph: np.std([sum(p50 * c for p50, c in vals) / sum(c for _, c in vals)
                        if sum(c for _, c in vals) > 0 else 0
                        for vals in [ep_per_phase_p50.get(ph, [(0, 1)])]])
            if len(ep_per_phase_p50.get(ph, [])) > 1 else 0
            for ph in ep_per_phase_p50
        },
        # per-phase (mean across replicates)
        "per_phase": {ph: np.mean(rates) for ph, rates in per_phase.items()},
        "per_phase_std": {ph: np.std(rates) if len(rates) > 1 else 0
                          for ph, rates in per_phase.items()},
        "per_phase_values": {ph: rates for ph, rates in per_phase.items()},
        # counts
        "total_requests": total_requests,
        "n_reaction_events": n_reaction_events,
        "n_runs": len(run_dirs),
        # throughput
        "throughput_mean": np.mean(throughputs) if throughputs else 0,
        "throughput_std": np.std(throughputs) if len(throughputs) > 1 else 0,
        "throughput_per_phase": {ph: np.mean(counts) for ph, counts in tp_per_phase.items()},
        "throughput_per_phase_std": {ph: np.std(counts) if len(counts) > 1 else 0
                                      for ph, counts in tp_per_phase.items()},
        # per-role CPU
        "compute_cpu_mean": np.mean(compute_cpus) if compute_cpus else 0,
        "compute_cpu_std": np.std(compute_cpus) if len(compute_cpus) > 1 else 0,
        "storage_cpu_mean": np.mean(storage_cpus) if storage_cpus else 0,
        "storage_cpu_std": np.std(storage_cpus) if len(storage_cpus) > 1 else 0,
    }


# ── Decision quality collection ─────────────────────────────────

DQ_PHASE_ORDER = [
    "baseline", "storage_storm", "tier1_hotspot",
    "cleanup_gap_2", "reverse_hotspot",
    "storage_storm_2", "demand_drop",
]


def _collect_dq_for_mode(run_dirs: list[Path]) -> dict[str, dict]:
    """Read per-run rq1_decision_quality.csv files and aggregate across
    replicates. Returns {phase_name: {breached_pct: [vals], spawns: [vals]}}."""
    phase_data: dict[str, dict] = {}
    for run_dir in run_dirs:
        dq_csv = run_dir / "analysis" / "rq1" / "rq1_decision_quality.csv"
        if not dq_csv.exists():
            continue
        with open(dq_csv) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            ph = r["phase"]
            if ph not in phase_data:
                phase_data[ph] = {"breached_pct": [], "spawns": []}
            tw = int(r.get("total_windows", 0))
            bw = int(r.get("breached_windows", 0))
            sp = int(r.get("spawns_initiated", 0))
            pct = (bw / tw * 100) if tw > 0 else 0.0
            phase_data[ph]["breached_pct"].append(pct)
            phase_data[ph]["spawns"].append(sp)
    # Average across replicates
    result: dict[str, dict] = {}
    for ph, d in phase_data.items():
        result[ph] = {
            "breached_pct": np.mean(d["breached_pct"]) if d["breached_pct"] else 0,
            "spawns": np.mean(d["spawns"]) if d["spawns"] else 0,
        }
    return result


def _plot_decision_quality_table(
    all_dq: list[dict[str, dict]],
    output_dir: Path,
    title_prefix: str = "RQ1 v3",
) -> None:
    """Render a cross-mode decision quality comparison table as PNG.

    Columns: Phase | Push Br% | Push Spwn | Poll-5s Br% | Poll-5s Spwn |
             Poll-12s Br% | Poll-12s Spwn | Poll-30s Br% | Poll-30s Spwn

    Legend is placed directly below the table.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    col_labels = [
        "Phase",
        "Push\nBr%", "Push\nSpwn",
        "Poll-5s\nBr%", "Poll-5s\nSpwn",
        "Poll-12s\nBr%", "Poll-12s\nSpwn",
        "Poll-30s\nBr%", "Poll-30s\nSpwn",
    ]
    cell_text: list[list[str]] = []
    for ph in DQ_PHASE_ORDER:
        row: list[str] = [ph.replace("_", "\n")]
        for dq in all_dq:
            entry = dq.get(ph, {"breached_pct": 0, "spawns": 0})
            br_pct = entry["breached_pct"]
            sp = entry["spawns"]
            row.append(f"{br_pct:.0f}%" if br_pct > 0 else "-")
            row.append(f"{sp:.1f}" if sp > 0 else "-")
        cell_text.append(row)

    n_phases = len(DQ_PHASE_ORDER)
    n_cols = len(col_labels)
    n_rows = n_phases + 1  # +1 for header

    # Larger figure: each row gets ~0.7" height, each col gets ~1.8" width
    fig_w = n_cols * 1.55
    fig_h = n_rows * 0.72 + 1.6  # extra 1.6" for title + legend
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    fig.suptitle(f"{title_prefix} — Decision Quality: Breached Windows & Spawns by Phase / Mode",
                 fontsize=14, fontweight="bold", y=0.96)

    table = ax.table(
        cellText=cell_text,
        colLabels=col_labels,
        loc="upper center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10.5)

    # Scale cells: make them large enough for text
    table.scale(1.0, 2.0)  # double row height for readability

    # Set equal column widths for data columns, wider for phase column
    col_widths = [0.18] + [0.10] * (n_cols - 1)
    for i in range(n_cols):
        for j in range(n_rows):
            cell = table[j, i]
            cell.set_width(col_widths[i])
            cell.set_height(0.08)

    # Highlight phases with any breach activity
    for i, ph in enumerate(DQ_PHASE_ORDER):
        has_breach = any(
            dq.get(ph, {}).get("breached_pct", 0) > 0
            for dq in all_dq
        )
        if has_breach:
            for j in range(len(col_labels)):
                cell = table[i + 1, j]
                cell.set_facecolor("#fff3e0")

    # Legend placed directly below the table
    legend_lines = [
        "Br% = mean(breached_windows / total_windows × 100) across 3 replicates",
        "Spwn = mean(spawns_initiated) across 3 replicates",
        "Orange rows = at least one mode recorded breached windows in that phase",
        "Note: compute nodes spawned in ALL 12 runs; Spwn=0 means spawns occurred in other phases",
    ]
    legend_text = "\n".join(legend_lines)
    fig.text(0.5, 0.03, legend_text, ha="center", fontsize=9,
             style="italic", color="#333333",
             bbox=dict(boxstyle="round,pad=0.4", facecolor="#f5f5f5", edgecolor="#cccccc", alpha=0.8))

    fig.tight_layout(rect=[0.02, 0.10, 0.98, 0.93])
    path = output_dir / "rq1_v13_decision_quality.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"Wrote {path}")


def _write_decision_quality_csv(
    all_dq: list[dict[str, dict]],
    output_dir: Path,
) -> None:
    """Write cross-mode decision quality CSV."""
    path = output_dir / "rq1_v13_decision_quality.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "phase",
            "push_breached_pct", "push_spawns",
            "poll5_breached_pct", "poll5_spawns",
            "poll12_breached_pct", "poll12_spawns",
            "poll30_breached_pct", "poll30_spawns",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for ph in DQ_PHASE_ORDER:
            row: dict = {"phase": ph}
            for i, mode_key in enumerate(["push", "poll5", "poll12", "poll30"]):
                entry = all_dq[i].get(ph, {"breached_pct": 0, "spawns": 0})
                row[f"{mode_key}_breached_pct"] = round(entry["breached_pct"], 1)
                row[f"{mode_key}_spawns"] = round(entry["spawns"], 1)
            w.writerow(row)
    print(f"Wrote {path}")


PHASE_ORDER = [
    "baseline", "storage_storm", "tier1_hotspot",
    "cleanup_gap_2", "reverse_hotspot",
    "storage_storm_2", "demand_drop",
]
MODE_COLORS = ["#2196F3", "#4CAF50", "#FF9800", "#F44336"]
MODE_LABELS = ["Push", "Poll-5s", "Poll-12s", "Poll-30s"]

# ── Style constants ──────────────────────────────────────────────
FIG_SINGLE = (10, 6)
FIG_DOUBLE = (14, 6)
FIG_PER_PHASE = (14, 6)
TITLE_SIZE = 13
LABEL_SIZE = 12
TICK_SIZE = 11
ANNO_SIZE = 11
DOT_SIZE = 50
BAR_ALPHA = 0.75
GRID_ALPHA = 0.25
RNG = np.random.default_rng(42)


def _style_bar_ax(ax, x, modes, ylabel, title):
    """Apply consistent styling to a single bar-chart axis."""
    ax.set_xticks(x)
    ax.set_xticklabels(modes, fontsize=TICK_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _add_scatter_dots(ax, x, data, key):
    """Add per-replicate scatter dots overlaid on bars."""
    for i, d in enumerate(data):
        vals = d.get(key, [])
        if vals and len(vals) > 1:
            jitter = RNG.uniform(-0.14, 0.14, len(vals))
            ax.scatter(
                np.full(len(vals), x[i]) + jitter, vals,
                color="black", s=DOT_SIZE, zorder=5,
                edgecolors="white", linewidth=1,
            )


def _add_bar_labels(ax, x, data, key, fmt, offset):
    """Add value labels above each bar."""
    for i, d in enumerate(data):
        v = d[key]
        ax.text(i, v + offset, fmt.format(v), ha="center",
                fontweight="bold", fontsize=ANNO_SIZE)


def _format_total_requests(data: list[dict]) -> str:
    """Build a subtitle string with total request counts per mode."""
    total_all = sum(d.get("total_requests", 0) for d in data)
    return f"({total_all:,} total requests across all modes, n=3 replicates each)"


def _add_sample_footnote(fig, data: list[dict], label: str, key: str):
    """Add a footnote showing sample size (e.g., reaction events or replicates)."""
    vals = [d.get(key, 0) for d in data]
    if all(v == vals[0] for v in vals):
        footnote = f"n = {vals[0]} {label} per mode"
    else:
        parts = ", ".join(f"{MODE_LABELS[i]}: {vals[i]}" for i in range(len(vals)))
        footnote = f"n ({label}): {parts}"
    fig.text(0.5, 0.01, footnote, ha="center", fontsize=7.5,
             color="#888888", style="italic")



def generate_graphs(
    push_dirs: list[Path],
    poll5_dirs: list[Path],
    poll12_dirs: list[Path],
    poll30_dirs: list[Path],
    output_dir: Path,
    title_prefix: str = "RQ1 v3",
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[generate_comparison_graphs] matplotlib not installed")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    all_dirs = [push_dirs, poll5_dirs, poll12_dirs, poll30_dirs]
    data = [collect_mode_data(dirs) for dirs in all_dirs]
    x = np.arange(len(MODE_LABELS))

    # ── Graph 1a: Reaction Events Detected (count) ───────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Reaction Events Detected",
                  f"{title_prefix} — Reaction Events Detected by Telemetry Mode\n(Poll-30s survivor bias: only detected breaches counted; undetected breaches have no row)")
    ax.bar(x, [d["n_reaction_events"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    # Per-replicate scatter: n_reaction_events is total, so show per-run as label
    for i, d in enumerate(data):
        ax.text(i, d["n_reaction_events"] - 2, f"n={d['n_reaction_events']}",
                ha="center", fontweight="bold", fontsize=ANNO_SIZE + 2, color="white")
    ax.set_ylim(0, max(d["n_reaction_events"] for d in data) + 5)
    _add_sample_footnote(fig, data, "total events across 3 runs", "n_reaction_events")
    plt.tight_layout(rect=[0, 0.06, 1, 0.90])
    fig.savefig(output_dir / "rq1_v13_reaction_latency_mean.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_reaction_latency_mean.png'}")

    # ── Graph 1b: Max Detected Reaction Latency (survivor-aware) ──
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Max Detected Reaction Latency (s)",
                  f"{title_prefix} — Max Detected Reaction Latency\nPoll-30s: 2/3 runs detected only 1–2 breaches; undetected breaches have infinite latency")
    ax.bar(x, [d["latency_max"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "latency_max_values")
    _add_bar_labels(ax, x, data, "latency_max", "{:.1f}s", 3)
    # Add detection count under each bar
    for i, d in enumerate(data):
        ax.text(i, -15, f"({d['n_reaction_events']} events)", ha="center",
                fontsize=ANNO_SIZE - 1, fontstyle="italic", color="#666666")
    _add_sample_footnote(fig, data, "detected reaction events", "n_reaction_events")
    plt.tight_layout(rect=[0, 0.10, 1, 0.88])
    fig.savefig(output_dir / "rq1_v13_reaction_latency_max.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_reaction_latency_max.png'}")
    _add_sample_footnote(fig, data, "reaction events", "n_reaction_events")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_reaction_latency_max.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_reaction_latency_max.png'}")

    # ── Graph 1b2: Overall Throughput ───────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Total Requests",
                  f"{title_prefix} — Throughput by Telemetry Mode")
    ax.bar(x, [d["throughput_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "throughput_values")
    _add_bar_labels(ax, x, data, "throughput_mean", "{:.0f}", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_throughput.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_throughput.png'}")

    # ── Graph 1b3: Per-Phase Throughput ─────────────────────────
    fig, ax = plt.subplots(figsize=FIG_PER_PHASE)
    phase_x = np.arange(len(PHASE_ORDER))
    width = 0.2
    for i, (mode, d) in enumerate(zip(MODE_LABELS, data)):
        values = [d["throughput_per_phase"].get(ph, 0) for ph in PHASE_ORDER]
        stds = [d.get("throughput_per_phase_std", {}).get(ph, 0) for ph in PHASE_ORDER]
        yerr_lower = [min(s, v) for v, s in zip(values, stds)]
        ax.bar(phase_x + i * width, values, width, label=mode,
               color=MODE_COLORS[i], edgecolor="black", alpha=BAR_ALPHA,
               yerr=(yerr_lower, stds), capsize=3, error_kw={"linewidth": 1.2})
    ax.set_xticks(phase_x + width * 1.5)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER],
                       fontsize=TICK_SIZE - 1)
    ax.set_ylabel("Requests per Phase", fontsize=LABEL_SIZE)
    ax.set_title(f"{title_prefix} — Per-Phase Throughput by Telemetry Mode",
                 fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=TICK_SIZE - 1, framealpha=0.8)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.94])
    fig.savefig(output_dir / "rq1_v13_throughput_per_phase.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_throughput_per_phase.png'}")

    # ── Graph 1c: Median Endpoint Latency (p50, user-facing) ────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Median Endpoint Latency — p50 (s)",
                  f"{title_prefix} — Median Endpoint Latency (p50) by Telemetry Mode")
    ax.bar(x, [d["ep_p50_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "ep_p50_values")
    _add_bar_labels(ax, x, data, "ep_p50_mean", "{:.3f}s", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_endpoint_latency_p50.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_endpoint_latency_p50.png'}")

    # ── Graph 1d: p95 Endpoint Latency (user-facing HTTP tail) ───
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "p95 Endpoint Latency (s)",
                  f"{title_prefix} — p95 Endpoint Latency by Telemetry Mode")
    ax.bar(x, [d["ep_p95_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "ep_p95_values")
    _add_bar_labels(ax, x, data, "ep_p95_mean", "{:.2f}s", 0.05)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_endpoint_latency_p95.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_endpoint_latency_p95.png'}")

    # ── Graph 1e: Per-Phase p95 Endpoint Latency ─────────────────
    fig, ax = plt.subplots(figsize=FIG_PER_PHASE)
    phase_x = np.arange(len(PHASE_ORDER))
    width = 0.2
    for i, (mode, d) in enumerate(zip(MODE_LABELS, data)):
        values = [d["ep_p95_per_phase"].get(ph, 0) for ph in PHASE_ORDER]
        stds = [d.get("ep_p95_per_phase_std", {}).get(ph, 0) for ph in PHASE_ORDER]
        yerr_lower = [min(s, v) for v, s in zip(values, stds)]
        ax.bar(phase_x + i * width, values, width, label=mode,
               color=MODE_COLORS[i], edgecolor="black", alpha=BAR_ALPHA,
               yerr=(yerr_lower, stds), capsize=3, error_kw={"linewidth": 1.2})
    ax.set_xticks(phase_x + width * 1.5)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER],
                       fontsize=TICK_SIZE - 1)
    ax.set_ylabel("p95 Endpoint Latency (s)", fontsize=LABEL_SIZE)
    ax.set_title(f"{title_prefix} — Per-Phase p95 Endpoint Latency by Telemetry Mode",
                 fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=TICK_SIZE - 1, framealpha=0.8)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.94])
    fig.savefig(output_dir / "rq1_v13_endpoint_latency_per_phase_p95.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_endpoint_latency_per_phase_p95.png'}")

    # ── Graph 1f: Per-Phase p50 Endpoint Latency ─────────────────
    fig, ax = plt.subplots(figsize=FIG_PER_PHASE)
    for i, (mode, d) in enumerate(zip(MODE_LABELS, data)):
        values = [d["ep_p50_per_phase"].get(ph, 0) for ph in PHASE_ORDER]
        stds = [d.get("ep_p50_per_phase_std", {}).get(ph, 0) for ph in PHASE_ORDER]
        yerr_lower = [min(s, v) for v, s in zip(values, stds)]
        ax.bar(phase_x + i * width, values, width, label=mode,
               color=MODE_COLORS[i], edgecolor="black", alpha=BAR_ALPHA,
               yerr=(yerr_lower, stds), capsize=3, error_kw={"linewidth": 1.2})
    ax.set_xticks(phase_x + width * 1.5)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER],
                       fontsize=TICK_SIZE - 1)
    ax.set_ylabel("p50 Endpoint Latency (s)", fontsize=LABEL_SIZE)
    ax.set_title(f"{title_prefix} — Per-Phase p50 Endpoint Latency by Telemetry Mode",
                 fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=TICK_SIZE - 1, framealpha=0.8)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.94])
    fig.savefig(output_dir / "rq1_v13_endpoint_latency_per_phase_p50.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_endpoint_latency_per_phase_p50.png'}")

    # ── Graph 2: Controller Overhead ─────────────────────────────
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIG_DOUBLE)
    _style_bar_ax(ax1, x, MODE_LABELS, "CPU %",
                  f"{title_prefix} — Avg Controller CPU by Telemetry Mode")
    ax1.bar(x, [d["cpu_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax1, x, data, "cpu_values")
    _add_bar_labels(ax1, x, data, "cpu_mean", "{:.1f}%", 0.15)
    _style_bar_ax(ax2, x, MODE_LABELS, "RSS (MB)",
                  f"{title_prefix} — Avg Controller RAM by Telemetry Mode")
    ax2.bar(x, [d["ram_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax2, x, data, "ram_values")
    _add_bar_labels(ax2, x, data, "ram_mean", "{:.0f} MB", 2)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_overhead_comparison.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_overhead_comparison.png'}")

    # ── Graph 2b: Compute Node CPU ──────────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "CPU %",
                  f"{title_prefix} — Avg Compute Node CPU by Telemetry Mode")
    ax.bar(x, [d["compute_cpu_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "compute_cpu_values")
    _add_bar_labels(ax, x, data, "compute_cpu_mean", "{:.1f}%", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_compute_cpu.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_compute_cpu.png'}")

    # ── Graph 2c: Storage Node CPU ──────────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "CPU %",
                  f"{title_prefix} — Avg Storage Node CPU by Telemetry Mode")
    ax.bar(x, [d["storage_cpu_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "storage_cpu_values")
    _add_bar_labels(ax, x, data, "storage_cpu_mean", "{:.1f}%", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_storage_cpu.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_storage_cpu.png'}")

    # ── Graph 3: Staleness ───────────────────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Max Staleness (s)",
                  f"{title_prefix} — Max Information Age (Staleness) by Telemetry Mode")
    ax.bar(x, [d["staleness_max"] for d in data], color=MODE_COLORS,
           edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "staleness_values")
    _add_bar_labels(ax, x, data, "staleness_max", "{:.1f}s", 0.3)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_staleness_comparison.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_staleness_comparison.png'}")

    # ── Graph 4: Timeout Rate (merged — was two duplicate graphs) ─
    total_req_str = _format_total_requests(data)
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Mean Timeout Rate (%)",
                  f"{title_prefix} — Mean Timeout Rate by Telemetry Mode\n{total_req_str}")
    ax.bar(x, [d["timeout_mean"] for d in data], color=MODE_COLORS,
           edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "timeout_values")
    _add_bar_labels(ax, x, data, "timeout_mean", "{:.1f}%", 2)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.97])
    fig.savefig(output_dir / "rq1_v13_timeout_comparison.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_timeout_comparison.png'}")
    # ── Graph 4b: Severely Degraded Requests (latency > 20s) ───
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "% Requests with Latency > 20s",
                  f"{title_prefix} — Severely Degraded Requests (latency > 20s) by Telemetry Mode")
    ax.bar(x, [d["degraded_20s_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "degraded_20s_values")
    _add_bar_labels(ax, x, data, "degraded_20s_mean", "{:.1f}%", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_degraded_20s.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_degraded_20s.png'}")

    # ── Graph 4c: Moderately Degraded Requests (latency > 10s) ──
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "% Requests with Latency > 10s",
                  f"{title_prefix} — Moderately Degraded Requests (latency > 10s) by Telemetry Mode")
    ax.bar(x, [d["degraded_10s_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "degraded_10s_values")
    _add_bar_labels(ax, x, data, "degraded_10s_mean", "{:.1f}%", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_degraded_10s.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_degraded_10s.png'}")

    # ── Graph 4d: Lightly Degraded Requests (latency > 5s) ──────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "% Requests with Latency > 5s",
                  f"{title_prefix} — Degraded Requests (latency > 5s) by Telemetry Mode")
    ax.bar(x, [d["degraded_5s_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "degraded_5s_values")
    _add_bar_labels(ax, x, data, "degraded_5s_mean", "{:.1f}%", 0.02)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "rq1_v13_degraded_5s.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_degraded_5s.png'}")

    # ── Graph 5: Per-Phase Timeout ───────────────────────────────
    fig, ax = plt.subplots(figsize=FIG_PER_PHASE)
    phase_x = np.arange(len(PHASE_ORDER))
    width = 0.2
    for i, (mode, d) in enumerate(zip(MODE_LABELS, data)):
        values = [d["per_phase"].get(ph, 0) for ph in PHASE_ORDER]
        stds = [d.get("per_phase_std", {}).get(ph, 0) for ph in PHASE_ORDER]
        # Clip error bars: don't extend below 0
        yerr_lower = [min(s, v) for v, s in zip(values, stds)]
        yerr_upper = stds
        bars = ax.bar(phase_x + i * width, values, width, label=mode,
                      color=MODE_COLORS[i], edgecolor="black", alpha=BAR_ALPHA,
                      yerr=(yerr_lower, yerr_upper), capsize=3, error_kw={"linewidth": 1.2})
    ax.set_xticks(phase_x + width * 1.5)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER],
                       fontsize=TICK_SIZE - 1)
    ax.set_ylabel("Timeout Rate (%)", fontsize=LABEL_SIZE)
    ax.set_title(f"{title_prefix} — Per-Phase Timeout Rate by Telemetry Mode\n{total_req_str}",
                 fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=TICK_SIZE - 1, framealpha=0.8)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.94])
    fig.savefig(output_dir / "rq1_v13_per_phase_timeout.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'rq1_v13_per_phase_timeout.png'}")

    # ── Graph 6 removed (was duplicate of Graph 4) ──────────────
    all_dq = [_collect_dq_for_mode(dirs) for dirs in all_dirs]
    _plot_decision_quality_table(all_dq, output_dir, title_prefix)
    _write_decision_quality_csv(all_dq, output_dir)

    print("\nAll comparison graphs generated.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate RQ1 mode-comparison graphs")
    parser.add_argument("--run-dirs-push", nargs="*", default=[], dest="push",
                        help="Push mode run directories")
    parser.add_argument("--run-dirs-poll5", nargs="*", default=[], dest="poll5",
                        help="Poll-5s mode run directories")
    parser.add_argument("--run-dirs-poll12", nargs="*", default=[], dest="poll12",
                        help="Poll-12s mode run directories")
    parser.add_argument("--run-dirs-poll30", nargs="*", default=[], dest="poll30",
                        help="Poll-30s mode run directories")
    parser.add_argument("--output-dir", required=True, dest="output",
                        help="Output directory for PNGs")
    parser.add_argument("--title-prefix", default="RQ1 v3", dest="title_prefix",
                        help="Prefix for graph titles (default: 'RQ1 v3')")
    args = parser.parse_args()

    generate_graphs(
        push_dirs=[Path(d) for d in args.push],
        poll5_dirs=[Path(d) for d in args.poll5],
        poll12_dirs=[Path(d) for d in args.poll12],
        poll30_dirs=[Path(d) for d in args.poll30],
        output_dir=Path(args.output),
        title_prefix=args.title_prefix,
    )


if __name__ == "__main__":
    main()
