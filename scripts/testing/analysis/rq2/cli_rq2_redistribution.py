"""cli_rq2_redistribution — measure load redistribution time per scale-up event.

Produces per-run CSVs for RQ2 Graphs 1–4:
  - rq2_redistribution_profile.csv  (Graph 1: share vs. time since spawn)
  - rq2_redistribution_summary.csv  (Graph 2: per-event redistribution times)
  - rq2_cumulative_load.csv         (Graph 4: cumulative load over time)
  - rq2_transition_quality.csv      (Graph 3: per-mode p95 latency/failure rate)

Graph 5 (coordination-gap penalty) is derived from Graph 2 data by a
separate plotting script — it is the difference between topology_slowstart
and topology_lifecycle mean redistribution times.

Usage:
    python -m source.scripts.testing.analysis.rq2.cli_rq2_redistribution <run_dir> [<run_dir> ...]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _container_role(name: str | None) -> str | None:
    if not name:
        return None
    if name.startswith("edge_server"):
        return "compute"
    if name.startswith("edge_storage"):
        return "storage"
    return None


def _extract_lan(container: str) -> str:
    """Extract LAN id ('1' or '2') from container name like 'edge_server_lan1_dyn18'."""
    if "_lan1_" in container or container.endswith("_lan1"):
        return "1"
    if "_lan2_" in container or container.endswith("_lan2"):
        return "2"
    return "1"  # fallback


def _detect_mode(run_dir: Path) -> str:
    """Read BACKEND_SELECTION_POLICY from controller_env_snapshot.env."""
    env_file = run_dir / "controller_env_snapshot.env"
    if not env_file.exists():
        return "topology_lifecycle"  # default
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BACKEND_SELECTION_POLICY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "topology_lifecycle"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    rank = max(0.0, min(1.0, fraction)) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    if lower == upper:
        return ordered[lower]
    frac = rank - lower
    return ordered[lower] * (1.0 - frac) + ordered[upper] * frac


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyse_run(run_dir: Path) -> dict:
    """Analyse a single run folder and return result dicts keyed by role.

    Returns:
        mode: detected policy mode string
        by_role: dict[role] -> {
            profile_rows: list[dict],
            summary_rows: list[dict],
            cumulative_rows: list[dict],
        }
        transition_quality: dict with p95_latency_ms and failure_rate_pct
    """
    from ..loader import load_run

    r = load_run(run_dir)
    mode = _detect_mode(run_dir)
    out_dir = Path(run_dir) / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Filter spawn_done events ──────────────────────────────────────
    spawns: list[dict] = []  # {ts, lan, tier, container}
    for ev in r.events:
        if ev.kind != "spawn_done":
            continue
        role = _container_role(ev.container)
        if role not in ("compute", "storage"):
            continue
        lan = ev.lan or _extract_lan(ev.container or "")
        spawns.append({
            "ts": ev.ts,
            "lan": lan,
            "tier": role,
            "container": ev.container or "",
        })

    if not spawns:
        print(f"[cli_rq2_redistribution] {run_dir.name}: no spawn_done events — skipping")
        return {"mode": mode, "by_role": {}, "transition_quality": {}}

    # ── ±45s isolation filter (per-role, per-LAN) ─────────────────────
    valid_spawns: list[dict] = []
    for sp in spawns:
        conflicts = [
            s for s in spawns
            if s["lan"] == sp["lan"]
            and s["tier"] == sp["tier"]
            and s["ts"] != sp["ts"]
            and abs(s["ts"] - sp["ts"]) <= 45.0
        ]
        if not conflicts:
            valid_spawns.append(sp)

    if not valid_spawns:
        print(f"[cli_rq2_redistribution] {run_dir.name}: no isolated spawns — skipping")
        return {"mode": mode, "by_role": {}, "transition_quality": {}}

    # ── Build MAC lookup from per_node_stats ──────────────────────────
    node_rows: list[dict] = []
    for row in r.node_rows:
        node_rows.append({
            "timestamp": _safe_float(row.get("timestamp", ""), 0.0),
            "phase": row.get("phase", ""),
            "network_id": row.get("network_id", ""),
            "window_end": _safe_float(row.get("window_end", ""), 0.0),
            "server_id": row.get("server_id", ""),
            "role": row.get("role", ""),
            "request_count": _safe_float(row.get("request_count", ""), 0.0),
            "avg_connections": _safe_float(row.get("avg_connections", ""), 0.0),
            "member_state": row.get("member_state", ""),
        })

    # ── Per-role analysis ─────────────────────────────────────────────
    by_role: dict[str, dict] = {}

    for role in ("compute", "storage"):
        role_spawns = [s for s in valid_spawns if s["tier"] == role]
        if not role_spawns:
            continue

        profile_events: list[list[tuple[float, float]]] = []
        summary_events: list[dict] = []
        cumulative_events: list[list[tuple[float, float]]] = []

        for sp in role_spawns:
            spawn_ts = sp["ts"]
            lan = sp["lan"]

            # ── MAC resolution: role-aware temporal proximity ─────────
            before_macs: set[str] = set()
            after_macs: set[str] = set()
            for nr in node_rows:
                if nr["role"] != role:
                    continue
                if nr["window_end"] < spawn_ts:
                    before_macs.add(nr["server_id"])
                elif spawn_ts <= nr["window_end"] <= spawn_ts + 15:
                    after_macs.add(nr["server_id"])

            new_macs = after_macs - before_macs
            if len(new_macs) != 1:
                continue  # ambiguous
            mac = new_macs.pop()

            # ── Collect rows for this MAC ──────────────────────────────
            mac_rows = [
                nr for nr in node_rows
                if nr["server_id"] == mac and nr["window_end"] >= spawn_ts
            ]
            mac_rows.sort(key=lambda nr: nr["window_end"])
            if not mac_rows:
                continue

            # ── Metric selection ───────────────────────────────────────
            metric_col = "request_count" if role == "compute" else "avg_connections"

            # ── Share over time ────────────────────────────────────────
            event_profile: list[tuple[float, float]] = []
            event_cumulative: list[tuple[float, float]] = []
            cum = 0.0

            for nr in mac_rows:
                win_end = nr["window_end"]

                peers = [
                    n for n in node_rows
                    if n["role"] == role
                    and abs(n["window_end"] - win_end) < 0.5
                ]
                if role == "storage":
                    peers = [p for p in peers if p["member_state"] == "SECONDARY"]

                total = sum(p[metric_col] for p in peers)
                if total <= 0:
                    share = 0.0
                else:
                    share = nr[metric_col] / total

                time_since_spawn = max(0.0, nr["timestamp"] - spawn_ts)
                event_profile.append((time_since_spawn, share))

                cum += nr[metric_col]
                event_cumulative.append((time_since_spawn, cum))

            profile_events.append(event_profile)
            cumulative_events.append(event_cumulative)

            # ── Equilibrium detection ──────────────────────────────────
            equilibrium_ts: float | None = None
            for i in range(len(event_profile) - 2):
                win_ts = event_profile[i][0] + spawn_ts
                peers_at_w = [
                    n for n in node_rows
                    if n["role"] == role
                    and abs(n["window_end"] - win_ts) < 0.5
                ]
                if role == "storage":
                    peers_at_w = [p for p in peers_at_w if p["member_state"] == "SECONDARY"]
                n_peers = len(set(p["server_id"] for p in peers_at_w))
                if n_peers == 0:
                    continue
                expected_share = 1.0 / n_peers

                shares = [event_profile[j][1] for j in range(i, i + 3)]
                if all(abs(s - expected_share) <= 0.10 for s in shares):
                    equilibrium_ts = event_profile[i][0] + spawn_ts
                    break

            redistribution_s = (equilibrium_ts - spawn_ts) if equilibrium_ts else None

            summary_events.append({
                "mode": mode,
                "role": role,
                "container": sp["container"],
                "lan": lan,
                "spawn_ts": f"{spawn_ts:.3f}",
                "equilibrium_ts": f"{equilibrium_ts:.3f}" if equilibrium_ts else "",
                "redistribution_s": f"{redistribution_s:.1f}" if redistribution_s else "",
            })

        if not summary_events:
            continue

        # ── Aggregate profile across events (1s bins) ──────────────────
        max_t = 60
        bins = {t: [] for t in range(max_t + 1)}
        for ep in profile_events:
            for t_s, share in ep:
                bin_idx = int(t_s)
                if 0 <= bin_idx <= max_t:
                    bins[bin_idx].append(share)

        profile_rows = []
        cumulative_rows = []
        for t in range(max_t + 1):
            vals = bins[t]
            if vals:
                mean_share = sum(vals) / len(vals)
                std_share = (
                    (sum((v - mean_share) ** 2 for v in vals) / len(vals)) ** 0.5
                    if len(vals) > 1 else 0.0
                )
                profile_rows.append({
                    "mode": mode,
                    "role": role,
                    "time_since_spawn_s": t,
                    "mean_share": f"{mean_share:.4f}",
                    "std_share": f"{std_share:.4f}",
                    "n_events": len(vals),
                })

            cum_vals = []
            for ce in cumulative_events:
                for ct_s, cval in ce:
                    if int(ct_s) == t:
                        cum_vals.append(cval)
            if cum_vals:
                mean_cum = sum(cum_vals) / len(cum_vals)
                cumulative_rows.append({
                    "mode": mode,
                    "role": role,
                    "time_since_spawn_s": t,
                    "mean_cumulative_load": f"{mean_cum:.2f}",
                    "n_events": len(cum_vals),
                })

        # ── Per-mode summary aggregates ────────────────────────────────
        valid_times = [float(s["redistribution_s"]) for s in summary_events if s["redistribution_s"]]
        by_role[role] = {
            "profile_rows": profile_rows,
            "cumulative_rows": cumulative_rows,
            "summary_rows": summary_events,
            "aggregates": {
                "mode": mode,
                "role": role,
                "n_events": len(valid_times),
                "mean_s": f"{sum(valid_times)/len(valid_times):.1f}" if valid_times else "",
                "median_s": f"{_percentile(valid_times, 0.5):.1f}" if valid_times else "",
                "p95_s": f"{_percentile(valid_times, 0.95):.1f}" if valid_times else "",
                "min_s": f"{min(valid_times):.1f}" if valid_times else "",
                "max_s": f"{max(valid_times):.1f}" if valid_times else "",
            },
        }

    # ── Transition-window service quality (Graph 3) ───────────────────
    transition_quality = {}
    if r.all_client_rows:
        spike_rows = [row for row in r.all_client_rows if row.get("phase") == "compute_spike"]
        if spike_rows:
            latencies = [
                _safe_float(row.get("latency_ms", ""), 0.0)
                for row in spike_rows
                if _safe_float(row.get("latency_ms", ""), 0.0) > 0
            ]
            failures = [
                row for row in spike_rows
                if str(row.get("status", "")).lower() in ("failed", "error", "1", "true")
                or _safe_float(row.get("failure", ""), 0.0) > 0
            ]
            p95 = _percentile(latencies, 0.95) if latencies else 0.0
            fail_rate = (len(failures) / len(spike_rows) * 100) if spike_rows else 0.0
            transition_quality = {
                "mode": mode,
                "p95_latency_ms": f"{p95:.1f}",
                "failure_rate_pct": f"{fail_rate:.2f}",
                "n_requests": len(spike_rows),
            }

    return {
        "mode": mode,
        "by_role": by_role,
        "transition_quality": transition_quality,
    }


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(run_dir: Path, result: dict) -> None:
    out_dir = Path(run_dir) / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_profile: list[dict] = []
    all_summary: list[dict] = []
    all_cumulative: list[dict] = []
    all_aggregates: list[dict] = []

    for _, role_data in result.get("by_role", {}).items():
        all_profile.extend(role_data.get("profile_rows", []))
        all_summary.extend(role_data.get("summary_rows", []))
        all_cumulative.extend(role_data.get("cumulative_rows", []))
        agg = role_data.get("aggregates", {})
        if agg:
            all_aggregates.append(agg)

    if all_profile:
        _write_csv(
            out_dir / "rq2_redistribution_profile.csv",
            all_profile,
            ["mode", "role", "time_since_spawn_s", "mean_share", "std_share", "n_events"],
        )
    if all_summary:
        _write_csv(
            out_dir / "rq2_redistribution_summary.csv",
            all_summary,
            ["mode", "role", "container", "lan", "spawn_ts", "equilibrium_ts", "redistribution_s"],
        )
    if all_aggregates:
        _write_csv(
            out_dir / "rq2_redistribution_aggregates.csv",
            all_aggregates,
            ["mode", "role", "n_events", "mean_s", "median_s", "p95_s", "min_s", "max_s"],
        )
    if all_cumulative:
        _write_csv(
            out_dir / "rq2_cumulative_load.csv",
            all_cumulative,
            ["mode", "role", "time_since_spawn_s", "mean_cumulative_load", "n_events"],
        )
    tq = result.get("transition_quality", {})
    if tq:
        _write_csv(
            out_dir / "rq2_transition_quality.csv",
            [tq],
            ["mode", "p95_latency_ms", "failure_rate_pct", "n_requests"],
        )

    print(f"[cli_rq2_redistribution] {run_dir.name}: mode={result['mode']} "
          f"roles={list(result.get('by_role', {}).keys())} "
          f"events={sum(len(d.get('summary_rows', [])) for d in result.get('by_role', {}).values())}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RQ2 redistribution analysis — measure load redistribution time"
    )
    parser.add_argument(
        "run_dirs", nargs="+", type=Path,
        help="One or more run directories containing per_node_stats.csv, "
             "client_requests.csv, controller logs, and controller_env_snapshot.env",
    )
    args = parser.parse_args()

    for run_dir in args.run_dirs:
        if not run_dir.is_dir():
            print(f"[cli_rq2_redistribution] skipping: not a directory — {run_dir}")
            continue
        try:
            result = analyse_run(run_dir)
            write_outputs(run_dir, result)
        except Exception as exc:
            print(f"[cli_rq2_redistribution] ERROR in {run_dir.name}: {exc}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
