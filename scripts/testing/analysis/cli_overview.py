"""cli_overview — one-page dashboard for a run directory.

Produces <run_dir>/analysis/overview.png with stacked time-series panels:
  - request rate (from client CSV, concatenated across phases)
  - compute CPU (domain median + per-node thin lines)
  - storage CPU (domain median + per-node thin lines)
  - T_proc
  - T_db (total, read, write)
  - node counts (compute, storage)

Usage:
    python -m source.scripts.testing.analysis.cli_overview --run-dir <dir>
"""
from __future__ import annotations

import argparse
import os
import warnings
from pathlib import Path


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


def run(run_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[cli_overview] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .phase_window import phase_boundaries
    from .plots import shade_phases, overlay_events

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    bounds = phase_boundaries(r.t0, r.phases)
    t_domain = [_safe_float(row["window_end"]) - r.t0 for row in r.domain_rows]

    has_db_decomp = (
        any(row.get("avg_time_db_read_ms") for row in r.domain_rows)
        or any(row.get("avg_time_db_read_ms") for row in r.debug_rows)
    )
    # Prefer debug_rows for decomposed DB fields when domain_rows doesn't have them
    _db_rows = r.debug_rows if r.debug_rows and not any(
        row.get("avg_time_db_read_ms") for row in r.domain_rows
    ) else r.domain_rows
    has_nodes = bool(r.node_rows)

    # Figure 1: Latency (T_proc + T_db)
    fig1, (ax1a, ax1b) = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig1.suptitle(f"Latency - {run_dir.name}", fontsize=12)

    tproc = [_col(row, "avg_time_proc_ms", "median_time_proc_ms") for row in r.domain_rows]
    ax1a.plot(t_domain, tproc, color="#1abf4a")
    ax1a.set_ylabel("ms")
    ax1a.set_title("T_proc")
    shade_phases(ax1a, bounds, r.t0)

    tdb = [_col(row, "avg_time_db_ms", "median_time_db_ms") for row in r.domain_rows]
    ax1b.plot(t_domain, tdb, color="#bf1a8c", linewidth=1.5, label="T_db total")
    if has_db_decomp:
        tr = [_safe_float(row.get("avg_time_db_read_ms")) for row in _db_rows]
        tw = [_safe_float(row.get("avg_time_db_write_ms")) for row in _db_rows]
        ax1b.fill_between(t_domain, 0, tr, alpha=0.3, color="#1a7abf", label="read")
        ax1b.fill_between(t_domain, tr, [a + b for a, b in zip(tr, tw)],
                        alpha=0.3, color="#bf5a1a", label="write")
    ax1b.set_ylabel("ms")
    ax1b.set_title("T_db")
    ax1b.legend(fontsize=7)
    ax1b.set_xlabel("time (s)")
    shade_phases(ax1b, bounds, r.t0)

    fig1.tight_layout()
    fig1.savefig(out_dir / "overview_latency.png", dpi=150)
    plt.close(fig1)
    print(f"[cli_overview] wrote {out_dir / 'overview_latency.png'}")

    # Figure 2: Resources (compute CPU + storage CPU)
    fig2, (ax2a, ax2b) = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig2.suptitle(f"Resource usage - {run_dir.name}", fontsize=12)

    cpu = [_col(row, "average_cpu_percent", "median_cpu_percent") for row in r.domain_rows]
    ax2a.plot(t_domain, cpu, color="#1a7abf", linewidth=1.5, label="avg")
    if has_nodes:
        for sid in {row["server_id"] for row in r.node_rows if row.get("role") == "compute"}:
            nrows = [row for row in r.node_rows
                     if row.get("role") == "compute" and row["server_id"] == sid]
            nt = [_safe_float(row["window_end"]) - r.t0 for row in nrows]
            nc = [_safe_float(row.get("cpu_percent")) for row in nrows]
            ax2a.plot(nt, nc, color="#aac8e0", linewidth=0.5, alpha=0.6)
    ax2a.set_ylabel("CPU %")
    ax2a.set_title("Compute CPU")
    shade_phases(ax2a, bounds, r.t0)
    overlay_events(ax2a, r.events, r.t0, tier="compute")

    stcpu = [_col(row, "avg_storage_cpu_percent", "median_storage_cpu_percent") for row in r.domain_rows]
    ax2b.plot(t_domain, stcpu, color="#bf5a1a", linewidth=1.5, label="avg")
    if has_nodes:
        for sid in {row["server_id"] for row in r.node_rows if row.get("role") == "storage"}:
            nrows = [row for row in r.node_rows
                     if row.get("role") == "storage" and row["server_id"] == sid]
            nt = [_safe_float(row["window_end"]) - r.t0 for row in nrows]
            nc = [_safe_float(row.get("cpu_percent")) for row in nrows]
            ax2b.plot(nt, nc, color="#e0b89a", linewidth=0.5, alpha=0.6)
    ax2b.set_ylabel("CPU %")
    ax2b.set_title("Storage CPU")
    ax2b.set_xlabel("time (s)")
    shade_phases(ax2b, bounds, r.t0)
    overlay_events(ax2b, r.events, r.t0, tier="storage")

    fig2.tight_layout()
    fig2.savefig(out_dir / "overview_resources.png", dpi=150)
    plt.close(fig2)
    print(f"[cli_overview] wrote {out_dir / 'overview_resources.png'}")

    # Figure 3: Throughput (request rate + node counts)
    fig3, (ax3a, ax3b) = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig3.suptitle(f"Throughput & scale - {run_dir.name}", fontsize=12)

    req = [_safe_float(row.get("total_requests", 0)) for row in r.domain_rows]
    ax3a.plot(t_domain, req, color="#1a7abf")
    ax3a.set_ylabel("requests / window")
    ax3a.set_title("Request rate")
    shade_phases(ax3a, bounds, r.t0)
    overlay_events(ax3a, r.events, r.t0)

    nc = [_safe_float(row.get("server_count", 0)) for row in r.domain_rows]
    ns = [_safe_float(row.get("storage_count", 0)) for row in r.domain_rows]
    ax3b.plot(t_domain, nc, color="#1a7abf", label="compute")
    ax3b.plot(t_domain, ns, color="#bf5a1a", label="storage")
    ax3b.set_ylabel("nodes")
    ax3b.set_xlabel("time (s)")
    ax3b.set_title("Node counts")
    ax3b.legend(fontsize=7)
    shade_phases(ax3b, bounds, r.t0)
    overlay_events(ax3b, r.events, r.t0)

    fig3.tight_layout()
    fig3.savefig(out_dir / "overview_throughput.png", dpi=150)
    plt.close(fig3)
    print(f"[cli_overview] wrote {out_dir / 'overview_throughput.png'}")

    _append_summary(out_dir / "summary.md", run_dir)


def _append_summary(summary_path: Path, run_dir: Path) -> None:
    with summary_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## Overview\n\nSee `analysis/overview_latency.png`, "
                f"`analysis/overview_resources.png`, "
                f"`analysis/overview_throughput.png` — "
                f"generated from `{run_dir.name}`.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
