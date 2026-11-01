"""cli_lifecycle_gantt — container lifecycle Gantt chart for a run directory.

Produces <run_dir>/analysis/lifecycle_gantt.png showing:
  - Horizontal bars per dynamic container from first-seen to removed
  - Colour-coded by role: compute (blue), storage (orange), selective (green)
  - Phase background bands
  - Elasticity event markers (spawn, armed, cooldown) when controller logs are present

Only dynamic containers (edge_server_*_dyn*, edge_storage_*_dyn*, sel_sync_*_dyn*)
are shown. Static infrastructure containers are excluded.

Usage:
    python -m source.scripts.testing.analysis.cli_lifecycle_gantt --run-dir <dir>
"""
from __future__ import annotations

import argparse
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
    except ImportError:
        print("[cli_lifecycle_gantt] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .phase_window import phase_boundaries
    from .plots import shade_phases, overlay_events
    from .simple_metrics import container_role, parse_iso_ts, infer_origin_ts, infer_end_s

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    origin_ts = infer_origin_ts(r)
    bounds = phase_boundaries(origin_ts, r.phases)
    end_s = infer_end_s(r, origin_ts)

    # ── Collect dynamic container lifecycles ────────────────────────────
    # Build: container_name -> {first_seen, last_seen, role, events: [(t, event)]}
    lifecycles: dict[str, dict] = {}

    ordered = sorted(
        r.container_event_rows,
        key=lambda row: (
            parse_iso_ts(row.get("timestamp_iso")),
            _safe_float(row.get("monotonic_s"), 0.0),
        ),
    )

    for row in ordered:
        name = str(row.get("container", ""))
        role = container_role(name)
        if role is None:
            continue
        # Only dynamic containers
        if "_dyn" not in name:
            continue

        ts = parse_iso_ts(row.get("timestamp_iso"))
        mono = _safe_float(row.get("monotonic_s"), 0.0)
        t = (ts - origin_ts) if ts > 0 else mono
        if t < 0:
            t = 0.0

        event = str(row.get("event", "")).lower()
        state = str(row.get("state", "")).lower()

        if name not in lifecycles:
            lifecycles[name] = {
                "role": role,
                "first_seen": t,
                "last_seen": t,
                "events": [],
            }

        lc = lifecycles[name]
        lc["first_seen"] = min(lc["first_seen"], t)
        if event == "removed" or state != "running":
            lc["last_seen"] = t
            lc["events"].append((t, "removed"))
        else:
            lc["last_seen"] = max(lc["last_seen"], t)
            lc["events"].append((t, event))

    if not lifecycles:
        print("[cli_lifecycle_gantt] no dynamic containers found — skipping")
        return

    # Sort by role then first_seen
    sorted_containers = sorted(
        lifecycles.items(),
        key=lambda item: (
            {"compute": 0, "storage": 1, "selective": 2}.get(item[1]["role"], 9),
            item[1]["first_seen"],
        ),
    )

    # ── Plot ────────────────────────────────────────────────────────────
    role_colors = {"compute": "#1a7abf", "storage": "#bf5a1a", "selective": "#1abf4a"}
    edge_color = {"compute": "#0d5c8a", "storage": "#8a3e13", "selective": "#0d8a35"}

    n_containers = len(sorted_containers)
    fig_height = max(8, n_containers * 0.35 + 2)
    fig, ax = plt.subplots(1, 1, figsize=(16, fig_height))
    fig.suptitle(f"Container lifecycle — {run_dir.name}", fontsize=12)

    y_labels: list[str] = []
    for i, (cname, lc) in enumerate(sorted_containers):
        y = i + 1
        y_labels.append(cname)
        color = role_colors.get(lc["role"], "#888888")
        ec = edge_color.get(lc["role"], "#555555")

        start = lc["first_seen"]
        end = lc["last_seen"]
        if end <= start:
            end = start + 1  # minimum visible width

        ax.barh(y, end - start, left=start, height=0.7, color=color, edgecolor=ec, linewidth=0.5, alpha=0.85)

        # Mark "removed" events with an X marker if the container was later re-added
        # (Simple approach: just a thin bar, events as scatter would be too noisy)

    ax.set_yticks(range(1, n_containers + 1))
    ax.set_yticklabels(y_labels, fontsize=7)
    ax.set_xlabel("time (s)")
    ax.set_title(f"Dynamic container lifecycles ({n_containers} containers)")
    shade_phases(ax, bounds, origin_ts)

    # Add role legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor="#1a7abf", edgecolor="#0d5c8a", label="compute"),
        Patch(facecolor="#bf5a1a", edgecolor="#8a3e13", label="storage"),
        Patch(facecolor="#1abf4a", edgecolor="#0d8a35", label="selective"),
    ]
    ax.legend(handles=legend_elements, fontsize=8, loc="upper right")

    if end_s > 0:
        ax.set_xlim(0, end_s)

    plt.tight_layout()
    out_path = out_dir / "lifecycle_gantt.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_lifecycle_gantt] wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
