"""Simple run-summary metrics derived from request and container-event artifacts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import ceil, floor

from .client_status import is_completed, is_failure as _cs_is_failure


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_iso_ts(value: str | None) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = floor(rank)
    upper = ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def infer_origin_ts(run) -> float:
    candidates: list[float] = []

    for row in getattr(run, "container_event_rows", []):
        ts = parse_iso_ts(row.get("timestamp_iso"))
        if ts > 0:
            candidates.append(ts)

    for row in getattr(run, "all_client_rows", []):
        ts = parse_iso_ts(row.get("sent_at"))
        if ts > 0:
            candidates.append(ts)

    for row in getattr(run, "domain_rows", []):
        ts = safe_float(row.get("window_end"), 0.0)
        if ts > 0:
            candidates.append(ts)

    if candidates:
        return min(candidates)
    return getattr(run, "t0", 0.0)


def infer_end_s(run, origin_ts: float) -> float:
    candidates: list[float] = []

    for row in getattr(run, "container_event_rows", []):
        ts = parse_iso_ts(row.get("timestamp_iso"))
        if ts > 0:
            candidates.append(max(0.0, ts - origin_ts))
        else:
            candidates.append(max(0.0, safe_float(row.get("monotonic_s"), 0.0)))

    for row in getattr(run, "all_client_rows", []):
        ts = parse_iso_ts(row.get("sent_at"))
        if ts > 0:
            candidates.append(max(0.0, ts - origin_ts))

    for row in getattr(run, "domain_rows", []):
        ts = safe_float(row.get("window_end"), 0.0)
        if ts > 0:
            candidates.append(max(0.0, ts - origin_ts))

    return max(candidates) if candidates else 0.0


def is_failure(row: dict, header=None) -> bool:
    """Status-aware failure: only *completed* rows with http_status not 200/blank.

    ``timeout``/``dropped``/``canceled`` rows are never failures. Legacy
    13-column CSVs have no ``status`` column, so every row is ``completed``
    and this matches the previous ``http_status``-based behavior.
    """
    return _cs_is_failure(row, header)


def bucket_client_rows(rows: list[dict], origin_ts: float, bucket_s: int = 30) -> list[dict]:
    if bucket_s <= 0:
        raise ValueError("bucket_s must be > 0")

    buckets: dict[int, dict[str, object]] = defaultdict(
        lambda: {"latencies_ms": [], "request_count": 0, "completed_count": 0, "failure_count": 0}
    )
    max_index = -1

    for row in rows:
        ts = parse_iso_ts(row.get("timestamp"))
        if ts <= 0:
            continue
        rel_s = max(0.0, ts - origin_ts)
        index = int(rel_s // bucket_s)
        bucket = buckets[index]
        bucket["request_count"] = int(bucket["request_count"]) + 1
        if not is_completed(row):
            continue
        bucket["completed_count"] = int(bucket["completed_count"]) + 1
        bucket["failure_count"] = int(bucket["failure_count"]) + int(is_failure(row))
        bucket["latencies_ms"].append(1000.0 * safe_float(row.get("latency_s"), 0.0))
        max_index = max(max_index, index)

    if max_index < 0:
        return []

    out: list[dict] = []
    for index in range(max_index + 1):
        bucket = buckets[index]
        latencies = list(bucket["latencies_ms"])
        request_count = int(bucket["request_count"])
        completed_count = int(bucket["completed_count"])
        failure_count = int(bucket["failure_count"])
        avg_latency_ms = (sum(latencies) / completed_count) if completed_count else 0.0
        p95_latency_ms = percentile(latencies, 0.95) if latencies else 0.0
        p99_latency_ms = percentile(latencies, 0.99) if latencies else 0.0
        failure_rate_pct = (100.0 * failure_count / completed_count) if completed_count else 0.0
        out.append({
            "bucket_index": index,
            "bucket_start_s": index * bucket_s,
            "bucket_mid_s": index * bucket_s + (bucket_s / 2.0),
            "bucket_end_s": (index + 1) * bucket_s,
            "request_count": request_count,
            "completed_count": completed_count,
            "failure_count": failure_count,
            "success_count": completed_count - failure_count,
            "avg_latency_ms": avg_latency_ms,
            "p95_latency_ms": p95_latency_ms,
            "p99_latency_ms": p99_latency_ms,
            "failure_rate_pct": failure_rate_pct,
        })
    return out


def container_role(name: str | None) -> str | None:
    if not name:
        return None
    if name.startswith("edge_server"):
        return "compute"
    if name.startswith("edge_storage"):
        return "storage"
    if name.startswith("sel_sync_"):
        return "selective"
    return None


def build_container_step_series(event_rows: list[dict], origin_ts: float) -> list[dict]:
    running_by_container: dict[str, str] = {}
    points: list[dict] = []

    def append_point(t_s: float) -> None:
        compute = sum(1 for role in running_by_container.values() if role == "compute")
        storage = sum(1 for role in running_by_container.values() if role == "storage")
        selective = sum(1 for role in running_by_container.values() if role == "selective")
        point = {
            "t_s": t_s,
            "compute_nodes": compute,
            "storage_nodes": storage,
            "selective_nodes": selective,
            "total_nodes": compute + storage + selective,
        }
        if points and abs(points[-1]["t_s"] - t_s) < 1e-9:
            points[-1] = point
        else:
            points.append(point)

    ordered = sorted(
        event_rows,
        key=lambda row: (
            parse_iso_ts(row.get("timestamp_iso")),
            safe_float(row.get("monotonic_s"), 0.0),
            row.get("container", ""),
        ),
    )

    for row in ordered:
        name = row.get("container", "")
        role = container_role(name)
        if role is None:
            continue

        ts = parse_iso_ts(row.get("timestamp_iso"))
        rel_s = max(0.0, ts - origin_ts) if ts > 0 else max(0.0, safe_float(row.get("monotonic_s"), 0.0))
        event = str(row.get("event", "")).lower()
        state = str(row.get("state", "")).lower()

        if event == "removed" or state != "running":
            running_by_container.pop(name, None)
        else:
            running_by_container[name] = role

        append_point(rel_s)

    if points and points[0]["t_s"] > 0.0:
        points.insert(0, {
            "t_s": 0.0,
            "compute_nodes": 0,
            "storage_nodes": 0,
            "selective_nodes": 0,
            "total_nodes": 0,
        })
    return points


def summarize_client_rows(rows: list[dict]) -> dict:
    request_count = len(rows)
    completed = [row for row in rows if is_completed(row)]
    completed_count = len(completed)
    failure_count = sum(1 for row in completed if is_failure(row))
    latencies_ms = [1000.0 * safe_float(row.get("latency_s"), 0.0) for row in completed]
    avg_latency_ms = (sum(latencies_ms) / completed_count) if completed_count else 0.0
    p95_latency_ms = percentile(latencies_ms, 0.95) if latencies_ms else 0.0
    p99_latency_ms = percentile(latencies_ms, 0.99) if latencies_ms else 0.0
    failure_rate_pct = (100.0 * failure_count / completed_count) if completed_count else 0.0
    return {
        "request_count": request_count,
        "completed_count": completed_count,
        "failure_count": failure_count,
        "avg_latency_ms": avg_latency_ms,
        "p95_latency_ms": p95_latency_ms,
        "p99_latency_ms": p99_latency_ms,
        "failure_rate_pct": failure_rate_pct,
    }


def summarize_client_rows_by_phase(rows: list[dict], phase_names: list[str]) -> list[dict]:
    by_phase: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_phase[str(row.get("phase", "unknown"))].append(row)

    out: list[dict] = []
    ordered_names = list(phase_names)
    for name in by_phase:
        if name not in ordered_names:
            ordered_names.append(name)

    for phase in ordered_names:
        items = by_phase.get(phase, [])
        if not items:
            continue
        summary = summarize_client_rows(items)
        out.append({"phase": phase, **summary})
    return out


def time_weighted_mean(points: list[dict], key: str, end_s: float) -> float:
    if not points:
        return 0.0
    if end_s <= 0:
        return safe_float(points[-1].get(key), 0.0)

    area = 0.0
    prev_t = 0.0
    prev_value = 0.0
    for point in points:
        point_t = max(0.0, min(end_s, safe_float(point.get("t_s"), 0.0)))
        area += max(0.0, point_t - prev_t) * prev_value
        prev_t = point_t
        prev_value = safe_float(point.get(key), 0.0)
    area += max(0.0, end_s - prev_t) * prev_value
    return area / end_s


def max_step_value(points: list[dict], key: str) -> float:
    if not points:
        return 0.0
    return max(safe_float(point.get(key), 0.0) for point in points)