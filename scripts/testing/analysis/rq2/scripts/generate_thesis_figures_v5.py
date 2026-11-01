"""Thesis figures for the RQ2 v5 scaling-action campaign.

Reads the cell-level summary produced by ``analysis/rq2v5_cell_stats.py``
(``rq2v5_cell_stats.csv``) and writes the three figures used by the RQ2
results section:

1. ``rq2_mixed_press_v3.png`` -- mixed press: processing, data access and
   completion by scaling policy.
2. ``rq2_data_press_v3.png`` -- data-access press: same panels, data access on
   a logarithmic axis.
3. ``rq2_storage_relief_v5.png`` -- storage utilisation and members serving,
   as a 2x2 grid of press x metric.

The palette and rcParams come from the shared ``thesis_figure_style.py``
module, so these figures match the Chapter 5 house style.

The superseded ``rq2v5_stor_db_43`` run (edge OOM at press onset, replaced by
``rq2v5_stor_db_43_r1``) is excluded from every panel.

Usage (from the repo root):

    python source/scripts/testing/analysis/rq2/scripts/generate_thesis_figures_v5.py \
        --cells docs/operation/testing/experiment/v5/rq2/rq2v5_cell_stats.csv \
        --outdir tese/images
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from pathlib import Path

_ANALYSIS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ANALYSIS_DIR))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from thesis_figure_style import (  # noqa: E402
    INK,
    apply_house_style,
    series_colours,
    style_axes,
)

MODES = ["none", "comp", "stor", "both"]
LABELS = {
    "none": "no scaling",
    "comp": "compute-only",
    "stor": "storage-only",
    "both": "bottleneck-aware",
}
EXCLUDED = {"rq2v5_stor_db_43"}

POLICY_COLORS = dict(zip(MODES, series_colours(len(MODES))))
GREY = INK

apply_house_style()


def _f(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_runs(path: Path) -> dict[tuple[str, str], list[dict]]:
    runs: dict[tuple[str, str], list[dict]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["label"] in EXCLUDED:
                continue
            lanes = {}
            for key in (
                "t_db_ms",
                "t_proc_ms",
                "completion_pct",
                "sto_cpu",
                "members",
            ):
                a, b = _f(row[f"lan1_{key}"]), _f(row[f"lan2_{key}"])
                if a is None or b is None:
                    raise ValueError(f"missing {key} in {row['label']}")
                lanes[key] = (a + b) / 2
            runs.setdefault((row["regime"], row["mode"]), []).append(lanes)
    return runs


def _stats(values: list[float]) -> tuple[float, float, float]:
    return st.median(values), min(values), max(values)


def _draw_metric(
    ax,
    runs: dict,
    regime: str,
    key: str,
    log: bool = False,
) -> None:
    for i, mode in enumerate(MODES):
        values = [r[key] for r in runs[(regime, mode)]]
        med, low, high = _stats(values)
        ax.bar(
            i,
            med,
            0.62,
            color=POLICY_COLORS[mode],
            alpha=0.85,
            edgecolor="white",
            zorder=2,
        )
        ax.errorbar(
            i,
            med,
            yerr=[[med - low], [high - med]],
            fmt="none",
            ecolor=GREY,
            capsize=3,
            linewidth=1,
            zorder=3,
        )
        jitter = (np.arange(len(values)) - (len(values) - 1) / 2) * 0.09
        ax.scatter(i + jitter, values, s=14, color=GREY, alpha=0.75,
                   zorder=4, linewidths=0)
    if log:
        ax.set_yscale("log")


def _finish_axes(ax, title: str, ylabel: str) -> None:
    ax.set_xticks(np.arange(len(MODES)))
    ax.set_xticklabels(
        [LABELS[m] for m in MODES],
        rotation=18,
        ha="right",
        rotation_mode="anchor",
    )
    style_axes(ax, ylabel, title)


def fig_press(runs: dict, regime: str, out: Path, log: bool) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(10.4, 3.4))

    _draw_metric(axes[0], runs, regime, "t_proc_ms")
    _finish_axes(axes[0], "(a) Processing", "Component latency (ms)")

    _draw_metric(axes[1], runs, regime, "t_db_ms", log=log)
    _finish_axes(
        axes[1],
        "(b) Data access",
        "Component latency (ms, log scale)" if log else "Component latency (ms)",
    )

    _draw_metric(axes[2], runs, regime, "completion_pct")
    _finish_axes(axes[2], "(c) Completion", "Completed requests (%)")
    axes[2].set_ylim(0, 105)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_relief(runs: dict, out: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(8.2, 6.2))
    panels = [
        ("mx", "Mixed press", "(a)", "(b)", 0),
        ("db", "Data-access press", "(c)", "(d)", 1),
    ]
    for regime, label, left_letter, right_letter, row in panels:
        _draw_metric(axes[row, 0], runs, regime, "sto_cpu")
        _finish_axes(
            axes[row, 0],
            f"{left_letter} {label}: storage utilisation",
            "Storage tier CPU (%)",
        )
        axes[row, 0].set_ylim(0, 105)

        _draw_metric(axes[row, 1], runs, regime, "members")
        _finish_axes(
            axes[row, 1],
            f"{right_letter} {label}: members serving",
            "Members",
        )
        axes[row, 1].set_ylim(0, 6)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def report(runs: dict) -> None:
    for regime in ("mx", "db"):
        for mode in MODES:
            rows = runs[(regime, mode)]
            proc = _stats([r["t_proc_ms"] for r in rows])
            db = _stats([r["t_db_ms"] for r in rows])
            comp = _stats([r["completion_pct"] for r in rows])
            cpu = st.median([r["sto_cpu"] for r in rows])
            mem = st.median([r["members"] for r in rows])
            print(
                f"{regime:>3} {mode:<5} n={len(rows)} "
                f"proc={proc[0]:.0f} ({proc[1]:.0f}-{proc[2]:.0f}) "
                f"db={db[0]:.0f} ({db[1]:.0f}-{db[2]:.0f}) "
                f"completion={comp[0]:.1f} ({comp[1]:.1f}-{comp[2]:.1f}) "
                f"storage_cpu={cpu:.0f} members={mem:.1f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cells",
        default="docs/operation/testing/experiment/v5/rq2/rq2v5_cell_stats.csv",
    )
    parser.add_argument("--outdir", default="tese/images")
    args = parser.parse_args()

    runs = load_runs(Path(args.cells))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    fig_press(runs, "mx", outdir / "rq2_mixed_press_v3.png", log=False)
    fig_press(runs, "db", outdir / "rq2_data_press_v3.png", log=True)
    fig_relief(runs, outdir / "rq2_storage_relief_v5.png")
    report(runs)
    print(f"wrote 3 figures to {outdir}")


if __name__ == "__main__":
    main()
