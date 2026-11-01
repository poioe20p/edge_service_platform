"""v7 Test A — Push vs Poll-30s cross-mode comparison graphs.

Mirrors the canonical generate_comparison_graphs.py structure but for 2 modes.
Produces: reaction latency (mean/max/combined), controller overhead,
staleness, timeout rate, per-phase timeout, decision quality table.
All graphs include per-replicate scatter dots for variance.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

from ...client_status import is_timeout_legacy

# ── Config ───────────────────────────────────────────────────────
MODE_LABELS = ["Push", "Poll-30s"]
MODE_COLORS = ["#2196F3", "#F44336"]
PHASE_ORDER = [
    "baseline", "storage_storm", "tier1_hotspot",
    "inter_hotspot_cooldown", "reverse_hotspot",
    "compute_spike", "demand_drop",
]
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

# ── Helpers ──────────────────────────────────────────────────────

def _safe_read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _style_bar_ax(ax, x, modes, ylabel, title):
    ax.set_xticks(x)
    ax.set_xticklabels(modes, fontsize=TICK_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _add_scatter_dots(ax, x, data, key):
    for i, d in enumerate(data):
        vals = d.get(key, [])
        if vals and len(vals) >= 1:
            jitter = RNG.uniform(-0.14, 0.14, len(vals))
            ax.scatter(
                np.full(len(vals), x[i]) + jitter, vals,
                color="black", s=DOT_SIZE, zorder=5,
                edgecolors="white", linewidth=1,
            )


def _add_sample_footnote(fig, data, label, key):
    vals = [d.get(key, 0) for d in data]
    if all(v == vals[0] for v in vals):
        footnote = f"n = {vals[0]} {label} per mode"
    else:
        parts = ", ".join(f"{MODE_LABELS[i]}: {vals[i]}" for i in range(len(vals)))
        footnote = f"n ({label}): {parts}"
    fig.text(0.5, 0.01, footnote, ha="center", fontsize=7.5,
             color="#888888", style="italic")


# ── Data Collection ──────────────────────────────────────────────

def collect_mode_data(run_dirs: list[Path]) -> dict:
    lats_all = []
    lats_per_run = []
    lats_max_per_run = []
    cpus = []
    rams = []
    stales_per_run = []
    timeouts = []
    total_requests = 0
    n_reaction_events = 0
    per_phase: dict[str, list[float]] = {}

    for run_dir in run_dirs:
        # Reaction latency
        run_lats = []
        for row in _safe_read_csv(run_dir / "analysis" / "rq1_reaction_latency.csv"):
            v = float(row["total_reaction_s"])
            run_lats.append(v)
            lats_all.append(v)
        if run_lats:
            lats_per_run.append(np.mean(run_lats))
            lats_max_per_run.append(np.max(run_lats))
            n_reaction_events += len(run_lats)

        # Controller CPU/RAM
        ctrl_rows = _safe_read_csv(run_dir / "controller_stats.csv")
        if ctrl_rows:
            cpus.append(np.mean([float(r.get("cpu_percent", 0) or 0) for r in ctrl_rows]))
            rams.append(np.mean([float(r.get("mem_usage_mb", 0) or 0) for r in ctrl_rows]))

        # Staleness
        run_stales = []
        for row in _safe_read_csv(run_dir / "analysis" / "rq1_staleness.csv"):
            run_stales.append(float(row["staleness_s"]))
        if run_stales:
            stales_per_run.append(np.max(run_stales))

        # Timeout rate
        cr_rows = _safe_read_csv(run_dir / "client_requests.csv")
        if cr_rows:
            total = len(cr_rows)
            total_requests += total
            failed = sum(1 for r in cr_rows if is_timeout_legacy(r))
            timeouts.append((failed / total) * 100 if total else 0)

            # Per-phase
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

    return {
        "latency_mean": np.mean(lats_all) if lats_all else 0,
        "latency_max": np.max(lats_all) if lats_all else 0,
        "cpu_mean": np.mean(cpus) if cpus else 0,
        "ram_mean": np.mean(rams) if rams else 0,
        "staleness_max": np.mean(stales_per_run) if stales_per_run else 0,
        "timeout_mean": np.mean(timeouts) if timeouts else 0,
        "latency_mean_std": np.std(lats_per_run) if len(lats_per_run) > 1 else 0,
        "latency_max_std": np.std(lats_max_per_run) if len(lats_max_per_run) > 1 else 0,
        "cpu_std": np.std(cpus) if len(cpus) > 1 else 0,
        "ram_std": np.std(rams) if len(rams) > 1 else 0,
        "staleness_max_std": np.std(stales_per_run) if len(stales_per_run) > 1 else 0,
        "timeout_std": np.std(timeouts) if len(timeouts) > 1 else 0,
        "latency_values": lats_per_run,
        "latency_max_values": lats_max_per_run,
        "cpu_values": cpus,
        "ram_values": rams,
        "staleness_values": stales_per_run,
        "timeout_values": timeouts,
        "per_phase": {ph: np.mean(rates) for ph, rates in per_phase.items()},
        "per_phase_std": {ph: np.std(rates) if len(rates) > 1 else 0
                          for ph, rates in per_phase.items()},
        "per_phase_values": {ph: rates for ph, rates in per_phase.items()},
        "total_requests": total_requests,
        "n_reaction_events": n_reaction_events,
        "n_runs": len(run_dirs),
    }


# ── Decision Quality ─────────────────────────────────────────────

def _collect_dq_for_mode(run_dirs: list[Path]) -> dict[str, dict]:
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
    result: dict[str, dict] = {}
    for ph, d in phase_data.items():
        result[ph] = {
            "breached_pct": np.mean(d["breached_pct"]) if d["breached_pct"] else 0,
            "spawns": np.mean(d["spawns"]) if d["spawns"] else 0,
        }
    return result


def _plot_decision_quality_table(all_dq: list[dict[str, dict]], output_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Include cleanup_gap phases for completeness
    v7_phase_order = [
        "baseline", "storage_storm", "cleanup_gap_1", "tier1_hotspot",
        "inter_hotspot_cooldown", "reverse_hotspot", "cleanup_gap_2",
        "compute_spike", "demand_drop",
    ]
    col_labels = ["Phase", "Push\nBr%", "Push\nSpwn", "Poll-30s\nBr%", "Poll-30s\nSpwn"]
    cell_text: list[list[str]] = []
    for ph in v7_phase_order:
        row = [ph.replace("_", "\n")]
        for dq in all_dq:
            entry = dq.get(ph, {"breached_pct": 0, "spawns": 0})
            br_pct = entry["breached_pct"]
            sp = entry["spawns"]
            row.append(f"{br_pct:.0f}%" if br_pct > 0 else "-")
            row.append(f"{sp:.1f}" if sp > 0 else "-")
        cell_text.append(row)

    n_phases = len(v7_phase_order)
    n_cols = len(col_labels)
    n_rows = n_phases + 1
    fig_w = n_cols * 1.55
    fig_h = n_rows * 0.72 + 1.6
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    fig.suptitle("v7 Test A — Decision Quality: Breached Windows & Spawns by Phase",
                 fontsize=14, fontweight="bold", y=0.96)

    table = ax.table(cellText=cell_text, colLabels=col_labels, loc="upper center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10.5)
    table.scale(1.0, 2.0)

    col_widths = [0.18] + [0.12] * (n_cols - 1)
    for i in range(n_cols):
        for j in range(n_rows):
            cell = table[j, i]
            cell.set_width(col_widths[i])
            cell.set_height(0.08)

    for i, ph in enumerate(v7_phase_order):
        has_breach = any(
            dq.get(ph, {}).get("breached_pct", 0) > 0 for dq in all_dq
        )
        if has_breach:
            for j in range(len(col_labels)):
                cell = table[i + 1, j]
                cell.set_facecolor("#fff3e0")

    legend_lines = [
        "Br% = mean(breached_windows / total_windows x 100) across replicates",
        "Spwn = mean(spawns_initiated) across replicates",
        "Orange rows = at least one mode recorded breached windows in that phase",
    ]
    legend_text = "\n".join(legend_lines)
    fig.text(0.5, 0.03, legend_text, ha="center", fontsize=9,
             style="italic", color="#333333",
             bbox=dict(boxstyle="round,pad=0.4", facecolor="#f5f5f5", edgecolor="#cccccc", alpha=0.8))
    fig.tight_layout(rect=[0.02, 0.10, 0.98, 0.93])
    path = output_dir / "v7_decision_quality.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"Wrote {path}")


def _write_decision_quality_csv(all_dq: list[dict[str, dict]], output_dir: Path) -> None:
    path = output_dir / "v7_decision_quality.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["phase", "push_breached_pct", "push_spawns",
                                           "poll30_breached_pct", "poll30_spawns"])
        w.writeheader()
        for ph in PHASE_ORDER:
            row = {"phase": ph}
            for i, mode_key in enumerate(["push", "poll30"]):
                entry = all_dq[i].get(ph, {"breached_pct": 0, "spawns": 0})
                row[f"{mode_key}_breached_pct"] = round(entry["breached_pct"], 1)
                row[f"{mode_key}_spawns"] = round(entry["spawns"], 1)
            w.writerow(row)
    print(f"Wrote {path}")


# ── Main Graph Generation ────────────────────────────────────────

def generate_graphs(push_dirs: list[Path], poll30_dirs: list[Path], output_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[v7_comparison] matplotlib not installed")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    data = [collect_mode_data(push_dirs), collect_mode_data(poll30_dirs)]
    x = np.arange(len(MODE_LABELS))

    # Print summary
    for i, mode in enumerate(MODE_LABELS):
        d = data[i]
        print(f"{mode}: {d['total_requests']:,} reqs, {d['timeout_mean']:.1f}% timeout "
              f"(+-{d['timeout_std']:.1f}%), {d['n_reaction_events']} reaction events, "
              f"n={d['n_runs']} runs")

    # ── Graph 1a: Mean Reaction Latency ──────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Mean Reaction Latency (s)",
                  "v7 Test A — Mean Reaction Latency: Push vs Poll-30s")
    ax.bar(x, [d["latency_mean"] for d in data], color=MODE_COLORS,
           edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "latency_values")
    for i, d in enumerate(data):
        ax.text(i, d["latency_mean"] + 2, f'{d["latency_mean"]:.1f}s',
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    _add_sample_footnote(fig, data, "reaction events", "n_reaction_events")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "v7_latency_mean.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_latency_mean.png'}")

    # ── Graph 1b: Max Reaction Latency ───────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Max Reaction Latency (s)",
                  "v7 Test A — Max Reaction Latency: Push vs Poll-30s")
    ax.bar(x, [d["latency_max"] for d in data], color=MODE_COLORS,
           edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "latency_max_values")
    for i, d in enumerate(data):
        ax.text(i, d["latency_max"] + 3, f'{d["latency_max"]:.1f}s',
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    _add_sample_footnote(fig, data, "reaction events", "n_reaction_events")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "v7_latency_max.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_latency_max.png'}")

    # ── Graph 1c: Reaction Latency Combined ──────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    w_bar = 0.35
    ax.bar(x - w_bar/2, [d["latency_mean"] for d in data], w_bar, label="Mean",
           color="#90CAF9", edgecolor="black", alpha=BAR_ALPHA)
    ax.bar(x + w_bar/2, [d["latency_max"] for d in data], w_bar, label="Max",
           color="#1565C0", edgecolor="black", alpha=BAR_ALPHA)
    _style_bar_ax(ax, x, MODE_LABELS, "Reaction Latency (s)",
                  "v7 Test A — Reaction Latency by Telemetry Mode")
    ax.legend(fontsize=TICK_SIZE - 1)
    for i in range(len(data)):
        ax.text(i - w_bar/2, data[i]["latency_mean"] + 1, f'{data[i]["latency_mean"]:.0f}s',
                ha="center", fontsize=ANNO_SIZE - 2, fontweight="bold", color="#1565C0")
        ax.text(i + w_bar/2, data[i]["latency_max"] + 1, f'{data[i]["latency_max"]:.0f}s',
                ha="center", fontsize=ANNO_SIZE - 2, fontweight="bold", color="#0D47A1")
    _add_sample_footnote(fig, data, "reaction events", "n_reaction_events")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "v7_reaction_latency_combined.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_reaction_latency_combined.png'}")

    # ── Graph 2: Controller Overhead ─────────────────────────────
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIG_DOUBLE)
    _style_bar_ax(ax1, x, MODE_LABELS, "CPU %",
                  "v7 Test A — Avg Controller CPU: Push vs Poll-30s")
    ax1.bar(x, [d["cpu_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax1, x, data, "cpu_values")
    for i, d in enumerate(data):
        ax1.text(i, d["cpu_mean"] + 0.15, f'{d["cpu_mean"]:.1f}%',
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    _style_bar_ax(ax2, x, MODE_LABELS, "RSS (MB)",
                  "v7 Test A — Avg Controller RAM: Push vs Poll-30s")
    ax2.bar(x, [d["ram_mean"] for d in data], color=MODE_COLORS,
            edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax2, x, data, "ram_values")
    for i, d in enumerate(data):
        ax2.text(i, d["ram_mean"] + 2, f'{d["ram_mean"]:.0f} MB',
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "v7_overhead_comparison.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_overhead_comparison.png'}")

    # ── Graph 3: Max Staleness ───────────────────────────────────
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Max Staleness (s)",
                  "v7 Test A — Max Information Age (Staleness): Push vs Poll-30s")
    ax.bar(x, [d["staleness_max"] for d in data], color=MODE_COLORS,
           edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "staleness_values")
    for i, d in enumerate(data):
        ax.text(i, d["staleness_max"] + 0.3, f'{d["staleness_max"]:.1f}s',
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(output_dir / "v7_staleness_comparison.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_staleness_comparison.png'}")

    # ── Graph 4: Timeout Rate overall ────────────────────────────
    total_req_str = f"({sum(d['total_requests'] for d in data):,} total requests)"
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _style_bar_ax(ax, x, MODE_LABELS, "Timeout Rate (%)",
                  f"v7 Test A — Timeout Rate: Push vs Poll-30s\n{total_req_str}")
    ax.bar(x, [d["timeout_mean"] for d in data], color=MODE_COLORS,
           edgecolor="black", alpha=BAR_ALPHA)
    _add_scatter_dots(ax, x, data, "timeout_values")
    for i, d in enumerate(data):
        std_str = f"\n+-{d['timeout_std']:.1f}%" if d['timeout_std'] > 0 else ""
        ax.text(i, d["timeout_mean"] + 2, f'{d["timeout_mean"]:.1f}%{std_str}',
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.97])
    fig.savefig(output_dir / "v7_timeout_comparison.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_timeout_comparison.png'}")

    # ── Graph 5: Per-Phase Timeout Rate ──────────────────────────
    fig, ax = plt.subplots(figsize=FIG_PER_PHASE)
    phase_x = np.arange(len(PHASE_ORDER))
    width = 0.35
    for i, (mode, d) in enumerate(zip(MODE_LABELS, data)):
        values = [d["per_phase"].get(ph, 0) for ph in PHASE_ORDER]
        stds = [d.get("per_phase_std", {}).get(ph, 0) for ph in PHASE_ORDER]
        ax.bar(phase_x + i * width, values, width, label=mode,
               color=MODE_COLORS[i], edgecolor="black", alpha=BAR_ALPHA,
               yerr=stds, capsize=3, error_kw={"linewidth": 1.2})
        # Scatter dots per phase
        for j, ph in enumerate(PHASE_ORDER):
            vals = d.get("per_phase_values", {}).get(ph, [])
            if vals and len(vals) >= 1:
                jitter = RNG.uniform(-0.06, 0.06, len(vals))
                ax.scatter(np.full(len(vals), phase_x[j] + i * width) + jitter,
                          vals, color="black", s=30, zorder=5,
                          edgecolors="white", linewidth=0.8)
    ax.set_xticks(phase_x + width / 2)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER],
                       fontsize=TICK_SIZE - 1)
    ax.set_ylabel("Timeout Rate (%)", fontsize=LABEL_SIZE)
    ax.set_title(f"v7 Test A — Per-Phase Timeout Rate\n{total_req_str}",
                 fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=TICK_SIZE - 1, framealpha=0.8)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    _add_sample_footnote(fig, data, "replicates", "n_runs")
    plt.tight_layout(rect=[0, 0.06, 1, 0.94])
    fig.savefig(output_dir / "v7_per_phase_timeout.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {output_dir / 'v7_per_phase_timeout.png'}")

    # ── Graph 6: Decision Quality ────────────────────────────────
    all_dq = [_collect_dq_for_mode(push_dirs), _collect_dq_for_mode(poll30_dirs)]
    _plot_decision_quality_table(all_dq, output_dir)
    _write_decision_quality_csv(all_dq, output_dir)

    print("\nAll v7 comparison graphs generated.")


def main() -> None:
    parser = argparse.ArgumentParser(description="v7 Test A Push vs Poll-30s comparison graphs")
    parser.add_argument("--push-dirs", nargs="+", required=True,
                        help="Push mode run directories")
    parser.add_argument("--poll30-dirs", nargs="+", required=True,
                        help="Poll-30s mode run directories")
    parser.add_argument("--output-dir", required=True,
                        help="Output directory for PNGs/CSVs")
    args = parser.parse_args()

    generate_graphs(
        push_dirs=[Path(d) for d in args.push_dirs],
        poll30_dirs=[Path(d) for d in args.poll30_dirs],
        output_dir=Path(args.output_dir),
    )


if __name__ == "__main__":
    main()
