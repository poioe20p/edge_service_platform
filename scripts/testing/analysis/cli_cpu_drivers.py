"""cli_cpu_drivers — per-node CPU load-balance diagnostic.

Computes a table of median CPU for the *oldest* node vs *newer* nodes per
phase and role. If newer nodes sit near 0 while the oldest is saturated the
symptom is a routing / load-balancing failure rather than an undersized tier.

Usage:
    python -m source.scripts.testing.analysis.cli_cpu_drivers --run-dir <dir>
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _median(values) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return float("nan")
    mid = len(vals) // 2
    if len(vals) % 2 == 0:
        return (vals[mid - 1] + vals[mid]) / 2.0
    return vals[mid]


def load_balance_table(run) -> list[dict]:
    """Return old-vs-new CPU median per phase and role."""
    if not run.node_rows:
        warnings.warn("per_node_stats.csv missing — load_balance_table is empty.")
        return []

    out: list[dict] = []
    for phase in run.phases:
        for role in ("compute", "storage"):
            rows = [r for r in run.node_rows
                    if r.get("phase") == phase.name
                    and r.get("role") == role
                    and r.get("cpu_percent") not in ("", None)]
            if not rows:
                continue
            first_seen: dict[str, float] = {}
            for r in rows:
                sid = r["server_id"]
                we = _safe_float(r["window_end"])
                first_seen[sid] = min(first_seen.get(sid, we), we)

            oldest = min(first_seen.values())
            old_nodes = {sid for sid, t in first_seen.items() if t == oldest}

            old_cpu = _median(
                _safe_float(r["cpu_percent"])
                for r in rows if r["server_id"] in old_nodes
            )
            new_cpu = _median(
                _safe_float(r["cpu_percent"])
                for r in rows if r["server_id"] not in old_nodes
            )
            out.append({
                "phase": phase.name,
                "role": role,
                "old_cpu_median": old_cpu,
                "new_cpu_median": new_cpu,
                "nodes": len(first_seen),
            })
    return out


def run(run_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[cli_cpu_drivers] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .phase_window import phase_boundaries
    from .plots import shade_phases, overlay_events

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    table = load_balance_table(r)

    # Print table to stdout
    print(f"\n{'Phase':<25} {'Role':<10} {'Nodes':>6} {'Old CPU':>10} {'New CPU':>10}")
    print("-" * 65)
    for row in table:
        print(f"{row['phase']:<25} {row['role']:<10} {row['nodes']:>6} "
              f"{row['old_cpu_median']:>9.1f}% {row['new_cpu_median']:>9.1f}%")

    if not table:
        _append_summary(out_dir / "summary.md",
                        "CPU Drivers: per_node_stats.csv missing — skipped.\n")
        return

    # Plot: grouped bar chart (old vs new CPU per phase per role)
    phases = [row["phase"] for row in table if row["role"] == "compute"]
    if not phases:
        phases = list({row["phase"] for row in table})

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax_idx, role in enumerate(("compute", "storage")):
        ax = axes[ax_idx]
        role_rows = [row for row in table if row["role"] == role]
        if not role_rows:
            ax.set_visible(False)
            continue
        ph_names = [row["phase"] for row in role_rows]
        old_vals = [row["old_cpu_median"] for row in role_rows]
        new_vals = [row["new_cpu_median"] for row in role_rows]
        xs = list(range(len(ph_names)))
        width = 0.35
        ax.bar([x - width / 2 for x in xs], old_vals, width, label="oldest node", color="#1a7abf", edgecolor="black", linewidth=0.8)
        ax.bar([x + width / 2 for x in xs], new_vals, width, label="new nodes", color="#bf5a1a", alpha=0.8, edgecolor="black", linewidth=0.8)
        ax.set_xticks(xs)
        ax.set_xticklabels(ph_names, rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("CPU % (median)")
        ax.set_title(f"{role.capitalize()} — old vs new node CPU")
        ax.legend(fontsize=8)

    plt.tight_layout()
    out_path = out_dir / "cpu_drivers.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_cpu_drivers] wrote {out_path}")

    _append_summary(out_dir / "summary.md",
                    f"See `analysis/cpu_drivers.png` for old-vs-new node load balance.\n")


def _append_summary(summary_path: Path, text: str) -> None:
    with summary_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## CPU Drivers\n\n{text}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
