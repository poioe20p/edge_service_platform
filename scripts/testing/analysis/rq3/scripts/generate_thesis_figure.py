"""RQ3 thesis figures --- rendered into the house thesis style.

Three renders for the RQ3 results section:
  * `rq3_admission_intervals_v2.png` -- the intact-source interval chain:
    (a) provisioning -> first successful response per backend,
    (b) scaling decision -> usable capacity per run (rate-12 cell).
    (v1, whose first panel was the log-scale verified-readiness ->
    admission dot plot, was superseded 2026-10-05; that interval remains
    in `tab:rq3_delays`.) Sources (read-only):
    `docs/operation/testing/experiment/v2/rq3/graphs/campaign_fixed/
    campaign_stratified_per_backend.csv` and `.../v2/rq3/rq3_probe_summary.csv`
    (the twelve P2 + cell-12 runs per arm, listed in CELL12_RUNS).
  * `rq3_event_loss_outcome.png` -- (a) slow share over time under total
    readiness-event loss, median across the six seed blocks, one line per
    admission configuration; (b) admitted backends per run (dots per block,
    median bar with min--max), showing that the configuration without a
    re-derivation path admits nothing while 32--35 candidates per run are
    abandoned and torn down. Source: the frozen-record export
    `docs/operation/testing/experiment/v3/rq3_timing/analysis/
    timing_figures_data.json`.
  * `rq3_recovery_ordering.png` -- provisioning -> admission per seed block
    for the two re-deriving configurations under total event loss
    (analysis-only re-anchored ordering). Source: the analysis-only companion
    `docs/operation/testing/experiment/v3/rq3_timing/analysis/
    timing_campaign_reanchored.json`. Kept as an archive render; not
    referenced by the thesis build since 2026-10-05.
  * `rq3_soundness_damage_v2.png` -- the timing-lie grid: episode damage
    against the claim lead per rule (seed medians as lines, individual
    seeds as dots). Sources: `docs/operation/testing/experiment/v3/
    rq3_soundness/analysis/rq3snd_damage_all.json` and
    `.../rq3snd_crossover_all.json`; the render is also copied to
    `tese/images/` for the thesis build. (v1, a two-panel version that
    included the retired false-claim family, was superseded 2026-10-05.)

Completion-era trio (Family 6, added 2026-10-15; the three renders the
thesis build carries). Palette and rcParams come from the shared Chapter 5
module `source/scripts/testing/analysis/thesis_figure_style.py`:
  * `rq3_admission_intervals_v7.png` -- the intact price of checking with
    all four admission modes: (a) provisioning -> first successful
    response per backend, per mode and start-up class (the confirmation
    check has no slow-class backends: the bind stall did not occur in the
    completion runs); (b) the rate-12 panel over its seven runs per
    mechanism. Sources:
    `.../v3/rq3_admission_modes/analysis/completion_figures_data.json`
    (panel a), the v2 cell-12 CSV for the six certified runs and the v3
    completion-support record for the seventh (panel b). v7 (2026-10-28)
    fills every bar at alpha 0.85, matching the RQ1 and RQ2 figures; v6
    had filled at 0.80, so the same palette rendered visibly lighter here.
    v6 was archived to `tese/images/unused/`.
  * `rq3_event_loss_outcome_v4.png` -- the lost-message outcome with the
    four modes over the seven completion blocks: (a) slow share over
    time (median per 10 s bin), (b) admitted backends per run. Source:
    the same completion export.
  * `rq3_premature_damage_v3.png` -- the premature-message damage at the
    evaluated ten-second lead, per mode: per-pair dots and pair medians
    against the 2 pp bound. Sources: `.../analysis/
    completion_premature_full.json` (A/B/C) and
    `completion_premature_wake.json` (D, with the block-1 pair
    re-included under the 2026-10-14 ruling).

Style comes from `source/scripts/testing/analysis/thesis_figure_style.py`
(house seaborn-deep palette, 150/200 dpi, black per-run dots).

Renders are written to the experiment folders
(`.../v2/rq3/graphs/thesis/` and `.../v3/rq3_timing/graphs/thesis/`) and
copied to `tese/images/` (house flow: a revised figure gets a new `_v2` name
and `main.tex` is updated).
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

_ANALYSIS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ANALYSIS_DIR))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from thesis_figure_style import (  # noqa: E402
    GRID,
    INK,
    PALETTE,
    apply_house_style,
    series_colours,
)

ROOT = Path(__file__).resolve().parents[6]
DATA = (ROOT / "docs/operation/testing/experiment/v3/rq3_timing/analysis/"
        "timing_figures_data.json")
OUT = ROOT / "docs/operation/testing/experiment/v3/rq3_timing/graphs/thesis"
OUT.mkdir(parents=True, exist_ok=True)

MODE_ORDER = ("event_only", "hybrid", "reconcile", "wake_verify")

# fixed cross-RQ colour order: position 1 grey, 2 blue, 3 orange, 4 green
COLORS = dict(zip(MODE_ORDER, series_colours(len(MODE_ORDER))))
LABELS = {
    "event_only": "backend notification",
    "hybrid": "notification with a fallback check",
    "reconcile": "periodic polling",
    "wake_verify": "notification with a confirmation check",
}
LABELS_PLOT = {
    "event_only": "notification only",
    "hybrid": "notification + fallback check",
    "reconcile": "periodic polling",
    "wake_verify": "notification + confirmation check",
}
BLOCKS = [1, 2, 3, 4, 5, 6]           # recorded six-block frame (archives)
COMPL_BLOCKS = [1, 2, 3, 4, 5, 6, 7]  # completion standard: seven blocks
ONSET_S = 150.0
BIN_MIN_SERVICE = 10
RNG = np.random.default_rng(42)

V2 = ROOT / "docs/operation/testing/experiment/v2/rq3"
STRATIFIED_CSV = V2 / "graphs/campaign_fixed/campaign_stratified_per_backend.csv"
PROBE_SUMMARY_CSV = V2 / "rq3_probe_summary.csv"
OUT_V2 = V2 / "graphs/thesis"
OUT_V2.mkdir(parents=True, exist_ok=True)
REANCHORED_JSON = (ROOT / "docs/operation/testing/experiment/v3/rq3_timing/"
                   "analysis/timing_campaign_reanchored.json")
SND = ROOT / "docs/operation/testing/experiment/v3/rq3_soundness"
SND_DAMAGE = SND / "analysis/rq3snd_damage_all.json"
SND_CROSS = SND / "analysis/rq3snd_crossover_all.json"
OUT_SND = SND / "graphs/thesis"
OUT_SND.mkdir(parents=True, exist_ok=True)
COL_SND = COLORS
LABELS_SND = LABELS

# completion campaign (Family 6): four-mode figure data + screen records
COMPL = ROOT / "docs/operation/testing/experiment/v3/rq3_admission_modes"
COMPL_DATA = COMPL / "analysis/completion_figures_data.json"
COMPL_PREMATURE_FULL = COMPL / "analysis/completion_premature_full.json"
COMPL_PREMATURE_WAKE = COMPL / "analysis/completion_premature_wake.json"
COMPL_SUPPORT = COMPL / "analysis/completion_support.json"
OUT_COMPL = COMPL / "graphs/thesis"
OUT_COMPL.mkdir(parents=True, exist_ok=True)

# wake block-1 pair damage recomputed from the archived runs under the
# 2026-10-14 ruling (partner tail 3.95 %: retained, not dropped);
# provenance: results.md section 2.5.
WAKE_B1_D_S = -0.873

# interval-figure arms: direct lifecycle vs periodic discovery (same fixed
# order as the event-loss figure: direct grey, discovery orange, fallback blue)
COL_ARM = {"direct": COLORS["event_only"], "discovery": COLORS["reconcile"],
           "fallback": COLORS["hybrid"]}
GROUPS = [("direct", "fast"), ("direct", "slow"),
          ("discovery", "fast"), ("discovery", "slow")]
GROUP_X = [0, 1, 3, 4]

# rate-12 cell: P2 + cell-12 runs 1..5 per arm of the probe campaign
CELL12_RUNS = {
    "direct": [
        "20260805_184258_rq3_probe_p2_direct",
        "20260805_205423_rq3_probe_cell12_direct_1",
        "20260806_022046_rq3_probe_cell12_direct_2",
        "20260806_024626_rq3_probe_cell12_direct_3",
        "20260806_031155_rq3_probe_cell12_direct_4",
        "20260806_033629_rq3_probe_cell12_direct_5",
    ],
    "discovery": [
        "20260805_190300_rq3_probe_p2_disc",
        "20260805_210637_rq3_probe_cell12_disc_1",
        "20260806_023330_rq3_probe_cell12_disc_2",
        "20260806_025851_rq3_probe_cell12_disc_3",
        "20260806_032414_rq3_probe_cell12_disc_4",
        "20260806_034852_rq3_probe_cell12_disc_5",
    ],
}

# Seventh replicate per mechanism (2026-10-13). Its per-run value is recorded
# only in the v3 completion-support record, not in the v2 probe summary CSV.
CELL12_EXTRA = {
    "direct": ("direct_scale_to_usable_s",
               "20261013_040546_rq3_probe_cell12_direct_6"),
    "discovery": ("discovery_scale_to_usable_s",
                  "20261013_042031_rq3_probe_cell12_disc_6"),
}

apply_house_style()


def load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))["data"]


def pooled_series(data: dict, arm: str) -> tuple[list[int], list[float]]:
    """Median slow share (%) per 10 s bin across the six event-loss blocks."""
    buckets: dict[int, list[float]] = {}
    for block in BLOCKS:
        record = data[f"b{block}_loss_all_{arm}"]
        for point in record["series"]:
            if point["slow_rate"] is None or point["service"] < BIN_MIN_SERVICE:
                continue
            buckets.setdefault(round(point["t_rel"]), []).append(point["slow_rate"])
    xs = sorted(buckets)
    ys = [100.0 * float(np.median(buckets[t])) for t in xs]
    return xs, ys


def panel_a(ax, data: dict) -> None:
    x_last = -60
    for arm in ("reconcile", "hybrid", "event_only"):  # affected line on top
        xs, ys = pooled_series(data, arm)
        x_last = max(x_last, xs[-1])
        ax.plot(xs, ys, color=COLORS[arm], lw=1.9, label=LABELS[arm])
    ax.axvspan(0, ONSET_S, color="#C44E52", alpha=0.10, lw=0)
    ax.set_xlabel("time since plateau onset (s)")
    ax.set_ylabel("slow share (%)")
    ax.set_xlim(-65, x_last + 5)
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.legend(loc="upper right", frameon=False)
    top = ax.get_ylim()[1]
    ax.text(ONSET_S - 4, top * 0.985, "onset window", ha="right", va="top",
            fontsize=9.5, color="#8c4a4a")
    ax.text(-0.10, 1.045, "(a)", transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def panel_b(ax, data: dict) -> None:
    for i, arm in enumerate(("event_only", "hybrid", "reconcile")):
        values = np.array(
            [data[f"b{block}_loss_all_{arm}"]["admissions"]["admitted"]
             for block in BLOCKS], dtype=float)
        median = float(np.median(values))
        ax.bar(i, median, 0.5, color=COLORS[arm], alpha=0.85,
               edgecolor="white", zorder=2)
        if values.min() != values.max():
            ax.errorbar(i, median,
                        yerr=[[median - values.min()], [values.max() - median]],
                        fmt="none", ecolor="#333", capsize=3, lw=1.2, zorder=3)
        jitter = np.linspace(-0.12, 0.12, len(values))
        ax.scatter(i + jitter, values, color=INK, s=20, zorder=4)
        ax.text(i, median + 0.22 if median > 0 else 0.6, f"{int(median)}",
                ha="center", fontsize=10, fontweight="bold")
    ax.set_xticks(np.arange(3), ["direct\nlifecycle", "fallback\nvariant",
                                 "periodic\ndiscovery"])
    ax.set_ylabel("admitted backends per run")
    ax.set_ylim(0, 7.5)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.text(0.03, 0.97, "direct-lifecycle runs: 0 admitted;\n"
            "32\u201335 candidates per run abandoned",
            transform=ax.transAxes, va="top", fontsize=9)
    ax.text(-0.16, 1.045, "(b)", transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def load_stratified() -> list[dict]:
    """Per-backend rows of the fixed-image readiness campaign."""
    with open(STRATIFIED_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["ready_ms"] = float(row["ready_to_admit_s"]) * 1000.0
        row["first_s"] = float(row["spawn_to_first_s"])
    return rows


def load_cell12() -> dict[str, list[float]]:
    """Scaling-decision -> usable-capacity per run of the rate-12 cell."""
    with open(PROBE_SUMMARY_CSV, newline="", encoding="utf-8") as fh:
        by_name = {Path(r["run_dir"]).name: r for r in csv.DictReader(fh)}
    support = json.loads(COMPL_SUPPORT.read_text(encoding="utf-8"))
    out: dict[str, list[float]] = {}
    for arm, names in CELL12_RUNS.items():
        out[arm] = [float(by_name[name]["scale_to_first_success_median_s"])
                    for name in names]  # KeyError = data drift; fail loudly
        key, run = CELL12_EXTRA[arm]
        assert run not in by_name, f"{run} is already in the v2 summary"
        out[arm].append(float(np.median(support["E-S2"][key])))
    return out


def _group_values(rows: list[dict], arm: str, stratum: str,
                  key: str) -> np.ndarray:
    return np.array([r[key] for r in rows
                     if r["arm"] == arm and r["stratum"] == stratum],
                    dtype=float)


def _stylize_strata_axis(ax) -> None:
    ax.set_xticks(GROUP_X, ["fast", "slow", "fast", "slow"])
    for label, (arm, _stratum) in zip(ax.get_xticklabels(), GROUPS):
        label.set_color(COL_ARM[arm])
    transform = ax.get_xaxis_transform()
    ax.text(0.5, -0.115, "direct lifecycle", transform=transform, ha="center",
            va="top", fontsize=9.5, color=COL_ARM["direct"])
    ax.text(3.5, -0.115, "periodic discovery", transform=transform,
            ha="center", va="top", fontsize=9.5, color=COL_ARM["discovery"])
    ax.grid(axis="y", alpha=0.25, linestyle="--")


def panel_intervals_b(ax, rows: list[dict], letter: str = "(b)") -> None:
    """Provisioning -> first successful response, per backend."""
    for x, (arm, stratum) in zip(GROUP_X, GROUPS):
        values = _group_values(rows, arm, stratum, "first_s")
        median = float(np.median(values))
        ax.bar(x, median, 0.55, color=COL_ARM[arm], alpha=0.85,
               edgecolor="white", zorder=2)
        ax.errorbar(x, median,
                    yerr=[[median - values.min()], [values.max() - median]],
                    fmt="none", ecolor="#333", capsize=3, lw=1.2, zorder=3)
        jitter = RNG.uniform(-0.15, 0.15, len(values))
        ax.scatter(x + jitter, values, s=14, color=INK, alpha=0.75,
                   linewidths=0, zorder=4)
        ax.text(x, values.max() + 0.8, f"{median:.1f}", ha="center",
                fontsize=9.5)
    ax.set_ylim(0, 25)
    ax.set_ylabel("provisioning \u2192 first success (s)")
    _stylize_strata_axis(ax)
    ax.text(-0.18, 1.05, letter, transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def panel_intervals_c(ax, probes: dict[str, list[float]],
                      letter: str = "(c)") -> None:
    """Scaling decision -> usable capacity, per run (rate-12 cell)."""
    for x, arm in enumerate(("direct", "discovery")):
        values = np.array(probes[arm], dtype=float)
        median = float(np.median(values))
        ax.bar(x, median, 0.5, color=COL_ARM[arm], alpha=0.85,
               edgecolor="white", zorder=2)
        ax.errorbar(x, median,
                    yerr=[[median - values.min()], [values.max() - median]],
                    fmt="none", ecolor="#333", capsize=3, lw=1.2, zorder=3)
        jitter = RNG.uniform(-0.09, 0.09, len(values))
        ax.scatter(x + jitter, values, s=28, color=INK, alpha=0.85,
                   linewidths=0, zorder=4)
        ax.text(x, values.max() + 0.35, f"{median:.2f}", ha="center",
                fontsize=9.5)
    ax.set_xticks([0, 1], ["backend\nnotification", "periodic\npolling"])
    for label, arm in zip(ax.get_xticklabels(), ("direct", "discovery")):
        label.set_color(COL_ARM[arm])
    ax.set_ylim(0, 10.5)
    ax.set_ylabel("scaling decision \u2192 usable capacity (s)")
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.text(-0.20, 1.05, letter, transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def interval_figure() -> None:
    """Intact-source admission-chain intervals (two panels, v2)."""
    rows = load_stratified()
    probes = load_cell12()
    fig, axes = plt.subplots(1, 2, figsize=(8.8, 4.4))
    panel_intervals_b(axes[0], rows, letter="(a)")
    panel_intervals_c(axes[1], probes, letter="(b)")
    fig.tight_layout()
    out = OUT_V2 / "rq3_admission_intervals_v2.png"
    fig.savefig(out)
    plt.close(fig)
    shutil.copyfile(out, ROOT / "tese/images/rq3_admission_intervals_v2.png")
    print(f"wrote {out} and tese/images/rq3_admission_intervals_v2.png")
    for arm, stratum in GROUPS:
        first = _group_values(rows, arm, stratum, "first_s")
        print(f"  {arm}/{stratum}: n={len(first)} "
              f"first_s med={np.median(first):.3f}")
    for arm, values in probes.items():
        print(f"  scale->usable {arm}: n={len(values)} "
              f"med={np.median(values):.2f} "
              f"range={min(values):.2f}-{max(values):.2f}")


def ordering_figure() -> None:
    """Provisioning -> admission under event loss, per seed block."""
    with open(REANCHORED_JSON, encoding="utf-8") as fh:
        payload = json.load(fh)
    blocks = payload["blocks"]
    fig, ax = plt.subplots(figsize=(6.9, 4.0))
    for entry in blocks:
        b = entry["block"]
        hy = entry["hybrid"]["spawn_to_admit_s"]
        rc = entry["reconcile"]["spawn_to_admit_s"]
        ax.plot([b, b], [rc, hy], color="#a8a8a8", lw=1.4, zorder=1)
    ax.scatter([e["block"] for e in blocks],
               [e["hybrid"]["spawn_to_admit_s"] for e in blocks],
               s=62, color=COL_ARM["fallback"], edgecolor="white",
               zorder=3, label="probe fallback variant")
    ax.scatter([e["block"] for e in blocks],
               [e["reconcile"]["spawn_to_admit_s"] for e in blocks],
               s=62, color=COL_ARM["discovery"], edgecolor="white",
               zorder=3, label="periodic discovery")
    ax.set_xticks(range(1, 7), ["b1", "b2", "b3", r"b4$^{*}$", "b5", "b6"])
    ax.set_xlabel("seed block")
    ax.set_ylabel("provisioning \u2192 admission (s)")
    ax.set_ylim(0, 26)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    out = OUT / "rq3_recovery_ordering.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")
    hy = [e["hybrid"]["spawn_to_admit_s"] for e in blocks]
    rc = [e["reconcile"]["spawn_to_admit_s"] for e in blocks]
    print(f"  hybrid {hy} med={np.median(hy):.2f}")
    print(f"  reconcile {rc} med={np.median(rc):.2f}")
    print(f"  reconcile faster: {sum(r < h for r, h in zip(rc, hy))}/6")


def event_loss_figure(data: dict) -> None:
    """Fault consequence: slow share over time + admitted backends per run."""
    fig, (axa, axb) = plt.subplots(
        1, 2, figsize=(11, 4.4), gridspec_kw={"width_ratios": [1.55, 1]})
    panel_a(axa, data)
    panel_b(axb, data)
    fig.tight_layout()
    out = OUT / "rq3_event_loss_outcome.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")

    for arm in ("event_only", "hybrid", "reconcile"):
        admitted = [data[f"b{b}_loss_all_{arm}"]["admissions"]["admitted"]
                    for b in BLOCKS]
        abandoned = [data[f"b{b}_loss_all_{arm}"]["admissions"]["abandoned"]
                     for b in BLOCKS]
        print(f"  {arm}: admitted {admitted}  abandoned {abandoned}")


def soundness_figure() -> None:
    """Timing-lie grid: episode damage against the claim lead (single panel).

    v2 (2026-10-05): the two-panel v1 included the retired false-claim
    family; the thesis now reports the timing grid only.
    """
    damage = json.loads(SND_DAMAGE.read_text(encoding="utf-8"))
    cross = json.loads(SND_CROSS.read_text(encoding="utf-8"))
    medians = damage["medians"]

    def seed_values(arm: str, cell: str) -> list[float]:
        record = medians.get(f"{arm}|{cell}")
        if record is None:
            return []
        return [record["seeds_ok"][s] for s in sorted(record["seeds_ok"])]

    fig, ax = plt.subplots(figsize=(6.4, 4.2))

    leads = [2, 5, 10]
    for arm in ("event_only", "hybrid", "reconcile"):
        meds = [medians[f"{arm}|premature{n}"]["d_bar_pp"] for n in leads]
        ax.plot(leads, meds, color=COLORS[arm], lw=1.9, marker="o", ms=5,
                label=LABELS[arm])
        for n in leads:
            for value in seed_values(arm, f"premature{n}"):
                ax.scatter(n + RNG.uniform(-0.09, 0.09), value, s=20,
                           color=INK, alpha=0.8, linewidths=0, zorder=4)
    wk = medians["wake_verify|premature10"]
    ax.scatter([10], [wk["d_bar_pp"]], s=36, color=COL_SND["wake_verify"],
               zorder=5, label=LABELS_SND["wake_verify"])
    for value in seed_values("wake_verify", "premature10"):
        ax.scatter(10 + RNG.uniform(-0.09, 0.09), value, s=20,
                   color=INK, alpha=0.8, linewidths=0, zorder=4)
    ax.axhline(0, color="#888", lw=0.9, zorder=1)
    ax.set_xticks(leads, ["2", "5", "10"])
    ax.set_xlabel("claim lead (s)")
    ax.set_ylabel("episode damage (pp)")
    ax.set_ylim(-13, 18.5)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.legend(loc="upper left", frameon=False)

    fig.tight_layout()
    out = OUT_SND / "rq3_soundness_damage_v2.png"
    fig.savefig(out)
    plt.close(fig)
    shutil.copyfile(out, ROOT / "tese/images/rq3_soundness_damage_v2.png")
    print(f"wrote {out} and tese/images/rq3_soundness_damage_v2.png")
    print(f"  band: {cross['crossover']['band']} "
          f"n*={cross['crossover']['n_star']} "
          f"gate_pass={cross['growth']['gate_pass']}")


def load_completion() -> dict:
    return json.loads(COMPL_DATA.read_text(encoding="utf-8"))


def panel_intervals_modes(ax, data: dict, letter: str = "(a)") -> None:
    """Provisioning -> first successful response per backend, four modes."""
    groups = [(mode, stratum) for mode in MODE_ORDER
              for stratum in ("fast", "slow")]
    xs = [0.0, 1.0, 2.6, 3.6, 5.2, 6.2, 7.8, 8.8]
    for x, (mode, stratum) in zip(xs, groups):
        values = np.array(
            [b["s"] for record in data["intact"].values()
             if record["arm"] == mode for b in record["backends"]
             if b["stratum"] == stratum], dtype=float)
        if len(values) == 0:
            ax.text(x, 7.0, "no slow-class\nbackends", ha="center",
                    va="bottom", fontsize=8.2, color=INK)
            continue
        median = float(np.median(values))
        ax.bar(x, median, 0.6, color=COLORS[mode], alpha=0.85,
               edgecolor="white", zorder=2)
        ax.errorbar(x, median,
                    yerr=[[median - values.min()], [values.max() - median]],
                    fmt="none", ecolor=INK, capsize=3, lw=1.2, zorder=3)
        jitter = RNG.uniform(-0.16, 0.16, len(values))
        ax.scatter(x + jitter, values, s=13, color=INK, alpha=0.75,
                   linewidths=0, zorder=4)
        ax.text(x, values.max() + 0.7, f"{median:.1f}", ha="center",
                fontsize=9)
    ax.set_xticks(xs, ["fast", "slow"] * 4)
    for label, (mode, _stratum) in zip(ax.get_xticklabels(), groups):
        label.set_color(COLORS[mode])
    ax.set_ylim(0, 27.5)
    ax.set_ylabel("provisioning \u2192 first success (s)")
    transform = ax.get_xaxis_transform()
    mode_short = {"event_only": "notification", "hybrid": "fallback check",
                  "reconcile": "polling",
                  "wake_verify": "confirmation check"}
    for x, mode in zip((0.5, 3.1, 5.7, 8.3), MODE_ORDER):
        ax.text(x, -0.115, mode_short[mode], transform=transform, ha="center",
                va="top", fontsize=8.4, color=COLORS[mode])
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle=":")
    ax.set_axisbelow(True)
    ax.text(-0.16, 1.05, letter, transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def panel_intervals_modes_fast(ax, data: dict, letter: str = "(a)") -> None:
    """Provisioning -> first successful response, fast-binding backends only.

    The stalled start-up class is left out because it carries no information
    about the admission mode. It appears only in the runs recorded before the
    socket-bind fault was fixed (every run from 2026-10-11 onward binds under a
    second), and where it occurs it adds about ten seconds to every mode alike,
    so the figure would otherwise compare calendar eras rather than modes.
    """
    xs = list(range(len(MODE_ORDER)))
    for x, mode in zip(xs, MODE_ORDER):
        values = np.array(
            [b["s"] for record in data["intact"].values()
             if record["arm"] == mode for b in record["backends"]
             if b["stratum"] == "fast"], dtype=float)
        if len(values) == 0:
            continue
        median = float(np.median(values))
        ax.bar(x, median, 0.58, color=COLORS[mode], alpha=0.85,
               edgecolor="white", zorder=2)
        ax.errorbar(x, median,
                    yerr=[[median - values.min()], [values.max() - median]],
                    fmt="none", ecolor=INK, capsize=3, lw=1.2, zorder=3)
        jitter = RNG.uniform(-0.15, 0.15, len(values))
        ax.scatter(x + jitter, values, s=13, color=INK, alpha=0.75,
                   linewidths=0, zorder=4)
        ax.text(x, values.max() + 0.5, f"{median:.1f}", ha="center",
                fontsize=9)
        print(f"  {mode}/fast: n={len(values)} med={median:.2f}")
    ax.set_xticks(xs, ["notification", "fallback check", "polling",
                       "confirmation check"])
    for label, mode in zip(ax.get_xticklabels(), MODE_ORDER):
        label.set_color(COLORS[mode])
    ax.set_ylim(0, 20.0)
    ax.set_yticks([0, 5, 10, 15, 20])
    ax.set_ylabel("provisioning \u2192 first success (s)")
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle=":")
    ax.set_axisbelow(True)
    ax.text(-0.16, 1.05, letter, transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def intervals_v4_figure() -> None:
    """Intact intervals on fast-binding backends (the build's v8 render)."""
    data = load_completion()
    probes = load_cell12()
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.4))
    panel_intervals_modes_fast(axes[0], data, letter="(a)")
    panel_intervals_c(axes[1], probes, letter="(b)")
    fig.tight_layout()
    out = OUT_COMPL / "rq3_admission_intervals_v8.png"
    fig.savefig(out)
    plt.close(fig)
    shutil.copyfile(out, ROOT / "tese/images/rq3_admission_intervals_v8.png")
    print(f"wrote {out} and tese/images/rq3_admission_intervals_v8.png")


def intervals_v3_figure() -> None:
    """Intact intervals with all four modes (v3, archive: both strata)."""
    data = load_completion()
    probes = load_cell12()
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.4))
    panel_intervals_modes(axes[0], data, letter="(a)")
    panel_intervals_c(axes[1], probes, letter="(b)")
    fig.tight_layout()
    out = OUT_COMPL / "rq3_admission_intervals_v7.png"
    fig.savefig(out)
    plt.close(fig)
    shutil.copyfile(out, ROOT / "tese/images/rq3_admission_intervals_v7.png")
    print(f"wrote {out} and tese/images/rq3_admission_intervals_v7.png")
    for mode in MODE_ORDER:
        for stratum in ("fast", "slow"):
            values = [b["s"] for record in data["intact"].values()
                      if record["arm"] == mode for b in record["backends"]
                      if b["stratum"] == stratum]
            median = f"{np.median(values):.2f}" if values else "n/a"
            print(f"  {mode}/{stratum}: n={len(values)} med={median}")


def pooled_series_modes(data: dict, mode: str) -> tuple[list[int], list[float]]:
    """Median slow share (%) per 10 s bin across the seven loss blocks."""
    buckets: dict[int, list[float]] = {}
    for record in data["lost"].values():
        if record["arm"] != mode:
            continue
        for point in record["series"]:
            if point["slow_rate"] is None or point["service"] < BIN_MIN_SERVICE:
                continue
            buckets.setdefault(round(point["t_rel"]), []).append(
                point["slow_rate"])
    xs = sorted(buckets)
    ys = [100.0 * float(np.median(buckets[t])) for t in xs]
    return xs, ys


def panel_a_modes(ax, data: dict) -> None:
    x_last = -60
    for mode in ("reconcile", "wake_verify", "hybrid", "event_only"):
        xs, ys = pooled_series_modes(data, mode)
        x_last = max(x_last, xs[-1])
        ax.plot(xs, ys, color=COLORS[mode], lw=1.9, label=LABELS_PLOT[mode])
    ax.axvspan(0, ONSET_S, color=PALETTE["grey"], alpha=0.10, lw=0)
    ax.set_xlabel("time since plateau onset (s)")
    ax.set_ylabel("slow share (%)")
    ax.set_xlim(-65, x_last + 5)
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle=":")
    ax.set_axisbelow(True)
    ax.legend(loc="upper right", frameon=False, fontsize=8.6)
    top = ax.get_ylim()[1]
    ax.text(ONSET_S - 4, top * 0.985, "onset window", ha="right", va="top",
            fontsize=9.5, color=INK)
    ax.text(-0.10, 1.045, "(a)", transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def panel_b_modes(ax, data: dict) -> None:
    for i, mode in enumerate(MODE_ORDER):
        values = np.array(
            [data["lost"][f"b{block}_{mode}"]["admissions"]["admitted"]
             for block in COMPL_BLOCKS], dtype=float)
        median = float(np.median(values))
        ax.bar(i, median, 0.5, color=COLORS[mode], alpha=0.85,
               edgecolor="white", zorder=2)
        if values.min() != values.max():
            ax.errorbar(i, median,
                        yerr=[[median - values.min()],
                              [values.max() - median]],
                        fmt="none", ecolor=INK, capsize=3, lw=1.2, zorder=3)
        jitter = np.linspace(-0.12, 0.12, len(values))
        ax.scatter(i + jitter, values, color=INK, s=20, zorder=4)
        ax.text(i, median + 0.22 if median > 0 else 0.6, f"{int(median)}",
                ha="center", fontsize=10, fontweight="bold")
    ax.set_xticks(np.arange(4), ["notification", "fallback\ncheck",
                                 "polling", "confirmation\ncheck"])
    for label, mode in zip(ax.get_xticklabels(), MODE_ORDER):
        label.set_color(COLORS[mode])
    ax.set_ylabel("admitted backends per run")
    ax.set_ylim(0, 7.8)
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle=":")
    ax.set_axisbelow(True)
    ax.text(0.03, 0.97, "notification runs: 0 admitted;\n"
            "32\u201335 candidates per run abandoned",
            transform=ax.transAxes, va="top", fontsize=8.8)
    ax.text(-0.16, 1.045, "(b)", transform=ax.transAxes, fontweight="bold",
            fontsize=11)


def event_loss_figure_v2() -> None:
    """Lost-message outcome with the four modes over seven completion blocks."""
    data = load_completion()
    fig, (axa, axb) = plt.subplots(
        1, 2, figsize=(11.2, 4.4), gridspec_kw={"width_ratios": [1.5, 1]})
    panel_a_modes(axa, data)
    panel_b_modes(axb, data)
    fig.tight_layout()
    out = OUT_COMPL / "rq3_event_loss_outcome_v4.png"
    fig.savefig(out)
    plt.close(fig)
    shutil.copyfile(out, ROOT / "tese/images/rq3_event_loss_outcome_v4.png")
    print(f"wrote {out} and tese/images/rq3_event_loss_outcome_v4.png")
    for mode in MODE_ORDER:
        admitted = [data["lost"][f"b{b}_{mode}"]["admissions"]["admitted"]
                    for b in COMPL_BLOCKS]
        print(f"  {mode}: admitted {admitted}")


def premature_damage_figure() -> None:
    """Premature-message damage at the 10 s lead, per mode (single panel).

    Pair medians and per-pair dots; the block-1 wake pair is re-included
    under the 2026-10-14 ruling (2-4 % marginal variation is a note, not
    a drop); the dropped event-only block-5 pair (8.93 % partner tail,
    outside the ruling) stays dropped.
    """
    full = json.loads(COMPL_PREMATURE_FULL.read_text(encoding="utf-8"))
    wake = json.loads(COMPL_PREMATURE_WAKE.read_text(encoding="utf-8"))
    rules = dict(full["E-P3"]["rules"])
    rules["wake_verify"] = wake["E-P3"]["rules"]["wake_verify"]

    fig, ax = plt.subplots(figsize=(7.0, 4.3))
    for i, mode in enumerate(MODE_ORDER):
        values = [pair["d_s"] for pair in rules[mode]["pairs"]
                  if pair.get("status") == "ok"]
        if mode == "wake_verify":
            values.append(WAKE_B1_D_S)
        values = np.array(values, dtype=float)
        median = float(np.median(values))
        ax.bar(i, median, 0.5, color=COLORS[mode], alpha=0.85,
               edgecolor="white", zorder=2)
        if values.min() != values.max():
            ax.errorbar(i, median,
                        yerr=[[median - values.min()],
                              [values.max() - median]],
                        fmt="none", ecolor=INK, capsize=3, lw=1.2, zorder=3)
        jitter = RNG.uniform(-0.13, 0.13, len(values))
        ax.scatter(i + jitter, values, color=INK, s=20, alpha=0.85,
                   linewidths=0, zorder=4)
        stored = rules[mode].get("median_d_pp")
        label_value = (float(np.median(values))
                       if mode == "wake_verify" or stored is None
                       else float(stored))
        eps = 1e-9 if label_value >= 0 else -1e-9
        ax.text(i, values.max() + 0.9, f"{label_value + eps:+.1f}",
                ha="center", fontsize=9.5, fontweight="bold")
    ax.axhline(0, color=INK, lw=0.9, zorder=1)
    ax.axhline(2, color=INK, lw=0.9, linestyle=":", zorder=1)
    ax.text(3.62, 2.65, "2 pp bound", ha="left", fontsize=8.4,
            color=INK)
    ax.set_xticks(np.arange(4), ["backend\nnotification", "fallback\ncheck",
                                 "periodic\npolling", "confirmation\ncheck"])
    for label, mode in zip(ax.get_xticklabels(), MODE_ORDER):
        label.set_color(COLORS[mode])
    ax.set_ylabel("episode damage (pp)")
    ax.set_xlim(-0.55, 4.05)
    ax.set_ylim(-10, 27)
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle=":")
    ax.set_axisbelow(True)
    ax.text(0.035, 0.045, "claim \u2192 first successful response:\n"
            "11.0 s (confirmation check) vs 19.5 s (periodic polling)",
            transform=ax.transAxes, va="bottom", fontsize=8.6)
    fig.tight_layout()
    out = OUT_COMPL / "rq3_premature_damage_v3.png"
    fig.savefig(out)
    plt.close(fig)
    shutil.copyfile(out, ROOT / "tese/images/rq3_premature_damage_v3.png")
    print(f"wrote {out} and tese/images/rq3_premature_damage_v3.png")
    for mode in MODE_ORDER:
        values = [pair["d_s"] for pair in rules[mode]["pairs"]
                  if pair.get("status") == "ok"]
        if mode == "wake_verify":
            values.append(WAKE_B1_D_S)
        print(f"  {mode}: n={len(values)} med={np.median(values):+.2f} pp")


def main() -> None:
    """Render the three thesis-carrying figures of the completion campaign.

    The earlier renders (`interval_figure`, `event_loss_figure`,
    `ordering_figure`, `soundness_figure`) are archive-only: regenerate
    them explicitly if ever needed; they are not part of the build.
    """
    intervals_v4_figure()
    premature_damage_figure()


if __name__ == "__main__":
    main()
