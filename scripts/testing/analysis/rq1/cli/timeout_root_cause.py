"""timeout_root_cause — M7: Classify every timeout by root cause.

Produces <run_dir>/analysis/rq1/:
  rq1_timeout_root_cause.csv  — per-timeout classification

Categories (applied in precedence order):
  1. Capacity gap   — server_count insufficient, CPU high
  2. Cold start     — node provisioned < 30s before request
  3. Storage bound  — storage_count < 3 AND T_db elevated
  4. WAN saturation — cross-region, p95 cross-region latency > 25s
  5. Transient spike — isolated, >= 90% neighbors succeeded
  6. Unclassified   — catch-all

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.timeout_root_cause --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from ...loader import load_run
from ...client_status import is_timeout


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bucket_key(ts: float, bucket_s: int = 10) -> int:
    """Return bucket start timestamp for a given timestamp."""
    return int(ts // bucket_s) * bucket_s


def _is_timeout_row(row: dict) -> bool:
    """True when *row* is a timeout.

    Open-loop rows carry ``status="timeout"``. Legacy 13-column CSVs (no
    ``status`` column) identified timeouts by elapsed latency at the cap
    (>= 29.9 s with CURL_MAX_TIME=30); the latency heuristic is kept for
    those legacy rows only.
    """
    if "status" in row:
        return is_timeout(row)
    return _safe_float(row.get("latency_s")) >= 29.9


def classify_timeouts(run_dir: Path) -> list[dict]:
    """Classify every timeout (latency_s >= 29.9) in the run."""
    run = load_run(run_dir)
    if not run.all_client_rows:
        print("[timeout_root_cause] No client_requests.csv data")
        return []

    # Build lookup structures from resource_stats
    # window_end -> (server_count, storage_count, t_db_median)
    resource_by_window: dict[float, dict] = {}
    for row in run.domain_rows:
        ts = _safe_float(row.get("window_end"))
        if ts <= 0:
            continue
        resource_by_window[ts] = {
            "server_count": _safe_float(row.get("server_count")),
            "storage_count": _safe_float(row.get("storage_count")),
        }

    # Build per-node CPU lookup: window_end -> list of cpu_percent
    cpu_by_window: dict[float, list[float]] = defaultdict(list)
    for row in run.node_rows:
        ts = _safe_float(row.get("window_end"))
        cpu = _safe_float(row.get("cpu_percent"))
        if ts > 0 and cpu > 0:
            cpu_by_window[ts].append(cpu)

    # Build node add times for cold start detection
    node_add_times: list[float] = []
    for row in run.container_event_rows:
        nt = row.get("node_type", "")
        if "compute" in nt.lower():
            add_ts = _safe_float(row.get("add_time"))
            if add_ts > 0:
                node_add_times.append(add_ts)

    # Also check node_lifecycle_timings if available
    nlt_path = run_dir / "node_lifecycle_timings.csv"
    if nlt_path.exists():
        with nlt_path.open(newline="") as f:
            for row in csv.DictReader(f):
                nt = row.get("node_type", "")
                if "compute" in nt.lower():
                    add_ts = _safe_float(row.get("add_time"))
                    if add_ts > 0:
                        node_add_times.append(add_ts)

    # Compute peak server_count per phase for capacity gap detection
    phase_peak_srv: dict[str, float] = {}
    phase_peak_sto: dict[str, float] = {}
    for row in run.domain_rows:
        ph = row.get("phase", "unknown")
        srv = _safe_float(row.get("server_count"))
        sto = _safe_float(row.get("storage_count"))
        if srv > phase_peak_srv.get(ph, 0):
            phase_peak_srv[ph] = srv
        if sto > phase_peak_sto.get(ph, 0):
            phase_peak_sto[ph] = sto

    # Identify timeout rows and classify
    results = []
    for row in run.all_client_rows:
        if not _is_timeout_row(row):
            continue
        lat = _safe_float(row.get("latency_s"))

        sent_at = _safe_float(row.get("sent_at"))
        phase = row.get("phase", "unknown")
        endpoint = row.get("endpoint", "unknown")
        client_lan = row.get("client_lan", "unknown")
        target_region = row.get("target_region", "unknown")

        # Find closest resource window
        window_key = _bucket_key(sent_at)
        res = resource_by_window.get(float(window_key), {})
        srv_at_req = res.get("server_count", 0)
        sto_at_req = res.get("storage_count", 0)

        # CPU at request time
        cpu_list = cpu_by_window.get(float(window_key), [])
        cpu_p95 = _percentile(cpu_list, 0.95) if cpu_list else 0

        # Cross-region?
        is_cross_region = (client_lan != target_region)

        # Cold start?
        is_cold_start = any(abs(sent_at - t) < 30 for t in node_add_times)

        # Compute per-bucket success rate for transient detection
        bucket_reqs = [r for r in run.all_client_rows
                       if r.get("phase") == phase
                       and r.get("endpoint") == endpoint
                       and _bucket_key(_safe_float(r.get("sent_at"))) == window_key]
        bucket_timeouts = sum(1 for r in bucket_reqs if _is_timeout_row(r))
        bucket_total = len(bucket_reqs)
        success_rate = (bucket_total - bucket_timeouts) / bucket_total if bucket_total > 0 else 1.0

        # Classify (precedence order)
        peak_srv = phase_peak_srv.get(phase, srv_at_req)

        # 1. Capacity gap
        if srv_at_req < peak_srv and cpu_p95 > 40:
            category = "capacity_gap"
        # 2. Cold start
        elif is_cold_start:
            category = "cold_start"
        # 3. Storage bound — storage_count insufficient AND endpoint is storage-heavy
        elif (sto_at_req < 3 and endpoint in ("content_lookup", "content_update", "content_aggregate")):
            category = "storage_bound"
        # 4. WAN saturation
        elif is_cross_region:
            category = "wan_saturation"
        # 5. Transient spike
        elif success_rate >= 0.90:
            category = "transient_spike"
        # 6. Unclassified
        else:
            category = "unclassified"

        results.append({
            "sent_at": sent_at,
            "phase": phase,
            "endpoint": endpoint,
            "client_lan": client_lan,
            "target_region": target_region,
            "latency_s": round(lat, 3),
            "category": category,
            "server_count": int(srv_at_req),
            "storage_count": int(sto_at_req),
            "cpu_p95": round(cpu_p95, 1),
            "peak_server_count": int(peak_srv),
            "note": "",
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="M7: Timeout root cause classification")
    parser.add_argument("--run-dir", required=True, type=Path, help="Path to run directory")
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    print(f"[timeout_root_cause] run_dir={run_dir}")

    rows = classify_timeouts(run_dir)
    if not rows:
        print("[timeout_root_cause] No timeouts to classify")
        return

    out_dir = run_dir / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "rq1_timeout_root_cause.csv"

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "sent_at", "phase", "endpoint", "client_lan", "target_region",
            "latency_s", "category", "server_count", "storage_count",
            "cpu_p95", "peak_server_count", "note",
        ])
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    from collections import Counter
    cat_counts = Counter(r["category"] for r in rows)
    print(f"[timeout_root_cause] wrote {out_path} ({len(rows)} timeouts)")
    for cat, count in cat_counts.most_common():
        print(f"  {cat}: {count} ({count/len(rows)*100:.1f}%)")


if __name__ == "__main__":
    main()
