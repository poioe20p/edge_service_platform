"""timings — RQ1 decision staleness and reaction latency plots.

Produces <run_dir>/analysis/:
  rq1_staleness.png          — time-series of staleness per LAN
  rq1_staleness.csv          — per-phase staleness statistics
  rq1_reaction_latency.png   — stacked bar of reaction-latency segments
  rq1_reaction_latency.csv   — per-scaling-event latency breakdown

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.timings --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from ...loader import load_run
from ...phase_window import phase_boundaries
from ...plots import shade_phases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    from math import ceil, floor
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = floor(rank)
    upper = ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def compute_staleness(debug_rows: list[dict], origin_ts: float) -> list[dict]:
    """For each debug row: staleness_s = consumed_at - window_end."""
    results = []
    for row in debug_rows:
        window_end = _safe_float(row.get("window_end"))
        consumed_at = _safe_float(row.get("consumed_at"))
        if window_end <= 0 or consumed_at <= 0:
            continue
        results.append({
            "network_id": row.get("network_id", ""),
            "phase":      row.get("phase", ""),
            "window_end": window_end,
            "consumed_at": consumed_at,
            "staleness_s": max(0.0, consumed_at - window_end),
            "t_s": max(0.0, window_end - origin_ts),
        })
    return results


def _staleness_per_phase(staleness_rows: list[dict],
                         phases, t0: float) -> dict[str, dict]:
    """Aggregate staleness stats per phase."""
    groups: dict[str, list[float]] = {}
    for row in staleness_rows:
        phase = row.get("phase", "unknown")
        groups.setdefault(phase, []).append(row["staleness_s"])
    result = {}
    for phase, vals in groups.items():
        result[phase] = {
            "count": len(vals),
            "mean": sum(vals) / len(vals) if vals else 0.0,
            "p50": _percentile(vals, 0.50),
            "p95": _percentile(vals, 0.95),
            "max": max(vals) if vals else 0.0,
        }
    return result


# ---------------------------------------------------------------------------
# Reaction latency (breach-detector based)
# ---------------------------------------------------------------------------

def compute_reaction_latency(
    debug_rows: list[dict],
    events,
    thresholds: dict,
) -> list[dict]:
    """Detect breaches from telemetry, match to spawn completions.

    Uses the shared breach_detector module — does NOT parse controller
    log alert events. Breach start is the first telemetry window_end
    where degradation_score ≥ threshold.
    """
    from ..lib.breach_detector import detect_breaches

    breaches = detect_breaches(debug_rows, thresholds)

    # Index spawn_done events by (lan, tier).
    # Normalise ev.lan ("1"/"2" from log parser) to match
    # breach["network_id"] ("lan1"/"lan2" from telemetry data).
    spawns_by_tier: dict[tuple[str, str], list] = {}
    for ev in events:
        if ev.kind == "spawn_done":
            lan = f"lan{ev.lan}" if ev.lan in ("1", "2") else ev.lan
            key = (lan, ev.tier)
            spawns_by_tier.setdefault(key, []).append(ev)

    results = []
    for breach in breaches:
        key = (breach["network_id"], breach["tier"])
        candidates = sorted(
            [e for e in spawns_by_tier.get(key, [])
             if e.ts > breach["window_end"]],
            key=lambda e: e.ts,
        )
        if not candidates:
            continue

        spawn_done = candidates[0]
        spawn_start_ts = spawn_done.ts
        for ev in events:
            ev_lan = f"lan{ev.lan}" if ev.lan in ("1", "2") else ev.lan
            if (ev.kind == "spawn_start"
                    and ev_lan == breach["network_id"]
                    and ev.tier == breach["tier"]
                    and ev.ts < spawn_done.ts
                    and ev.ts > breach["window_end"]):
                spawn_start_ts = ev.ts
                break

        results.append({
            "breach_window_end": breach["window_end"],
            "lan": breach["network_id"],
            "tier": breach["tier"],
            "score": breach["score"],
            "threshold": breach["threshold"],
            "breach_detection_s": round(
                max(0.0, spawn_start_ts - breach["window_end"]), 3),
            "provision_time_s": round(
                max(0.0, spawn_done.ts - spawn_start_ts), 3),
            "total_reaction_s": round(
                max(0.0, spawn_done.ts - breach["window_end"]), 3),
        })
    return results


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _plot_staleness(staleness_rows: list[dict], run_name: str,
                    boundaries, t0: float, out_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[timings] matplotlib not installed — skipping staleness plot")
        return

    lans = sorted(set(r["network_id"] for r in staleness_rows))
    n_lans = max(1, len(lans))
    fig, axes = plt.subplots(n_lans, 1, figsize=(14, 3 * n_lans),
                             sharex=True, squeeze=False)
    fig.suptitle(f"Decision Staleness — {run_name}", fontsize=12)

    for idx, lan in enumerate(lans):
        ax = axes[idx][0]
        lan_rows = [r for r in staleness_rows if r["network_id"] == lan]
        t_vals = [r["t_s"] for r in lan_rows]
        s_vals = [r["staleness_s"] for r in lan_rows]
        ax.plot(t_vals, s_vals, color="#1a7abf", linewidth=1.0)
        ax.axhline(0, color="gray", linestyle="--", linewidth=0.5)
        ax.set_ylabel("staleness (s)")
        ax.set_title(f"LAN: {lan}")
        shade_phases(ax, boundaries, t0)

    axes[-1][0].set_xlabel("time (s)")
    plt.tight_layout()
    out_path = out_dir / "rq1_staleness.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[timings] wrote {out_path}")


def _plot_reaction_latency(reaction_rows: list[dict], run_name: str,
                           phases, out_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[timings] matplotlib not installed — skipping reaction plot")
        return

    if not reaction_rows:
        print("[timings] no reaction latency events to plot")
        return

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(f"Reaction Latency — {run_name}", fontsize=12)

    # Left: stacked horizontal bars with value labels
    ax = axes[0]
    labels = [f"{r['lan']}/{r['tier']}#{i}"
              for i, r in enumerate(reaction_rows)]
    detection_vals = [r["breach_detection_s"] for r in reaction_rows]
    provision_vals = [r["provision_time_s"] for r in reaction_rows]
    total_vals = [r["total_reaction_s"] for r in reaction_rows]

    y_pos = list(range(len(reaction_rows)))

    # Detection bar (always present)
    ax.barh(y_pos, detection_vals, color="#bf8c1a",
            label="breach → spawn_start")

    # Provision bar — only draw when > 0 (skip zero-width artifacts)
    for i, pv in enumerate(provision_vals):
        if pv > 0:
            ax.barh(i, pv, left=detection_vals[i],
                    color="#1a7abf", label="spawn_start → spawn_done" if i == 0 else "")

    # Value labels on each segment
    for i in y_pos:
        det = detection_vals[i]
        prv = provision_vals[i]
        tot = total_vals[i]
        # Detection label inside the bar
        ax.text(det / 2, i, f"{det:.1f}s", ha="center", va="center",
                fontsize=7, color="white", fontweight="bold")
        # Provision label inside the bar (only when > 0)
        if prv > 0:
            ax.text(det + prv / 2, i, f"{prv:.1f}s", ha="center", va="center",
                    fontsize=7, color="white", fontweight="bold")
        # Total label outside the bar — placed after with more room when N/D
        offset = 0.8 if prv == 0 else 0.3
        note = "  (prov N/D)" if prv == 0 else ""
        ax.text(tot + offset, i, f"{tot:.1f}s{note}", ha="left", va="center",
                fontsize=6, color="#333333")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("seconds")
    ax.set_xlim(0, max(total_vals) * 1.25 if total_vals else 10)
    # Deduplicate legend entries
    handles, legends = ax.get_legend_handles_labels()
    by_label = dict(zip(legends, handles))
    ax.legend(by_label.values(), by_label.keys(), fontsize=7)
    ax.set_title("Per-event reaction latency")

    # Right: per-phase summary table
    ax = axes[1]
    ax.axis("off")
    from ...phase_window import phase_for_ts

    phase_groups: dict[str, dict[str, list[float]]] = {}
    for r in reaction_rows:
        phase = phase_for_ts(r["breach_window_end"], 0.0, phases)
        phase_groups.setdefault(phase, {"detection": [], "provision": [],
                                         "total": []})
        phase_groups[phase]["detection"].append(r["breach_detection_s"])
        phase_groups[phase]["provision"].append(r["provision_time_s"])
        phase_groups[phase]["total"].append(r["total_reaction_s"])

    if phase_groups:
        text_lines = ["Phase        N  Det(p95)  Prov(p95)  Total(p95)",
                       "-" * 52]
        for phase, grp in sorted(phase_groups.items()):
            n = len(grp["total"])
            d95 = _percentile(grp["detection"], 0.95)
            p95 = _percentile(grp["provision"], 0.95)
            t95 = _percentile(grp["total"], 0.95)
            d_str = f"{d95:>7.2f}s" if grp["detection"] else "    N/D"
            p_str = f"{p95:>8.2f}s" if any(v > 0 for v in grp["provision"]) else "     N/D"
            t_str = f"{t95:>9.2f}s" if grp["total"] else "      N/D"
            text_lines.append(
                f"{phase:<12} {n:>2}  {d_str}  {p_str}  {t_str}")
        ax.text(0.05, 0.95, "\n".join(text_lines), transform=ax.transAxes,
                fontsize=7, fontfamily="monospace", verticalalignment="top")

    plt.tight_layout()
    out_path = out_dir / "rq1_reaction_latency.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[timings] wrote {out_path}")


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def _write_staleness_csv(staleness_rows: list[dict],
                         phases, t0: float, out_dir: Path) -> None:
    """Write per-row staleness and per-phase aggregate CSVs."""
    # Per-row CSV
    path = out_dir / "rq1_staleness.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "network_id", "phase", "window_end", "consumed_at",
            "staleness_s", "t_s"])
        w.writeheader()
        w.writerows(staleness_rows)
    print(f"[timings] wrote {path}")

    # Per-phase aggregate CSV
    per_phase = _staleness_per_phase(staleness_rows, phases, t0)
    phase_path = out_dir / "rq1_staleness_per_phase.csv"
    with phase_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "phase", "count", "mean_s", "p50_s", "p95_s", "max_s"])
        w.writeheader()
        for phase, stats in sorted(per_phase.items()):
            w.writerow({
                "phase": phase,
                "count": stats["count"],
                "mean_s": f"{stats['mean']:.4f}",
                "p50_s": f"{stats['p50']:.4f}",
                "p95_s": f"{stats['p95']:.4f}",
                "max_s": f"{stats['max']:.4f}",
            })
    print(f"[timings] wrote {phase_path}")


def _write_reaction_csv(reaction_rows: list[dict], out_dir: Path) -> None:
    path = out_dir / "rq1_reaction_latency.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "breach_window_end", "lan", "tier", "score", "threshold",
            "breach_detection_s", "provision_time_s", "total_reaction_s"])
        w.writeheader()
        w.writerows(reaction_rows)
    print(f"[timings] wrote {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(run_dir: Path) -> None:
    print(f"[timings] run_dir={run_dir}")
    r = load_run(run_dir)
    out_dir = Path(run_dir) / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = r.t0

    boundaries = phase_boundaries(t0, r.phases) if r.phases else []

    # ── Staleness ────────────────────────────────────────────────────
    staleness_rows = compute_staleness(r.debug_rows, t0)
    print(f"[timings] {len(staleness_rows)} valid staleness rows")
    if staleness_rows:
        _plot_staleness(staleness_rows, run_dir.name, boundaries, t0, out_dir)
        _write_staleness_csv(staleness_rows, r.phases, t0, out_dir)
    else:
        print("[timings] no staleness rows — "
              "check debug_csv has window_end/consumed_at")

    # ── Reaction latency ─────────────────────────────────────────────
    from ..lib.breach_detector import load_env_snapshot, load_thresholds
    env = load_env_snapshot(str(run_dir))
    thresholds = load_thresholds(env)
    reaction_rows = compute_reaction_latency(
        r.debug_rows, r.events, thresholds)
    print(f"[timings] {len(reaction_rows)} reaction latency events "
          f"(breach-detector based)")
    if reaction_rows:
        _plot_reaction_latency(reaction_rows, run_dir.name, r.phases, out_dir)
        _write_reaction_csv(reaction_rows, out_dir)
    else:
        print("[timings] no reaction latency events "
              "(no breaches matched to spawn_done)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run(args.run_dir)


if __name__ == "__main__":
    main()
