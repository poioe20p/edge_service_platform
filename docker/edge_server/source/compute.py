"""compute.py — CPU-intensive edge analytics applied to pre-fetched data.

These functions implement generic edge analytics patterns — per-request data
enrichment, temporal trend detection, local service-pressure summarization,
and multi-factor composite ranking — instantiated with content-discovery
semantics for the experimental workload.

All functions are pure compute — no I/O, no DB calls. They operate on
data already retrieved by the workload route module.
"""

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone


# Relevance thresholds (fraction of relevance_baseline)
RELEVANCE_LEVELS = {
    "hot": 1.0,
    "trending": 0.85,
    "steady": 0.70,
    "quiet": 0.0,
}

TAG_PRIORITY = {
    "premium": 2.0,
    "trending": 1.8,
    "news": 1.3,
    "sports": 1.2,
    "technology": 1.1,
    "finance": 1.1,
    "health": 1.0,
    "education": 1.0,
    "science": 1.0,
    "entertainment": 0.9,
    "featured": 0.8,
    "archived": 0.5,
}

TREND_WINDOW_SIZE = 20

STALENESS_HALF_LIFE_S = 300.0


def score_content_relevance(
    engagement: float | None,
    baseline: float | None,
    content_type: str,
    content_id: str,
) -> dict:
    """Compute a multi-level relevance classification for a content item.

    Returns:
        {
            "relevance": "hot" | "trending" | "steady" | "quiet",
            "relevance_score": float,  # 0.0 = perfectly quiet, >1.0 = above baseline
            "calibration_hash": float,  # content-specific jitter factor
            "above_baseline": bool,
        }
    """
    if engagement is None or baseline is None or baseline == 0:
        return {
            "relevance": "quiet",
            "relevance_score": 0.0,
            "calibration_hash": 0.0,
            "above_baseline": False,
        }

    raw_ratio = engagement / baseline

    h = hashlib.sha256(content_id.encode()).hexdigest()
    calibration_hash = (int(h[:8], 16) % 1000) / 10000.0
    calibrated_ratio = raw_ratio + calibration_hash

    type_seed = sum(ord(c) for c in content_type) % 10
    exponent = 1.0 + type_seed * 0.1
    relevance_score = calibrated_ratio ** exponent

    relevance = "quiet"
    for level, cutoff in RELEVANCE_LEVELS.items():
        if calibrated_ratio >= cutoff:
            relevance = level
            break

    return {
        "relevance": relevance,
        "relevance_score": round(relevance_score, 4),
        "calibration_hash": round(calibration_hash, 6),
        "above_baseline": calibrated_ratio >= 1.0,
    }


def compute_trend(request_events: list[dict]) -> dict:
    """Compute a linear-regression trend over recent request events for a content item.

    Each event is expected to have 'latency_ms' and 'timestamp' fields.

    Returns:
        {
            "trend_slope": float,       # ms per second (positive = worsening)
            "trend_label": str,         # "rising" | "falling" | "stable" | "insufficient_data"
            "sample_count": int,
            "latency_mean": float,
            "latency_std": float,
        }
    """
    if len(request_events) < 3:
        return {
            "trend_slope": 0.0,
            "trend_label": "insufficient_data",
            "sample_count": len(request_events),
            "latency_mean": 0.0,
            "latency_std": 0.0,
        }

    points = []
    for ev in request_events:
        lat = ev.get("latency_ms")
        ts = ev.get("timestamp")
        if lat is None or ts is None:
            continue
        if isinstance(ts, datetime):
            epoch = ts.timestamp()
        elif isinstance(ts, (int, float)):
            epoch = float(ts)
        else:
            continue
        points.append((epoch, float(lat)))

    if len(points) < 3:
        return {
            "trend_slope": 0.0,
            "trend_label": "insufficient_data",
            "sample_count": len(points),
            "latency_mean": 0.0,
            "latency_std": 0.0,
        }

    n = len(points)
    x_vals = [p[0] for p in points]
    y_vals = [p[1] for p in points]
    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in points)
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    slope = numerator / denominator if denominator != 0 else 0.0

    if abs(slope) < 0.01:
        label = "stable"
    elif slope > 0:
        label = "rising"
    else:
        label = "falling"

    lat_values = [p[1] for p in points]

    return {
        "trend_slope": round(slope, 6),
        "trend_label": label,
        "sample_count": n,
        "latency_mean": round(statistics.mean(lat_values), 2),
        "latency_std": round(statistics.pstdev(lat_values), 2) if n > 1 else 0.0,
    }


def compute_service_pressure(
    events: list[dict],
    limit: int,
    region: str,
    window_seconds: float,
) -> dict:
    """Summarize recent local request activity for the serving edge server.

    Returns a summary plus the top content items and tags contributing to recent
    service pressure on this edge.
    """
    request_count = len(events)
    if request_count == 0:
        return {
            "region_served": region,
            "summary": {
                "request_count": 0,
                "unique_content_count": 0,
                "request_rate_rps": 0.0,
                "latency_mean_ms": 0.0,
                "latency_p95_ms": 0.0,
                "top_content_share": 0.0,
                "pressure_score": 0.0,
                "pressure_label": "low",
            },
            "request_kind_counts": {},
            "top_content": [],
            "top_tags": [],
        }

    window_seconds = max(window_seconds, 1.0)
    latencies = [float(ev.get("latency_ms", 0.0)) for ev in events]

    content_items: dict[str, dict] = {}
    tags: dict[str, dict] = {}
    request_kind_counts: dict[str, int] = {}

    for ev in events:
        request_kind = str(ev.get("request_kind", "unknown"))
        request_kind_counts[request_kind] = request_kind_counts.get(request_kind, 0) + 1

        content_id = ev.get("content_id")
        latency = float(ev.get("latency_ms", 0.0))
        timestamp = float(ev.get("timestamp", 0.0))
        relevance = ev.get("relevance", "quiet")
        status = ev.get("status", "unknown")
        event_tags = tuple(ev.get("tags") or ())

        for tag in event_tags:
            tag_stats = tags.setdefault(
                tag,
                {
                    "tag": tag,
                    "request_count": 0,
                    "content_ids": set(),
                    "latencies": [],
                },
            )
            tag_stats["request_count"] += 1
            if content_id:
                tag_stats["content_ids"].add(content_id)
            tag_stats["latencies"].append(latency)

        if not content_id:
            continue

        content_stats = content_items.setdefault(
            content_id,
            {
                "content_id": content_id,
                "request_count": 0,
                "latencies": [],
                "last_seen_epoch": 0.0,
                "last_relevance": "quiet",
                "last_status": "unknown",
                "tags": (),
            },
        )
        content_stats["request_count"] += 1
        content_stats["latencies"].append(latency)
        if timestamp >= content_stats["last_seen_epoch"]:
            content_stats["last_seen_epoch"] = timestamp
            content_stats["last_relevance"] = relevance
            content_stats["last_status"] = status
            content_stats["tags"] = event_tags

    top_content = []
    for content_stats in content_items.values():
        content_latencies = content_stats.pop("latencies")
        top_content.append(
            {
                "content_id": content_stats["content_id"],
                "request_count": content_stats["request_count"],
                "avg_latency_ms": round(statistics.mean(content_latencies), 2),
                "last_seen_epoch": round(content_stats["last_seen_epoch"], 3),
                "last_relevance": content_stats["last_relevance"],
                "last_status": content_stats["last_status"],
                "tags": list(content_stats["tags"]),
            }
        )
    top_content.sort(key=lambda d: (d["request_count"], d["avg_latency_ms"]), reverse=True)
    top_content = top_content[:limit]

    top_tags = []
    for tag_stats in tags.values():
        tag_latencies = tag_stats["latencies"]
        top_tags.append(
            {
                "tag": tag_stats["tag"],
                "request_count": tag_stats["request_count"],
                "unique_content_count": len(tag_stats["content_ids"]),
                "avg_latency_ms": round(statistics.mean(tag_latencies), 2),
            }
        )
    top_tags.sort(key=lambda d: (d["request_count"], d["avg_latency_ms"]), reverse=True)
    top_tags = top_tags[:limit]

    request_rate_rps = request_count / window_seconds
    latency_mean_ms = statistics.mean(latencies)
    latency_p95_ms = _percentile(latencies, 0.95)
    top_content_share = (top_content[0]["request_count"] / request_count) if top_content else 0.0

    rate_component = _normalize(request_rate_rps, 10.0)
    latency_component = _normalize(latency_p95_ms, 150.0)
    concentration_component = _normalize(top_content_share, 0.5)

    pressure_score = (
        0.35 * rate_component
        + 0.30 * latency_component
        + 0.20 * concentration_component
    )
    if pressure_score < 0.35:
        pressure_label = "low"
    elif pressure_score < 0.65:
        pressure_label = "moderate"
    else:
        pressure_label = "high"

    return {
        "region_served": region,
        "summary": {
            "request_count": request_count,
            "unique_content_count": len(content_items),
            "request_rate_rps": round(request_rate_rps, 2),
            "latency_mean_ms": round(latency_mean_ms, 2),
            "latency_p95_ms": round(latency_p95_ms, 2),
            "top_content_share": round(top_content_share, 4),
            "pressure_score": round(pressure_score, 4),
            "pressure_label": pressure_label,
        },
        "request_kind_counts": dict(
            sorted(request_kind_counts.items(), key=lambda item: item[1], reverse=True)
        ),
        "top_content": top_content,
        "top_tags": top_tags,
    }


def _normalize(value: float, ceiling: float) -> float:
    if ceiling <= 0:
        return 0.0
    return max(0.0, min(value / ceiling, 1.0))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]

    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def score_feed_relevance(content_items: list[dict], now: datetime | None = None) -> list[dict]:
    """Compute multi-factor relevance scores for feed content candidates.

    For each content item, relevance combines:
      - Baseline proximity: engagement / relevance_baseline (exponential near 1.0)
      - Tag priority: sum of TAG_PRIORITY weights for the content item's tags
      - Status weight: numeric mapping of payload.status
      - Staleness penalty: exponential decay based on last_updated age

    Each content item dict is enriched with 'urgency_score' and 'urgency_breakdown'.
    The list is re-sorted by urgency_score descending.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    STATUS_WEIGHT = {
        "hot": 3.0,
        "trending": 2.0,
        "quiet": 1.0,
        "unknown":  0.5,
    }

    for item in content_items:
        engagement = item.get("payload", {}).get("engagement")
        baseline = item.get("metadata", {}).get("relevance_baseline")
        if engagement is not None and baseline:
            proximity = engagement / baseline
            proximity_score = proximity ** 3
        else:
            proximity_score = 0.0

        tags = item.get("tags", [])
        tag_score = sum(TAG_PRIORITY.get(t, 0.5) for t in tags)
        tag_score = tag_score / max(len(tags), 1)

        status = item.get("payload", {}).get("status", "unknown")
        status_score = STATUS_WEIGHT.get(status, 0.5)

        last_updated = item.get("last_updated")
        if isinstance(last_updated, datetime):
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
            age_s = (now - last_updated).total_seconds()
        else:
            age_s = STALENESS_HALF_LIFE_S

        decay = math.exp(-0.693 * age_s / STALENESS_HALF_LIFE_S)  # 0.693 = ln(2)

        urgency = (
            0.40 * proximity_score
            + 0.25 * tag_score
            + 0.20 * status_score
            + 0.15 * decay
        )

        item["urgency_score"] = round(urgency, 4)
        item["urgency_breakdown"] = {
            "proximity": round(proximity_score, 4),
            "tag_priority": round(tag_score, 4),
            "status_weight": round(status_score, 4),
            "freshness_decay": round(decay, 4),
        }

    content_items.sort(key=lambda item: item.get("urgency_score", 0), reverse=True)
    return content_items


def compute_feed_summary(content_items: list[dict]) -> dict:
    """Compute summary statistics across all feed content items.

    Returns aggregate metrics: mean/std/min/max of urgency scores and
    engagement-to-baseline ratios, plus counts per status category.
    """
    if not content_items:
        return {"content_count": 0}

    urgency_scores = [item.get("urgency_score", 0) for item in content_items]
    ratios = []
    status_counts: dict[str, int] = {}

    for item in content_items:
        engagement = item.get("payload", {}).get("engagement")
        baseline = item.get("metadata", {}).get("relevance_baseline")
        if engagement is not None and baseline:
            ratios.append(engagement / baseline)

        status = item.get("payload", {}).get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    summary = {
        "content_count": len(content_items),
        "urgency_mean": round(statistics.mean(urgency_scores), 4),
        "urgency_std": round(statistics.pstdev(urgency_scores), 4) if len(urgency_scores) > 1 else 0.0,
        "urgency_max": round(max(urgency_scores), 4),
        "status_distribution": status_counts,
    }

    if ratios:
        summary["ratio_mean"] = round(statistics.mean(ratios), 4)
        summary["ratio_std"] = round(statistics.pstdev(ratios), 4) if len(ratios) > 1 else 0.0
        summary["ratio_max"] = round(max(ratios), 4)

    return summary


def verify_feed_integrity(content_items: list[dict], work_factor: int = 200) -> None:
    """Per-content cryptographic integrity check.

    Simulates edge-side data integrity verification — each content item's payload
    is hashed iteratively to produce a short integrity fingerprint. The
    *work_factor* controls CPU time linearly. Pure compute, no I/O.

    Each content item dict is enriched with ``integrity_hash`` (first 16 hex chars
    of the final SHA-256 digest). The hash is deterministic: same content item
    + same payload always produces the same fingerprint.
    """
    for item in content_items:
        payload = json.dumps(item.get("payload", {}), sort_keys=True, default=str)
        content_id = str(item.get("_id", ""))
        data = f"{content_id}:{payload}".encode()
        h: bytes = data
        for _ in range(work_factor):
            h = hashlib.sha256(h).digest()
        item["integrity_hash"] = h.hex()[:16]


def hash_chain(nonce: str, rounds: int) -> bytes:
    """Chained SHA-256 over a request-bound nonce — pure CPU, no I/O.

    ``rounds`` (HASH_WORK_FACTOR) controls CPU time linearly; total work is
    one seed hash plus ``rounds`` chained rounds (``rounds + 1`` SHA-256
    calls). The result is returned to the caller so the work cannot be
    optimised away. Deterministic: the same ``(nonce, rounds)`` always yields
    the same digest.
    See docs/operation/testing/experiment/v4/rq2/rq2v4_p2_01_hash_kind.md.
    """
    digest = hashlib.sha256(nonce.encode("utf-8")).digest()
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return digest
