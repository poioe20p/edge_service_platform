#!/usr/bin/env python3
"""Render the four RQ3 timing-campaign figures from the frozen run records.

Reads (workspace, read-only):
  docs/operation/testing/experiment/v3/rq3_timing/analysis/timing_campaign.json
  docs/operation/testing/experiment/v3/rq3_timing/analysis/timing_figures_data.json
    (companion export produced by rq3/timing_figures_export.py on the VM)
Writes:
  docs/operation/testing/experiment/v3/rq3_timing/graphs/
    ht1_block_contrast.png     per-block Delta_slow, full + onset windows
    slow_share_series.png      slow share over time (median across blocks)
    relief_tails.png           tail-window slow share per block/arm
    admission_accounting.png   admitted dynamic backends per run (arm x cell)

The four figures document the timing campaign (H-T1 co-primary windows, tail
health, relief accounting). Numbers render exactly the frozen records; the
TTR re-anchoring companion (timing_campaign_reanchored.json) is NOT part of
these figures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[5]
CAMP = ROOT / "docs/operation/testing/experiment/v3/rq3_timing"
DATA_DIR = CAMP / "analysis"
OUT_DIR = CAMP / "graphs"

ARMS = ("event_only", "hybrid", "reconcile")
COLORS = {"event_only": "#c0392b", "hybrid": "#e67e22", "reconcile": "#2471a3"}
CONTRAST_PP = 5.0
CONTROL_TAIL_MAX_PP = 2.0

plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 150,
    "font.size": 9.5, "axes.titlesize": 10.5, "axes.labelsize": 9.5,
    "axes.grid": True, "grid.alpha": 0.3, "grid.linestyle": ":",
    "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False,
})


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fig_block_contrast(camp: dict) -> Path:
    blocks = [1, 2, 3, 4, 5, 6]
    assessable = camp["assessable_blocks"]
    full = dict(zip(assessable, camp["block_delta_slow_pp"]))
    onset = dict(zip(assessable, camp["block_onset_delta_slow_pp"]))
    note = camp["block_exclusions"].get("4", "excluded")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=False)
    panels = (
        (axes[0], full, "Full plateau \u0394_slow (pp)",
         camp["H-T1"]["median_pp"]),
        (axes[1], onset, "Onset window \u0394_slow (pp, first 150 s)",
         camp["H-T1"]["onset_median_pp"]),
    )
    for ax, values, title, median in panels:
        xs = np.arange(len(blocks))
        bar_vals = [values.get(block, np.nan) for block in blocks]
        ax.bar(xs, bar_vals, 0.55, color="#c0392b", alpha=0.85)
        for x, value in zip(xs, bar_vals):
            if not np.isnan(value):
                ax.text(x, value + 0.6, f"{value:.1f}", ha="center", fontsize=8.5)
        ax.axhline(CONTRAST_PP, color="#555555", ls="--", lw=1.1)
        ax.axhline(median, color="#2471a3", ls=":", lw=1.2)
        ax.set_xticks(xs, [f"b{b}" for b in blocks])
        ax.set_title(title)
        ylim_top = max(values.values()) * 1.25
        ax.set_ylim(0, ylim_top)
        ax.text(-0.5, ylim_top * 0.98, f"median {median:.2f} pp",
                ha="left", va="top", fontsize=8, color="#2471a3")
        ax.text(-0.5, ylim_top * 0.90, "\u2265 5 pp bar", ha="left",
                va="top", fontsize=8, color="#555555")
        ax.annotate("excluded\n(controls tail >2 pp)", xy=(3, 1.5),
                    ha="center", fontsize=8, color="#777777")
    fig.suptitle("H-T1: slow-rate contrast by block \u2014 event_only \u2212 "
                 "max(hybrid, reconcile), rate 2.0", y=0.99)
    fig.text(0.5, 0.005,
             f"5/5 assessable blocks \u2265 5 pp in both windows \u00b7 "
             f"MWU p = {camp['H-T1']['mwu_p']:.4f} \u00b7 "
             f"Cliff's \u03b4 = {camp['H-T1']['cliffs_delta']:.1f} \u00b7 "
             "b4 " + note,
             ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    out = OUT_DIR / "ht1_block_contrast.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def fig_slow_series(data: dict) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5.2))
    runs = data["data"]
    x_last = -60
    for arm in ARMS:
        buckets: dict[int, list[float]] = {}
        for record in runs.values():
            if record["cell"] != "loss_all" or record["arm"] != arm:
                continue
            for point in record["series"]:
                if point["slow_rate"] is None or point["service"] < 10:
                    continue
                buckets.setdefault(round(point["t_rel"]), []).append(
                    point["slow_rate"])
        xs = sorted(buckets)
        if not xs:
            continue
        x_last = max(x_last, xs[-1])
        ys = [100.0 * float(np.median(buckets[t])) for t in xs]
        ax.plot(xs, ys, label=arm, color=COLORS[arm], lw=1.8)

    ax.axvspan(0, 150, color="#e8b3b3", alpha=0.30, lw=0)
    ax.axvspan(max(x_last - 300, 0), x_last, color="#b9cfe8", alpha=0.30, lw=0)
    ax.text(2, ax.get_ylim()[1] * 0.93, "onset window\n(0\u2013150 s)",
            fontsize=8, color="#8c4a4a", va="top")
    ax.text(x_last - 295, ax.get_ylim()[1] * 0.93, "tail window\n(last 300 s)",
            fontsize=8, color="#3c5a7a", va="top")
    ax.set_xlabel("time since plateau onset (s)")
    ax.set_ylabel("slow share (%)")
    ax.set_xlim(-65, x_last + 5)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right")
    ax.set_title("Slow-share recovery by arm \u2014 loss_all runs, "
                 "median across 6 blocks (rate 2.0)")
    fig.tight_layout()
    out = OUT_DIR / "slow_share_series.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def fig_relief_tails(data: dict) -> Path:
    blocks = data["blocks"]
    runs = data["data"]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    width = 0.26
    xs = np.arange(len(blocks))
    top = 0.0
    for i, arm in enumerate(ARMS):
        vals = []
        none_vals = []
        for block in blocks:
            record = runs.get(f"b{block}_loss_all_{arm}")
            value = record["slow_tail"] if record else None
            vals.append(100.0 * value if value is not None else np.nan)
            record = runs.get(f"b{block}_none_{arm}")
            value = record["slow_tail"] if record else None
            none_vals.append(100.0 * value if value is not None else np.nan)
        ax.bar(xs + (i - 1) * width, vals, width, color=COLORS[arm],
               label=f"{arm} (loss_all)")
        ax.scatter(xs + (i - 1) * width, none_vals, marker="o", s=16,
                   facecolors="none", edgecolors="#444444", linewidths=0.9,
                   zorder=3, label="none-cell" if i == 0 else None)
        for x, v in zip(xs + (i - 1) * width, vals):
            if not np.isnan(v):
                top = max(top, v)
                if v > CONTROL_TAIL_MAX_PP:
                    ax.text(x, v + 0.2, f"{v:.1f}", ha="center", fontsize=8)
    ax.axhline(CONTROL_TAIL_MAX_PP, color="#555555", ls="--", lw=1.1)
    ax.text(len(blocks) - 0.4, CONTROL_TAIL_MAX_PP + 0.25,
            "control ceiling \u2264 2 pp", ha="right", fontsize=8,
            color="#555555")
    ax.set_xticks(xs, [f"b{b}" for b in blocks])
    ax.set_ylabel("slow share, last 300 s of plateau (%)")
    ax.set_ylim(0, top * 1.2)
    ax.legend(loc="upper right", ncols=2)
    ax.set_title("Relief-window health by block \u2014 tail slow share after "
                 "the relief wave (rate 2.0)")
    fig.text(0.5, 0.005,
             "b4: loss_all control tail >2 pp (excluded from H-T1 gates) "
             "\u00b7 open circles: none-cell runs \u00b7 none-cell floor "
             "16/18 (one tail over: b5/reconcile 4.3 %)",
             ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    out = OUT_DIR / "relief_tails.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def fig_admission(data: dict) -> Path:
    blocks = data["blocks"]
    runs = data["data"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), sharey=False)
    width = 0.26
    xs = np.arange(len(blocks))
    for ax, cell in zip(axes, ("loss_all", "none")):
        for i, arm in enumerate(ARMS):
            admitted, abandoned = [], []
            for block in blocks:
                record = runs.get(f"b{block}_{cell}_{arm}")
                adm = record["admissions"] if record else {}
                admitted.append(adm.get("admitted", np.nan))
                abandoned.append(adm.get("abandoned", np.nan))
            xpos = xs + (i - 1) * width
            ax.bar(xpos, admitted, width, color=COLORS[arm], label=arm)
            ax.bar(xpos, abandoned, width, bottom=admitted, color="#95a5a6",
                   hatch="//", edgecolor="white", linewidth=0.4)
            for xp, adm_v, ab_v in zip(xpos, admitted, abandoned):
                if np.isnan(adm_v):
                    continue
                label = f"{int(adm_v)}" if not ab_v else f"{int(adm_v)}+{int(ab_v)}"
                ax.text(xp, adm_v + (ab_v or 0) + 0.5, label, ha="center",
                        fontsize=7.5, color="#555555")
        ax.set_xticks(xs, [f"b{b}" for b in blocks])
        ax.set_title(f"{cell} runs")
    axes[0].set_ylim(0, 40)
    axes[1].set_ylim(0, 7.5)
    axes[0].set_ylabel("dynamic backends per run")
    axes[1].set_ylabel("dynamic backends per run")
    from matplotlib.patches import Patch
    handles, labels = axes[0].get_legend_handles_labels()
    handles.append(Patch(facecolor="#95a5a6", hatch="//", edgecolor="white"))
    labels.append("abandoned (never admitted)")
    fig.legend(handles, labels, loc="upper center", ncols=5,
               bbox_to_anchor=(0.5, 0.945), fontsize=8.5)
    fig.suptitle("Admission accounting \u2014 dynamic compute backends "
                 "per run (rate 2.0)", y=0.995)
    fig.text(0.5, 0.005,
             "loss_all/event_only: 0 admitted \u00b7 32\u201335 candidates "
             "abandoned per block (never admitted under total loss) \u00b7 "
             "admission path: hybrid \u2192 probe_fallback (20 s), "
             "reconcile \u2192 probe (10 s poll) \u00b7 all other runs: "
             "0 abandoned",
             ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 0.90))
    out = OUT_DIR / "admission_accounting.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def main() -> int:
    camp = load_json(DATA_DIR / "timing_campaign.json")
    data = load_json(DATA_DIR / "timing_figures_data.json")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = [
        fig_block_contrast(camp),
        fig_slow_series(data),
        fig_relief_tails(data),
        fig_admission(data),
    ]
    for path in written:
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
