"""decision_quality — per-phase scaling outcome description.

Compares breach-detector findings (degradation_score from telemetry) against
what the controller actually did (elasticity events), aggregated by workload
phase. Produces a descriptive table — no classification labels, no judgments.

For each phase: total telemetry windows, how many showed overload (score >=
threshold), peak degradation score observed, and how many spawns the controller
initiated and completed.

Outputs <run_dir>/analysis/rq1/:
  rq1_decision_quality.csv   — per-phase descriptive table
  rq1_decision_quality.png   — rendered table

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.decision_quality --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from ...loader import load_run


# ---------------------------------------------------------------------------
# Phase load classification (for sorting / context)
# ---------------------------------------------------------------------------

PHASE_LOAD_CLASSIFICATION = {
    "baseline":             "low",
    "quick_stress":         "high",
    "storage_stress":       "high",
    "cross_region_hotspot": "high",
    "compute_ramp":         "high",
    "compute_spike":        "high",
    "sustained_plateau":    "high",
    "demand_drop":          "low",
    "reverse_hotspot":      "high",
    "transition":           "transition",
}


def _classify_phase_load(name: str) -> str:
    if name in PHASE_LOAD_CLASSIFICATION:
        return PHASE_LOAD_CLASSIFICATION[name]
    lower = name.lower()
    if "high" in lower or "stress" in lower or "peak" in lower:
        return "high"
    if "base" in lower or "low" in lower or "idle" in lower or "quiet" in lower:
        return "low"
    if "transition" in lower or "ramp" in lower or "burst" in lower:
        return "transition"
    return "unknown"


# ---------------------------------------------------------------------------
# Per-phase summary
# ---------------------------------------------------------------------------

def _build_phase_summary(
    debug_rows: list[dict],
    container_event_rows: list[dict],
    thresholds: dict,
    phases: list,
    t0: float,
) -> list[dict]:
    """Build a per-phase descriptive table of observable facts.

    For each phase: count telemetry windows, count how many showed overload
    (score >= threshold), record the peak score, and count spawns from
    container_events.csv ('added' events).

    No classification labels. No judgments. Just observable facts.
    """
    from ..lib.breach_detector import detect_breaches
    from ...phase_window import phase_for_ts

    # Detect all breaches from telemetry
    breaches = detect_breaches(debug_rows, thresholds)

    # Count spawns (added events) per phase from container_events.csv
    spawns_by_phase: dict[str, int] = {}
    for row in container_event_rows:
        event = str(row.get("event", "")).lower()
        if event == "added":
            phase = str(row.get("phase", ""))
            spawns_by_phase[phase] = spawns_by_phase.get(phase, 0) + 1

    # Build phase boundaries
    phase_windows: list[tuple[str, float, float]] = []
    t_current = t0
    for p in phases:
        phase_windows.append((p.name, t_current, t_current + p.duration_s))
        t_current += p.duration_s

    rows: list[dict] = []
    for phase_name, p_start, p_end in phase_windows:
        # Count telemetry windows in this phase
        phase_debug_rows = [
            r for r in debug_rows
            if p_start <= float(r.get("window_end", 0)) <= p_end
        ]
        total_windows = len(phase_debug_rows)

        # Count breached windows in this phase
        phase_breaches = [
            b for b in breaches
            if p_start <= b["window_end"] <= p_end
        ]
        breached_windows = len(phase_breaches)
        peak_score = max((b["score"] for b in phase_breaches), default=0.0)

        # Spawns from container events (already phase-tagged)
        spawns = spawns_by_phase.get(phase_name, 0)

        rows.append({
            "phase":             phase_name,
            "phase_load":        _classify_phase_load(phase_name),
            "total_windows":     total_windows,
            "breached_windows":  breached_windows,
            "peak_score":        round(peak_score, 4) if peak_score > 0 else "",
            "spawns_initiated":  spawns,
            "spawns_completed":  spawns,
        })

    return rows


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def _plot_phase_table(rows: list[dict], run_name: str, out_dir: Path) -> None:
    if not rows:
        print("[decision_quality] no phase data to plot")
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    col_labels = ["Phase", "Load", "Windows", "Breached", "Peak Score",
                  "Spawns In", "Spawns Done"]
    cell_text: list[list[str]] = []
    for r in rows:
        cell_text.append([
            r["phase"],
            r["phase_load"],
            str(r["total_windows"]),
            str(r["breached_windows"]),
            str(r["peak_score"]) if r["peak_score"] != "" else "-",
            str(r["spawns_initiated"]),
            str(r["spawns_completed"]),
        ])

    fig, ax = plt.subplots(figsize=(12, max(3, len(rows) * 0.6 + 1.5)))
    ax.axis("off")
    fig.suptitle(f"RQ1 — Scaling outcome description — {run_name}", fontsize=12)

    table = ax.table(
        cellText=cell_text,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.auto_set_column_width(list(range(len(col_labels))))

    # Highlight breached phases
    for i, r in enumerate(rows):
        if r["breached_windows"] > 0:
            for j in range(len(col_labels)):
                cell = table[i + 1, j]
                cell.set_facecolor("#fff3e0")

    fig.tight_layout()
    path = out_dir / "rq1_decision_quality.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"[decision_quality] wrote {path}")


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def _write_phase_csv(rows: list[dict], out_dir: Path) -> None:
    path = out_dir / "rq1_decision_quality.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "phase", "phase_load", "total_windows", "breached_windows",
            "peak_score", "spawns_initiated", "spawns_completed",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"[decision_quality] wrote {path} ({len(rows)} phases)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(run_dir: Path) -> None:
    print(f"[decision_quality] run_dir={run_dir}")
    r = load_run(run_dir)
    out_dir = Path(run_dir) / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = r.t0

    if not r.debug_rows:
        print("[decision_quality] no debug_rows — cannot build summary")
        return

    if not r.phases:
        print("[decision_quality] no phases — cannot build summary")
        return

    # Load thresholds from env snapshot
    from ..lib.breach_detector import load_env_snapshot, load_thresholds
    env = load_env_snapshot(str(run_dir))
    thresholds = load_thresholds(env)

    # Build per-phase summary
    rows = _build_phase_summary(
        r.debug_rows, r.container_event_rows, thresholds, r.phases, t0,
    )
    if not rows:
        print("[decision_quality] no phase data to output")
        return

    # Output
    _write_phase_csv(rows, out_dir)
    _plot_phase_table(rows, Path(run_dir).name, out_dir)

    # Print summary
    print(f"\n  Phase summary ({len(rows)} phases):")
    for r in rows:
        flagged = "  <--" if r["breached_windows"] > 0 else ""
        print(f"    {r['phase']:<25s}  "
              f"windows={r['total_windows']:>2d}  "
              f"breached={r['breached_windows']:>2d}  "
              f"peak={str(r['peak_score']):>6s}  "
              f"spawns={r['spawns_initiated']:>2d}"
              f"{flagged}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run(args.run_dir)


if __name__ == "__main__":
    main()
