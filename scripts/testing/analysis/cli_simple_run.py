"""cli_simple_run — simple run-level service and node-count plots.

Produces <run_dir>/analysis/simple_run.png with six panels:
  - average latency over time
  - p95 latency over time
  - p99 latency over time
  - failure rate over time
  - total active nodes over time
  - active nodes by type over time

Usage:
    python -m source.scripts.testing.analysis.cli_simple_run --run-dir <dir>
"""
from __future__ import annotations

import argparse
from pathlib import Path


def run(run_dir: Path, bucket_s: int = 30) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[cli_simple_run] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .phase_window import phase_boundaries
    from .plots import shade_phases
    from .simple_metrics import (
        bucket_client_rows,
        build_container_step_series,
        infer_end_s,
        infer_origin_ts,
    )

    r = load_run(run_dir)
    out_dir = Path(run_dir) / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    origin_ts = infer_origin_ts(r)
    end_s = infer_end_s(r, origin_ts)
    bounds = phase_boundaries(origin_ts, r.phases)
    client_buckets = bucket_client_rows(r.all_client_rows, origin_ts, bucket_s=bucket_s)
    node_points = build_container_step_series(r.container_event_rows, origin_ts)

    fig, axes = plt.subplots(6, 1, figsize=(14, 19), sharex=True)
    fig.suptitle(f"Simple run summary — {Path(run_dir).name}", fontsize=12)

    x_bucket = [row["bucket_mid_s"] for row in client_buckets]

    ax = axes[0]
    ax.plot(x_bucket, [row["avg_latency_ms"] for row in client_buckets], color="#1a7abf", linewidth=1.6)
    ax.set_ylabel("ms")
    ax.set_title("Average latency")
    shade_phases(ax, bounds, origin_ts)

    ax = axes[1]
    ax.plot(x_bucket, [row["p95_latency_ms"] for row in client_buckets], color="#bf5a1a", linewidth=1.6)
    ax.set_ylabel("ms")
    ax.set_title("p95 latency")
    shade_phases(ax, bounds, origin_ts)

    ax = axes[2]
    ax.plot(x_bucket, [row["p99_latency_ms"] for row in client_buckets], color="#8b1a8b", linewidth=1.6)
    ax.set_ylabel("ms")
    ax.set_title("p99 latency")
    shade_phases(ax, bounds, origin_ts)

    ax = axes[3]
    ax.plot(x_bucket, [row["failure_rate_pct"] for row in client_buckets], color="#bf1a1a", linewidth=1.6)
    ax.set_ylabel("%")
    ax.set_title("Failure rate")
    shade_phases(ax, bounds, origin_ts)

    ax = axes[4]
    node_x = [row["t_s"] for row in node_points]
    ax.step(node_x, [row["total_nodes"] for row in node_points], where="post", color="#1a7abf", linewidth=1.6)
    ax.set_ylabel("nodes")
    ax.set_title("Total active nodes")
    shade_phases(ax, bounds, origin_ts)

    ax = axes[5]
    ax.step(node_x, [row["compute_nodes"] for row in node_points], where="post", color="#1a7abf", linewidth=1.5, label="compute")
    ax.step(node_x, [row["storage_nodes"] for row in node_points], where="post", color="#bf5a1a", linewidth=1.5, label="storage")
    ax.step(node_x, [row["selective_nodes"] for row in node_points], where="post", color="#1abf4a", linewidth=1.5, label="selective")
    ax.set_ylabel("nodes")
    ax.set_xlabel("time (s)")
    ax.set_title("Active nodes by type")
    ax.legend(fontsize=8)
    shade_phases(ax, bounds, origin_ts)

    if end_s > 0:
        axes[-1].set_xlim(0, end_s)

    plt.tight_layout()
    out_path = out_dir / "simple_run.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_simple_run] wrote {out_path}")

    _append_summary(out_dir / "summary.md", Path(run_dir))


def _append_summary(summary_path: Path, run_dir: Path) -> None:
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n## Simple Run Summary\n\nSee `analysis/simple_run.png` — generated from `{run_dir.name}`.\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    parser.add_argument("--bucket-s", type=int, default=30, metavar="SECONDS")
    args = parser.parse_args()
    run(Path(args.run_dir), bucket_s=args.bucket_s)


if __name__ == "__main__":
    main()