"""cli_mechanism_compare — mechanism-necessity cross-run comparison.

Produces a single multi-panel PNG in the chosen output directory:
  - mechanism_compare.png

Panels (2 columns × 4 rows):
  Row 1: Avg latency by phase   | Failure rate by phase
  Row 2: Avg compute CPU%       | Avg storage CPU%
  Row 3: Avg compute RAM (MB)   | Avg storage RAM (MB)
  Row 4: Owner-LAN avg_time_db  | Consumer-LAN avg_time_db  (hotspot phases only)

Each panel is a grouped bar chart with one group per phase and one bar per run.
CPU and RAM values are computed from per_node_stats.csv (per-container data),
aggregated by role (compute = edge_server_*, storage = edge_storage_*) and
averaged across all containers of that role active in each phase.

avg_time_db_ms values are extracted from resource_stats.csv (domain_rows),
split by network_id (lan1/lan2). For hotspot phases, the owner LAN is the
target of hotspot_direction (e.g. lan2_to_lan1 → owner=lan1). For non-hotspot
phases, both LANs are averaged together.

Usage:
    python -m source.scripts.testing.analysis.cli_mechanism_compare \
        --run-dir <dir1> --run-dir <dir2> --run-dir <dir3> --run-dir <dir4> \
        [--output-dir <dir>]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _summarize_node_rows_by_phase_role(
    node_rows: list[dict],
    phase_names: list[str],
) -> dict[str, dict[str, dict[str, float]]]:
    """Aggregate per-node CPU and RAM by phase and role.

    Returns:
        {phase_name: {role: {"cpu": avg_cpu_pct, "ram": avg_ram_mb}}}
    """
    buckets: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in node_rows:
        phase = str(row.get("phase", ""))
        role = str(row.get("role", ""))
        if not phase or not role:
            continue
        buckets[phase][role].append(row)

    result: dict[str, dict[str, dict[str, float]]] = {}
    for phase in phase_names:
        result[phase] = {}
        for role in ("compute", "storage"):
            rows = buckets.get(phase, {}).get(role, [])
            cpu_vals = [_safe_float(r.get("cpu_percent")) for r in rows
                        if r.get("cpu_percent") not in ("", None)]
            ram_vals = [_safe_float(r.get("ram_used_mb")) for r in rows
                        if r.get("ram_used_mb") not in ("", None)]
            result[phase][role] = {
                "cpu": sum(cpu_vals) / len(cpu_vals) if cpu_vals else 0.0,
                "ram": sum(ram_vals) / len(ram_vals) if ram_vals else 0.0,
            }
    return result


def _load_hotspot_directions(run_dirs: list[Path]) -> dict[str, str | None]:
    """Read phases_snapshot.json from the first run to get hotspot_direction per phase.

    Returns {phase_name: hotspot_direction or None}.
    """
    snapshot_path = run_dirs[0] / "phases_snapshot.json"
    if not snapshot_path.exists():
        return {}
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        phases = data.get("phases", [])
    else:
        phases = data
    return {
        p.get("name", ""): p.get("hotspot_direction")
        for p in phases
    }


def _summarize_domain_time_db(
    domain_rows: list[dict],
    phase_names: list[str],
    hotspot_directions: dict[str, str | None],
) -> dict[str, dict[str, float]]:
    """Aggregate avg_time_db_ms by phase, split by owner vs consumer LAN.

    Returns {phase_name: {"owner": avg_ms, "consumer": avg_ms}}.
    For phases without a hotspot direction, both are averaged across both LANs.
    """
    # Collect per-phase per-LAN time_db values
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in domain_rows:
        phase = str(row.get("phase", ""))
        lan = str(row.get("network_id", ""))  # "lan1" or "lan2"
        val = row.get("avg_time_db_ms", "")
        if not phase or not lan or val in ("", None):
            continue
        buckets[phase][lan].append(_safe_float(val))

    result: dict[str, dict[str, float]] = {}
    for phase in phase_names:
        lan_data = buckets.get(phase, {})
        hotspot = hotspot_directions.get(phase)
        if hotspot == "lan2_to_lan1":
            owner_lan = "lan1"
            consumer_lan = "lan2"
        elif hotspot == "lan1_to_lan2":
            owner_lan = "lan2"
            consumer_lan = "lan1"
        else:
            # No hotspot — average both LANs for both owner and consumer
            all_vals = []
            for vals in lan_data.values():
                all_vals.extend(vals)
            avg = sum(all_vals) / len(all_vals) if all_vals else 0.0
            result[phase] = {"owner": avg, "consumer": avg}
            continue

        owner_vals = lan_data.get(owner_lan, [])
        consumer_vals = lan_data.get(consumer_lan, [])
        result[phase] = {
            "owner": sum(owner_vals) / len(owner_vals) if owner_vals else 0.0,
            "consumer": sum(consumer_vals) / len(consumer_vals) if consumer_vals else 0.0,
        }
    return result


def run(run_dirs: list[Path], output_dir: Path | None = None) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[cli_mechanism_compare] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .simple_metrics import summarize_client_rows_by_phase

    runs = [load_run(Path(run_dir)) for run_dir in run_dirs]
    if not runs:
        print("[cli_mechanism_compare] no run directories provided")
        return

    out_dir = Path(output_dir) if output_dir is not None else Path.cwd() / "mechanism_compare"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase name ordering (union across all runs, in first-run order) ──
    phase_names: list[str] = []
    seen: set[str] = set()
    for run_data in runs:
        for phase in run_data.phases:
            if phase.name not in seen:
                phase_names.append(phase.name)
                seen.add(phase.name)

    # ── Per-phase client-request summaries (latency, failure) ────────────
    phase_summaries: list[dict] = []
    for run_data in runs:
        ph = {
            row["phase"]: row
            for row in summarize_client_rows_by_phase(run_data.all_client_rows, phase_names)
        }
        phase_summaries.append(ph)

    # ── Per-phase per-role CPU/RAM summaries ─────────────────────────────
    node_summaries: list[dict] = []
    for run_data in runs:
        node_summaries.append(
            _summarize_node_rows_by_phase_role(run_data.node_rows, phase_names)
        )

    run_names = [r.run_dir.name for r in runs]
    run_count = len(runs)

    # ── Hotspot directions (from first run's phases_snapshot.json) ──────
    hotspot_directions = _load_hotspot_directions(run_dirs)

    # ── Per-phase time_db summaries from resource_stats.csv ─────────────
    time_db_summaries: list[dict] = []
    for run_data in runs:
        time_db_summaries.append(
            _summarize_domain_time_db(run_data.domain_rows, phase_names, hotspot_directions)
        )

    # ── Plot ────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(4, 2, figsize=(22, 24))
    fig.suptitle("Mechanism Necessity — cross-run comparison", fontsize=14, fontweight="bold")

    x = np.arange(len(phase_names))
    bar_width = max(0.08, 0.7 / max(run_count, 1))

    # Colour palette — consistent across panels
    palette = ["#1a7abf", "#bf5a1a", "#1abf4a", "#8b1a8b"][:run_count]

    # ── Row 1, Col 0: Avg latency by phase ──────────────────────────────
    ax = axes[0][0]
    for i, (ph, name, color) in enumerate(zip(phase_summaries, run_names, palette)):
        offset = (i - (run_count - 1) / 2.0) * bar_width
        values = [ph.get(p, {}).get("avg_latency_ms", 0.0) for p in phase_names]
        ax.bar(x + offset, values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("ms")
    ax.set_title("Average latency by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phase_names, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)

    # ── Row 1, Col 1: Failure rate by phase ─────────────────────────────
    ax = axes[0][1]
    for i, (ph, name, color) in enumerate(zip(phase_summaries, run_names, palette)):
        offset = (i - (run_count - 1) / 2.0) * bar_width
        values = [ph.get(p, {}).get("failure_rate_pct", 0.0) for p in phase_names]
        ax.bar(x + offset, values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("%")
    ax.set_title("Failure rate by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phase_names, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)

    # ── Row 2, Col 0: Avg compute CPU% by phase ─────────────────────────
    ax = axes[1][0]
    for i, (ns, name, color) in enumerate(zip(node_summaries, run_names, palette)):
        offset = (i - (run_count - 1) / 2.0) * bar_width
        values = [ns.get(p, {}).get("compute", {}).get("cpu", 0.0) for p in phase_names]
        ax.bar(x + offset, values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("CPU %")
    ax.set_title("Average compute CPU% by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phase_names, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)

    # ── Row 2, Col 1: Avg storage CPU% by phase ─────────────────────────
    ax = axes[1][1]
    for i, (ns, name, color) in enumerate(zip(node_summaries, run_names, palette)):
        offset = (i - (run_count - 1) / 2.0) * bar_width
        values = [ns.get(p, {}).get("storage", {}).get("cpu", 0.0) for p in phase_names]
        ax.bar(x + offset, values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("CPU %")
    ax.set_title("Average storage CPU% by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phase_names, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)

    # ── Row 3, Col 0: Avg compute RAM (MB) by phase ─────────────────────
    ax = axes[2][0]
    for i, (ns, name, color) in enumerate(zip(node_summaries, run_names, palette)):
        offset = (i - (run_count - 1) / 2.0) * bar_width
        values = [ns.get(p, {}).get("compute", {}).get("ram", 0.0) for p in phase_names]
        ax.bar(x + offset, values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("MB")
    ax.set_title("Average compute RAM (MB) by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phase_names, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)

    # ── Row 3, Col 1: Avg storage RAM (MB) by phase ─────────────────────
    ax = axes[2][1]
    for i, (ns, name, color) in enumerate(zip(node_summaries, run_names, palette)):
        offset = (i - (run_count - 1) / 2.0) * bar_width
        values = [ns.get(p, {}).get("storage", {}).get("ram", 0.0) for p in phase_names]
        ax.bar(x + offset, values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("MB")
    ax.set_title("Average storage RAM (MB) by phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phase_names, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)

    # ── Row 4, Col 0: Owner-LAN avg_time_db_ms (hotspot phases only) ───
    ax = axes[3][0]
    hotspot_phases = [p for p in phase_names if hotspot_directions.get(p)]
    if hotspot_phases:
        hx = list(range(len(hotspot_phases)))
        for i, (tdb, name, color) in enumerate(zip(time_db_summaries, run_names, palette)):
            offset = (i - (run_count - 1) / 2.0) * bar_width
            values = [tdb.get(p, {}).get("owner", 0.0) for p in hotspot_phases]
            ax.bar([x + offset for x in hx], values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
        ax.set_ylabel("ms")
        ax.set_title("Owner-LAN avg_time_db_ms (hotspot phases)")
        ax.set_xticks(hx)
        ax.set_xticklabels(hotspot_phases, rotation=30, ha="right", fontsize=8)
        ax.legend(fontsize=7)
        ax.grid(axis="y", alpha=0.3)

    # ── Row 4, Col 1: Consumer-LAN avg_time_db_ms (hotspot phases only) ─
    ax = axes[3][1]
    if hotspot_phases:
        for i, (tdb, name, color) in enumerate(zip(time_db_summaries, run_names, palette)):
            offset = (i - (run_count - 1) / 2.0) * bar_width
            values = [tdb.get(p, {}).get("consumer", 0.0) for p in hotspot_phases]
            ax.bar([x + offset for x in hx], values, bar_width, label=name, color=color, alpha=0.85, edgecolor="black", linewidth=0.5)
        ax.set_ylabel("ms")
        ax.set_title("Consumer-LAN avg_time_db_ms (hotspot phases)")
        ax.set_xticks(hx)
        ax.set_xticklabels(hotspot_phases, rotation=30, ha="right", fontsize=8)
        ax.legend(fontsize=7)
        ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    out_path = out_dir / "mechanism_compare.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_mechanism_compare] wrote {out_path}")

    # ── Text summary ────────────────────────────────────────────────────
    _write_summary(out_dir / "mechanism_compare.md", run_names, phase_names,
                   phase_summaries, node_summaries, time_db_summaries, hotspot_directions)


def _write_summary(
    summary_path: Path,
    run_names: list[str],
    phase_names: list[str],
    phase_summaries: list[dict],
    node_summaries: list[dict],
    time_db_summaries: list[dict] | None = None,
    hotspot_directions: dict | None = None,
) -> None:
    with summary_path.open("w", encoding="utf-8") as f:
        f.write("# Mechanism Necessity — Cross-Run Comparison\n\n")
        f.write("See `mechanism_compare.png` for the full chart.\n\n")

        f.write("## Per-Phase Latency & Failure\n\n")
        f.write("| Phase | " + " | ".join(f"{name} lat" for name in run_names))
        f.write(" | " + " | ".join(f"{name} fail%" for name in run_names) + " |\n")
        f.write("|-------|" + "|".join(["---" for _ in range(len(run_names) * 2)]) + "|\n")
        for p in phase_names:
            lat_vals = [f"{ps.get(p, {}).get('avg_latency_ms', 0):.0f} ms" for ps in phase_summaries]
            fail_vals = [f"{ps.get(p, {}).get('failure_rate_pct', 0):.1f}%" for ps in phase_summaries]
            f.write(f"| {p} | " + " | ".join(lat_vals) + " | " + " | ".join(fail_vals) + " |\n")

        f.write("\n## Per-Phase Compute CPU% & RAM\n\n")
        f.write("| Phase | " + " | ".join(f"{name} CPU%" for name in run_names))
        f.write(" | " + " | ".join(f"{name} RAM" for name in run_names) + " |\n")
        f.write("|-------|" + "|".join(["---" for _ in range(len(run_names) * 2)]) + "|\n")
        for p in phase_names:
            cpu_vals = [f"{ns.get(p, {}).get('compute', {}).get('cpu', 0):.1f}%" for ns in node_summaries]
            ram_vals = [f"{ns.get(p, {}).get('compute', {}).get('ram', 0):.0f} MB" for ns in node_summaries]
            f.write(f"| {p} | " + " | ".join(cpu_vals) + " | " + " | ".join(ram_vals) + " |\n")

        f.write("\n## Per-Phase Storage CPU% & RAM\n\n")
        f.write("| Phase | " + " | ".join(f"{name} CPU%" for name in run_names))
        f.write(" | " + " | ".join(f"{name} RAM" for name in run_names) + " |\n")
        f.write("|-------|" + "|".join(["---" for _ in range(len(run_names) * 2)]) + "|\n")
        for p in phase_names:
            cpu_vals = [f"{ns.get(p, {}).get('storage', {}).get('cpu', 0):.1f}%" for ns in node_summaries]
            ram_vals = [f"{ns.get(p, {}).get('storage', {}).get('ram', 0):.0f} MB" for ns in node_summaries]
            f.write(f"| {p} | " + " | ".join(cpu_vals) + " | " + " | ".join(ram_vals) + " |\n")

        if time_db_summaries:
            hotspot_phases = [p for p in phase_names if (hotspot_directions or {}).get(p)]
            if hotspot_phases:
                f.write("\n## Per-Phase Owner-LAN & Consumer-LAN avg_time_db_ms (hotspot phases only)\n\n")
                f.write("| Phase | " + " | ".join(f"{name} owner" for name in run_names))
                f.write(" | " + " | ".join(f"{name} consumer" for name in run_names) + " |\n")
                f.write("|-------|" + "|".join(["---" for _ in range(len(run_names) * 2)]) + "|\n")
                for p in hotspot_phases:
                    owner_vals = [f"{tdb.get(p, {}).get('owner', 0):.0f} ms" for tdb in time_db_summaries]
                    consumer_vals = [f"{tdb.get(p, {}).get('consumer', 0):.0f} ms" for tdb in time_db_summaries]
                    f.write(f"| {p} | " + " | ".join(owner_vals) + " | " + " | ".join(consumer_vals) + " |\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", required=True, metavar="DIR")
    parser.add_argument("--output-dir", metavar="DIR")
    args = parser.parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else None
    run([Path(run_dir) for run_dir in args.run_dir], out_dir)


if __name__ == "__main__":
    main()
