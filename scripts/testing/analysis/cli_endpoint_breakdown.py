"""cli_endpoint_breakdown — per-endpoint latency and failure charts by phase.

Produces <run_dir>/analysis/endpoint_breakdown.png with two panels:
  - average latency by endpoint per phase (grouped bars)
  - failure count by endpoint per phase (stacked bars)

Endpoints: content_lookup (data-plane heavy), feed_ranking (compute-plane heavy),
service_pressure (mixed), hash_challenge (CPU-bound press, RQ2 v4).

Usage:
    python -m source.scripts.testing.analysis.cli_endpoint_breakdown --run-dir <dir>
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def run(run_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[cli_endpoint_breakdown] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .client_status import is_completed, is_failure
    from .loader import load_run

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    phase_order = [p.name for p in r.phases]
    if not phase_order:
        print("[cli_endpoint_breakdown] no phases found — skipping")
        return

    # ── Build per-phase per-endpoint data ───────────────────────────────
    endpoints_seen: set[str] = set()
    # phase -> endpoint -> {latencies_ms: [], failures: int, total: int}
    data: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"latencies_ms": [], "failures": 0, "total": 0}))

    for row in r.all_client_rows:
        phase = str(row.get("phase", "unknown"))
        endpoint = str(row.get("endpoint", "unknown"))
        endpoints_seen.add(endpoint)

        failed = is_failure(row)

        entry = data[phase][endpoint]
        entry["total"] += 1
        if is_completed(row):
            lat_s = _safe_float(row.get("latency_s"), 0.0)
            entry["latencies_ms"].append(lat_s * 1000.0)
        if failed:
            entry["failures"] += 1

    if not endpoints_seen:
        print("[cli_endpoint_breakdown] no endpoint data — skipping")
        return

    endpoints_sorted = sorted(endpoints_seen)
    phases_present = [p for p in phase_order if p in data]

    # ── Plot ────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(18, 7))
    fig.suptitle(f"Endpoint breakdown — {run_dir.name}", fontsize=13)

    endpoint_colors = {
        "content_lookup": "#1a7abf",
        "feed_ranking": "#bf5a1a",
        "service_pressure": "#1abf4a",
        "hash_challenge": "#7a1abf",
    }
    x = np.arange(len(phases_present))
    n_endpoints = len(endpoints_sorted)
    bar_width = 0.7 / n_endpoints

    # ── Panel 0: Average latency by endpoint per phase ──────────────────
    ax = axes[0]
    for i, ep in enumerate(endpoints_sorted):
        values = []
        for pname in phases_present:
            lats = data.get(pname, {}).get(ep, {}).get("latencies_ms", [])
            val = sum(lats) / len(lats) if lats else 0.0
            values.append(val)
        offset = (i - (n_endpoints - 1) / 2.0) * bar_width
        color = endpoint_colors.get(ep, "#888888")
        ax.bar(x + offset, values, bar_width, color=color, label=ep, alpha=0.85, edgecolor="black", linewidth=0.5)

    ax.set_ylabel("avg ms")
    ax.set_title("Average latency by endpoint per phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phases_present, rotation=45, ha="right", fontsize=8)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    # ── Panel 1: Failure count by endpoint per phase (stacked) ──────────
    ax = axes[1]
    bottom = np.zeros(len(phases_present))
    for ep in endpoints_sorted:
        values = [data.get(p, {}).get(ep, {}).get("failures", 0) for p in phases_present]
        color = endpoint_colors.get(ep, "#888888")
        ax.bar(x, values, bar_width * n_endpoints * 0.9, bottom=bottom, color=color, label=ep, alpha=0.85, edgecolor="black", linewidth=0.5)
        bottom = [b + v for b, v in zip(bottom, values)]

    ax.set_ylabel("failures")
    ax.set_title("Failure count by endpoint per phase")
    ax.set_xticks(x)
    ax.set_xticklabels(phases_present, rotation=45, ha="right", fontsize=8)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    out_path = out_dir / "endpoint_breakdown.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_endpoint_breakdown] wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
