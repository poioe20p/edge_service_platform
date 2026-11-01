"""cli_simple_compare — simple comparison plots across multiple run directories.

Produces two PNGs in the chosen output directory:
  - simple_compare_overall.png
  - simple_compare_phase.png

Usage:
    python -m source.scripts.testing.analysis.cli_simple_compare \
        --run-dir <dir1> --run-dir <dir2> [--output-dir <dir>]
"""
from __future__ import annotations

import argparse
from pathlib import Path


def run(run_dirs: list[Path], output_dir: Path | None = None) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[cli_simple_compare] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .simple_metrics import (
        build_container_step_series,
        infer_end_s,
        infer_origin_ts,
        max_step_value,
        summarize_client_rows,
        summarize_client_rows_by_phase,
        time_weighted_mean,
    )

    runs = [load_run(Path(run_dir)) for run_dir in run_dirs]
    if not runs:
        print("[cli_simple_compare] no run directories provided")
        return

    out_dir = Path(output_dir) if output_dir is not None else Path.cwd() / "analysis_compare"
    out_dir.mkdir(parents=True, exist_ok=True)

    phase_names: list[str] = []
    for run_data in runs:
        for phase in run_data.phases:
            if phase.name not in phase_names:
                phase_names.append(phase.name)

    summaries: list[dict] = []
    for run_data in runs:
        origin_ts = infer_origin_ts(run_data)
        end_s = infer_end_s(run_data, origin_ts)
        node_points = build_container_step_series(run_data.container_event_rows, origin_ts)
        overall = summarize_client_rows(run_data.all_client_rows)
        phases = {
            row["phase"]: row
            for row in summarize_client_rows_by_phase(run_data.all_client_rows, phase_names)
        }
        summaries.append({
            "name": run_data.run_dir.name,
            "overall": overall,
            "phase": phases,
            "mean_total_nodes": time_weighted_mean(node_points, "total_nodes", end_s),
            "max_total_nodes": max_step_value(node_points, "total_nodes"),
        })

    _plot_overall_summary(plt, summaries, out_dir / "simple_compare_overall.png")
    _plot_phase_summary(plt, summaries, phase_names, out_dir / "simple_compare_phase.png")
    _write_summary(out_dir / "summary.md", summaries)
    print(f"[cli_simple_compare] wrote {out_dir / 'simple_compare_overall.png'}")
    print(f"[cli_simple_compare] wrote {out_dir / 'simple_compare_phase.png'}")


def _plot_overall_summary(plt, summaries: list[dict], out_path: Path) -> None:
    names = [summary["name"] for summary in summaries]
    x = list(range(len(names)))

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Simple comparison — overall", fontsize=12)

    plots = [
        ("Overall average latency", "avg_latency_ms", "ms", "#1a7abf"),
        ("Overall failure rate", "failure_rate_pct", "%", "#bf1a1a"),
        ("Mean total nodes", "mean_total_nodes", "nodes", "#1abf4a"),
        ("Max total nodes", "max_total_nodes", "nodes", "#bf5a1a"),
    ]

    for ax, (title, key, ylabel, colour) in zip(axes.flat, plots):
        values = []
        for summary in summaries:
            if key in ("mean_total_nodes", "max_total_nodes"):
                values.append(summary[key])
            else:
                values.append(summary["overall"][key])
        ax.bar(x, values, color=colour, alpha=0.85, edgecolor="black", linewidth=0.8)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, names, rotation=20, ha="right")

    plt.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def _plot_phase_summary(plt, summaries: list[dict], phase_names: list[str], out_path: Path) -> None:
    if not phase_names:
        return

    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True)
    fig.suptitle("Simple comparison — by phase", fontsize=12)

    run_count = max(1, len(summaries))
    width = min(0.8 / run_count, 0.28)
    x = list(range(len(phase_names)))

    for index, summary in enumerate(summaries):
        offset = (index - (run_count - 1) / 2.0) * width
        avg_values = [summary["phase"].get(phase, {}).get("avg_latency_ms", 0.0) for phase in phase_names]
        fail_values = [summary["phase"].get(phase, {}).get("failure_rate_pct", 0.0) for phase in phase_names]
        xpos = [value + offset for value in x]
        axes[0].bar(xpos, avg_values, width=width, label=summary["name"], alpha=0.85, edgecolor="black", linewidth=0.8)
        axes[1].bar(xpos, fail_values, width=width, label=summary["name"], alpha=0.85, edgecolor="black", linewidth=0.8)

    axes[0].set_title("Average latency by phase")
    axes[0].set_ylabel("ms")
    axes[0].legend(fontsize=8)

    axes[1].set_title("Failure rate by phase")
    axes[1].set_ylabel("%")
    axes[1].set_xticks(x, phase_names, rotation=25, ha="right")

    plt.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def _write_summary(summary_path: Path, summaries: list[dict]) -> None:
    with summary_path.open("w", encoding="utf-8") as handle:
        handle.write("# Simple Comparison Output\n\n")
        handle.write("Generated files:\n\n")
        handle.write("- `simple_compare_overall.png`\n")
        handle.write("- `simple_compare_phase.png`\n\n")
        handle.write("Runs:\n\n")
        for summary in summaries:
            handle.write(f"- `{summary['name']}`\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", required=True, metavar="DIR")
    parser.add_argument("--output-dir", metavar="DIR")
    args = parser.parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else None
    run([Path(run_dir) for run_dir in args.run_dir], out_dir)


if __name__ == "__main__":
    main()