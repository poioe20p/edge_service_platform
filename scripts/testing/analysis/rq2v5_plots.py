#!/usr/bin/env python3
"""RQ2 v5 cross-mode comparison plots (W4).

Reads rq2v5_cell_stats.csv and writes the campaign's comparison figures:

  fig1_mx_tdb_by_mode.png        mx: episode t_db per mode (log scale)
  fig2_db_tdb_by_mode.png        db: episode t_db per mode (log scale)
  fig3_mx_completion_by_mode.png mx: episode completion per mode
  fig4_db_completion_by_mode.png db: episode completion per mode
  fig5_mechanism_by_mode.png     members in episode + storage-tier CPU per mode

Each marker is one seed (lane mean), the bar is the median. `stor_db_43` uses
the approved replacement (`*_r1`).

usage: rq2v5_plots.py --cells <rq2v5_cell_stats.csv> --outdir <graphs/comparison>
"""
import argparse
import csv
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

MODES = ["none", "comp", "stor", "both"]
COLORS = {"none": "#9aa0a6", "comp": "#4285f4", "stor": "#f9ab00", "both": "#34a853"}


def load(path):
    rows = list(csv.DictReader(open(path)))
    by = {}
    for r in rows:
        key = (r["regime"], r["mode"], r["seed"])
        if key in by and r["label"].endswith("_r1"):
            by[key] = r
        elif key not in by:
            by[key] = r
    return by


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def per_seed(by, regime, field):
    """{mode: [(seed, lane-mean value), ...]}"""
    out = {m: [] for m in MODES}
    seeds = sorted({s for (reg, m, s) in by if reg == regime}, key=int)
    for m in MODES:
        for s in seeds:
            r = by.get((regime, m, s))
            if not r:
                continue
            a, b = fnum(r.get(f"lan1_{field}")), fnum(r.get(f"lan2_{field}"))
            if a is None or b is None:
                continue
            out[m].append((s, (a + b) / 2.0))
    return out


def strip_box(ax, data, ylabel, title, log=False, fmt="{:.0f}"):
    positions = range(1, len(MODES) + 1)
    for i, m in enumerate(MODES):
        vals = [v for _, v in data[m]]
        if not vals:
            continue
        ax.boxplot([vals], positions=[i + 1], widths=0.5, showfliers=False,
                   medianprops={"color": "#202124", "linewidth": 2})
        xs = [i + 1 + (j - (len(vals) - 1) / 2.0) * 0.06 for j in range(len(vals))]
        ax.scatter(xs, vals, s=32, color=COLORS[m], zorder=3, edgecolor="white", linewidth=0.5)
        ax.annotate(fmt.format(statistics.median(vals)), (i + 1, max(vals)),
                    textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9)
    ax.set_xticks(list(positions))
    ax.set_xticklabels([f"{m}\n(n={len(data[m])})" for m in MODES])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if log:
        ax.set_yscale("log")
    ax.grid(axis="y", alpha=0.3)


def mechanism(ax, by, field, ylabel, title):
    width = 0.38
    for j, regime in enumerate(("mx", "db")):
        data = per_seed(by, regime, field)
        meds = [statistics.median([v for _, v in data[m]]) if data[m] else 0 for m in MODES]
        xs = [i + (j - 0.5) * width for i in range(len(MODES))]
        ax.bar(xs, meds, width=width, label=regime,
               color=["#c6dafc", "#9fc0f7", "#79a6f2", "#5488ec"] if regime == "mx"
               else ["#fde293", "#fbc65a", "#f9ab00", "#e08e00"])
        for x, v in zip(xs, meds):
            ax.annotate(f"{v:.0f}", (x, v), textcoords="offset points", xytext=(0, 3),
                        ha="center", fontsize=8)
    ax.set_xticks(range(len(MODES)))
    ax.set_xticklabels(MODES)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    by = load(args.cells)

    specs = [
        ("mx", "t_db_ms", "avg_time_db_ms (ms)", "mx block — episode t_db by mode", True, "{:.1f}", "fig1_mx_tdb_by_mode.png"),
        ("db", "t_db_ms", "avg_time_db_ms (ms, log)", "db block — episode t_db by mode", True, "{:.0f}", "fig2_db_tdb_by_mode.png"),
        ("mx", "completion_pct", "episode completion (%)", "mx block — episode completion by mode", False, "{:.1f}", "fig3_mx_completion_by_mode.png"),
        ("db", "completion_pct", "episode completion (%)", "db block — episode completion by mode", False, "{:.1f}", "fig4_db_completion_by_mode.png"),
    ]
    for regime, field, ylabel, title, log, fmt, name in specs:
        fig, ax = plt.subplots(figsize=(7.5, 4.2))
        strip_box(ax, per_seed(by, regime, field), ylabel, title, log=log, fmt=fmt)
        fig.tight_layout()
        out = os.path.join(args.outdir, name)
        fig.savefig(out, dpi=160)
        plt.close(fig)
        print("wrote", out)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    mechanism(axes[0], by, "members", "members carrying traffic in episode", "Storage members present (median)")
    mechanism(axes[1], by, "sto_cpu", "storage-tier CPU (%)", "Storage-tier CPU during episode (median)")
    fig.suptitle("Mechanism: members added and tier relief (mx = blue, db = amber)", fontsize=11)
    fig.tight_layout()
    out = os.path.join(args.outdir, "fig5_mechanism_by_mode.png")
    fig.savefig(out, dpi=160)
    plt.close(fig)
    print("wrote", out)


if __name__ == "__main__":
    main()
