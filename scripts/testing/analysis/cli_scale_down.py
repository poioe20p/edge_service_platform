"""cli_scale_down — reconstruct the scale-down predicate from CSV and cross-check logs.

Mirrors the ceiling-skip semantics of scaling_policy.py exactly: when
T_proc/T_db exceeds the timeout ceiling the deque is NOT updated, so
ceiling-skipped windows are excluded from hit counts.

Usage:
    python -m source.scripts.testing.analysis.cli_scale_down --run-dir <dir>
"""
from __future__ import annotations

import argparse
import warnings
from collections import defaultdict, deque
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — mirror scaling_config.py defaults.
# If scaling thresholds are re-tuned, update these constants in ONE place here.
# ---------------------------------------------------------------------------
TAU_CPU_DOWN = 65.0
TAU_PROC_DOWN_MS = 5.0
TAU_STORAGE_CPU_DOWN = 65.0
TAU_DB_DOWN_MS = 100.0
SCALE_DOWN_PROC_TIMEOUT_CEILING_MS = 5000.0
SCALE_DOWN_DB_TIMEOUT_CEILING_MS = 5000.0
SCALE_DOWN_COMPUTE_REQUIRED = 3
SCALE_DOWN_STORAGE_REQUIRED = 3
SCALE_DOWN_COMPUTE_WINDOW_SIZE = 5
SCALE_DOWN_STORAGE_WINDOW_SIZE = 5


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _col(row: dict, *names: str, default=0.0):
    """Return the first available column value from *names* (backward compat)."""
    for name in names:
        val = row.get(name)
        if val is not None and str(val).strip() != "":
            return _safe_float(val, default)
    return default


def predicate_timeline(run) -> list[dict]:
    """Reconstruct the per-window predicate state from domain_rows."""
    rows = []
    for r in run.domain_rows:
        cpu   = _col(r, "average_cpu_percent", "median_cpu_percent")
        proc  = _col(r, "avg_time_proc_ms", "median_time_proc_ms")
        stcpu = _col(r, "avg_storage_cpu_percent", "median_storage_cpu_percent")
        tdb   = _col(r, "avg_time_db_ms", "median_time_db_ms")

        ceiling_skip_compute = proc > SCALE_DOWN_PROC_TIMEOUT_CEILING_MS
        ceiling_skip_storage = tdb  > SCALE_DOWN_DB_TIMEOUT_CEILING_MS

        rows.append({
            "phase":      r.get("phase", ""),
            "network_id": r.get("network_id", ""),
            "t":          _safe_float(r.get("window_end", 0)) - run.t0,
            "ceiling_skip_compute": ceiling_skip_compute,
            "ceiling_skip_storage": ceiling_skip_storage,
            "compute_cpu_below":    cpu  < TAU_CPU_DOWN,
            "compute_proc_below":   proc < TAU_PROC_DOWN_MS,
            "compute_below": (not ceiling_skip_compute
                              and cpu  < TAU_CPU_DOWN
                              and proc < TAU_PROC_DOWN_MS),
            "storage_cpu_below":  stcpu < TAU_STORAGE_CPU_DOWN,
            "storage_db_below":   tdb   < TAU_DB_DOWN_MS,
            "storage_below": (not ceiling_skip_storage
                              and stcpu < TAU_STORAGE_CPU_DOWN
                              and tdb   < TAU_DB_DOWN_MS),
        })
    return rows


def unreachable_report(timeline: list[dict]) -> dict:
    """Per phase, count windows where each half of the AND predicate failed.

    Ceiling-skipped windows are reported under a separate ``*_ceiling_skip``
    key and excluded from the per-condition counts.
    """
    by_phase: dict = defaultdict(lambda: defaultdict(int))
    for r in timeline:
        by_phase[r["phase"]]["n"] += 1
        if r["ceiling_skip_compute"]:
            by_phase[r["phase"]]["compute_ceiling_skip"] += 1
        else:
            if r["compute_cpu_below"]:
                by_phase[r["phase"]]["compute_cpu_below"] += 1
            if r["compute_proc_below"]:
                by_phase[r["phase"]]["compute_proc_below"] += 1
        if r["ceiling_skip_storage"]:
            by_phase[r["phase"]]["storage_ceiling_skip"] += 1
        else:
            if r["storage_cpu_below"]:
                by_phase[r["phase"]]["storage_cpu_below"] += 1
            if r["storage_db_below"]:
                by_phase[r["phase"]]["storage_db_below"] += 1
    return dict(by_phase)


def reconstructed_hits(timeline: list[dict]) -> dict[str, list[int]]:
    """Reconstruct hit counters for compute and storage, respecting ceiling-skip."""
    compute_deque: deque[bool] = deque(maxlen=SCALE_DOWN_COMPUTE_WINDOW_SIZE)
    storage_deque: deque[bool] = deque(maxlen=SCALE_DOWN_STORAGE_WINDOW_SIZE)
    result: dict[str, list[int]] = {"compute": [], "storage": []}
    for r in timeline:
        if not r["ceiling_skip_compute"]:
            compute_deque.append(r["compute_below"])
        result["compute"].append(sum(compute_deque))
        if not r["ceiling_skip_storage"]:
            storage_deque.append(r["storage_below"])
        result["storage"].append(sum(storage_deque))
    return result


def run(run_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[cli_scale_down] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .phase_window import phase_boundaries
    from .plots import shade_phases, overlay_events

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    timeline = predicate_timeline(r)
    report = unreachable_report(timeline)
    hits = reconstructed_hits(timeline)

    # ── Print the unreachable report ─────────────────────────────────────────
    print(f"\n{'Phase':<30} {'n':>5} "
          f"{'cpu<':>8} {'proc<':>7} {'stCpu<':>8} {'db<':>7} "
          f"{'ceilC':>7} {'ceilS':>7}")
    print("-" * 80)
    for phase, counts in sorted(report.items()):
        n = counts.get("n", 0)
        print(
            f"{phase:<30} {n:>5} "
            f"{counts.get('compute_cpu_below', 0):>8} "
            f"{counts.get('compute_proc_below', 0):>7} "
            f"{counts.get('storage_cpu_below', 0):>8} "
            f"{counts.get('storage_db_below', 0):>7} "
            f"{counts.get('compute_ceiling_skip', 0):>7} "
            f"{counts.get('storage_ceiling_skip', 0):>7}"
        )

    # ── Log event overlay check ──────────────────────────────────────────────
    log_down_evals = [ev for ev in r.events if ev.kind == "down_eval"]
    if not log_down_evals:
        warnings.warn(
            "No [scale-down] eval lines found in controller logs. "
            "Run with LOG_LEVEL=DEBUG to see per-window eval lines."
        )
        print("\n[cli_scale_down] NOTE: no DEBUG eval lines in logs — "
              "showing CSV-reconstructed predicate only.")

    # ── Plot ─────────────────────────────────────────────────────────────────
    t_domain = [row["t"] for row in timeline]
    bounds = phase_boundaries(r.t0, r.phases)

    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    fig.suptitle(f"Scale-down predicate — {run_dir.name}", fontsize=12)

    # Panel 0: compute hit counter vs required
    ax = axes[0]
    ax.plot(t_domain, hits["compute"], color="#1a7abf", label="compute hits (reconstructed)")
    ax.axhline(SCALE_DOWN_COMPUTE_REQUIRED, color="#1a7abf", linestyle="--",
               linewidth=0.8, label=f"required={SCALE_DOWN_COMPUTE_REQUIRED}")
    # Overlay log armed events
    for ev in r.events:
        if ev.kind == "armed" and ev.tier == "compute":
            ax.axvline(ev.ts - r.t0, color="#bf1a1a", linestyle=":", linewidth=0.8)
    ax.set_ylabel("hits in window")
    ax.set_title("Compute scale-down hits")
    ax.legend(fontsize=7)
    shade_phases(ax, bounds, r.t0)

    # Panel 1: storage hit counter vs required
    ax = axes[1]
    ax.plot(t_domain, hits["storage"], color="#bf5a1a", label="storage hits (reconstructed)")
    ax.axhline(SCALE_DOWN_STORAGE_REQUIRED, color="#bf5a1a", linestyle="--",
               linewidth=0.8, label=f"required={SCALE_DOWN_STORAGE_REQUIRED}")
    for ev in r.events:
        if ev.kind == "armed" and ev.tier == "storage":
            ax.axvline(ev.ts - r.t0, color="#bf1a1a", linestyle=":", linewidth=0.8)
    ax.set_ylabel("hits in window")
    ax.set_title("Storage scale-down hits")
    ax.legend(fontsize=7)
    shade_phases(ax, bounds, r.t0)

    # Panel 2: raw metric values vs thresholds
    ax = axes[2]
    cpu_vals = [_col(row, "average_cpu_percent", "median_cpu_percent") for row in r.domain_rows]
    proc_vals = [_col(row, "avg_time_proc_ms", "median_time_proc_ms") for row in r.domain_rows]
    tdb_vals = [_col(row, "avg_time_db_ms", "median_time_db_ms") for row in r.domain_rows]
    ax.plot(t_domain, cpu_vals, color="#1a7abf", linewidth=0.8, label="CPU %")
    ax.axhline(TAU_CPU_DOWN, color="#1a7abf", linestyle="--", linewidth=0.6)
    ax.plot(t_domain, tdb_vals, color="#bf5a1a", linewidth=0.8, label="T_db ms")
    ax.axhline(TAU_DB_DOWN_MS, color="#bf5a1a", linestyle="--", linewidth=0.6)
    ax.set_ylabel("value")
    ax.set_xlabel("time (s)")
    ax.set_title("Raw metric values vs thresholds (dashed)")
    ax.legend(fontsize=7)
    shade_phases(ax, bounds, r.t0)

    plt.tight_layout()
    out_path = out_dir / "scale_down.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_scale_down] wrote {out_path}")

    _append_summary(out_dir / "summary.md", report)


def _append_summary(summary_path: Path, report: dict) -> None:
    with summary_path.open("a", encoding="utf-8") as f:
        f.write("\n## Scale-Down Audit\n\n")
        f.write("See `analysis/scale_down.png`. Predicate unreachable report:\n\n")
        f.write(f"| Phase | n | cpu< | proc< | stCpu< | db< | ceilC | ceilS |\n")
        f.write(f"|---|---|---|---|---|---|---|---|\n")
        for phase, counts in sorted(report.items()):
            n = counts.get("n", 0)
            f.write(
                f"| {phase} | {n} "
                f"| {counts.get('compute_cpu_below', 0)} "
                f"| {counts.get('compute_proc_below', 0)} "
                f"| {counts.get('storage_cpu_below', 0)} "
                f"| {counts.get('storage_db_below', 0)} "
                f"| {counts.get('compute_ceiling_skip', 0)} "
                f"| {counts.get('storage_ceiling_skip', 0)} |\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
