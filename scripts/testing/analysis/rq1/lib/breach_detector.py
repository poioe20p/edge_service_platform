"""Shared breach detection: replicates the controller's degradation_score
and threshold logic from telemetry data.

Used by both cli.timings (reaction latency) and
cli.decision_quality (scaling outcome description).
"""
from __future__ import annotations


def degradation_score(cpu: float, latency: float,
                      w_cpu: float, w_lat: float,
                      cpu_floor: float, cpu_span: float,
                      lat_floor: float, lat_span: float) -> float:
    """Weighted degradation score in [0, w_cpu + w_lat].

    Exact replica of ScalingPolicy.degradation_score().
    Both components saturate at 1.0.
    """
    if cpu_span:
        cpu_component = min(1.0, max(0.0, cpu - cpu_floor) / cpu_span)
    else:
        cpu_component = 0.0
    if lat_span:
        lat_component = min(1.0, max(0.0, latency - lat_floor) / lat_span)
    else:
        lat_component = 0.0
    return w_cpu * cpu_component + w_lat * lat_component


def storage_latency_signal(avg_time_db_ms: float,
                           p95_time_db_ms: float) -> float:
    """Tail-aware DB latency signal. Replicates ScalingPolicy method."""
    return max(avg_time_db_ms, p95_time_db_ms)


def load_thresholds(env_snapshot: dict[str, str]) -> dict:
    """Extract scale-up thresholds from controller_env_snapshot.env.

    Returns a dict with keys matching scaling_config.py names.
    Falls back to scaling_config.py defaults (not the override file).
    """
    def _f(key: str, default: float) -> float:
        try:
            return float(env_snapshot.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    return {
        # Compute weights — defaults from scaling_config.py
        "W_CPU":                          _f("SCALEUP_W_CPU", 0.40),
        "W_T_PROC":                       _f("SCALEUP_W_T_PROC", 0.60),
        "CPU_FLOOR":                      _f("SCALEUP_CPU_FLOOR", 5.0),
        "CPU_SPAN":                       _f("SCALEUP_CPU_SPAN", 10.0),
        "T_PROC_FLOOR":                   _f("SCALEUP_T_PROC_FLOOR", 20.0),
        "T_PROC_SPAN":                    _f("SCALEUP_T_PROC_SPAN", 80.0),
        "COMPUTE_BASE_THRESHOLD":         _f("SCALEUP_COMPUTE_BASE_THRESHOLD", 0.45),
        "COMPUTE_THRESHOLD_INCREMENT":    _f("SCALEUP_COMPUTE_THRESHOLD_INCREMENT", 0.10),
        "COMPUTE_MAX_THRESHOLD":          _f("SCALEUP_COMPUTE_MAX_THRESHOLD", 0.85),
        "COMPUTE_COOLDOWN_S":             _f("SCALEUP_COMPUTE_COOLDOWN_S", 45.0),
        "COMPUTE_PEER_RELIEF":            _f("SCALEUP_COMPUTE_PEER_RELIEF", 0.03),
        "COMPUTE_PEER_HEALTH_THRESHOLD":  _f("SCALEUP_COMPUTE_PEER_HEALTH_THRESHOLD", 0.35),
        # Storage weights — defaults from scaling_config.py
        "W_STORAGE_CPU":                  _f("SCALEUP_W_STORAGE_CPU", 0.70),
        "W_T_DB":                         _f("SCALEUP_W_T_DB", 0.30),
        "STORAGE_CPU_FLOOR":              _f("SCALEUP_STORAGE_CPU_FLOOR", 5.0),
        "STORAGE_CPU_SPAN":               _f("SCALEUP_STORAGE_CPU_SPAN", 10.0),
        "T_DB_FLOOR":                     _f("SCALEUP_T_DB_FLOOR", 150.0),
        "T_DB_SPAN":                      _f("SCALEUP_T_DB_SPAN", 600.0),
        "STORAGE_BASE_THRESHOLD":         _f("SCALEUP_STORAGE_BASE_THRESHOLD", 0.25),
        "STORAGE_THRESHOLD_INCREMENT":    _f("SCALEUP_STORAGE_THRESHOLD_INCREMENT", 0.10),
        "STORAGE_MIN_INCREMENT":          _f("SCALEUP_STORAGE_MIN_INCREMENT", 0.05),
        "STORAGE_MAX_THRESHOLD":          _f("SCALEUP_STORAGE_MAX_THRESHOLD", 0.55),
        "STORAGE_COOLDOWN_S":             _f("SCALEUP_STORAGE_COOLDOWN_S", 120.0),
    }


def _effective_compute_threshold(thresholds: dict, node_count: int,
                                  peer_score: float | None) -> float:
    """Compute effective threshold with dynamic count + peer relief."""
    base = thresholds["COMPUTE_BASE_THRESHOLD"]
    inc = thresholds["COMPUTE_THRESHOLD_INCREMENT"]
    cap = thresholds["COMPUTE_MAX_THRESHOLD"]
    dynamic_part = node_count * inc

    peer_relief = 0.0
    if (peer_score is not None
            and peer_score <= thresholds["COMPUTE_PEER_HEALTH_THRESHOLD"]):
        peer_relief = thresholds["COMPUTE_PEER_RELIEF"]

    return min(base + dynamic_part + peer_relief, cap)


def _effective_storage_threshold(thresholds: dict, node_count: int) -> float:
    """Compute effective storage threshold with diminishing increments.

    Replicates the controller's:
      cumulative = Σ max(increment × 0.5ⁱ, min_increment)
    """
    base = thresholds["STORAGE_BASE_THRESHOLD"]
    inc = thresholds["STORAGE_THRESHOLD_INCREMENT"]
    min_inc = thresholds["STORAGE_MIN_INCREMENT"]
    cap = thresholds["STORAGE_MAX_THRESHOLD"]

    cumulative = sum(
        max(inc * 0.5 ** i, min_inc)
        for i in range(node_count)
    )
    return min(base + cumulative, cap)


def _find_peer_row(window_end: float,
                   peer_rows: list[dict]) -> dict | None:
    """Return the peer-LAN debug row closest to (and ≤) window_end."""
    best = None
    for row in peer_rows:
        w = float(row.get("window_end", 0))
        if w <= window_end and (best is None
                                or w > float(best.get("window_end", 0))):
            best = row
    return best


def _col(row: dict, *names: str, default: float = 0.0) -> float:
    """Return the first available column value from *names* (handles both
    domain_rows and debug_rows naming conventions)."""
    for name in names:
        val = row.get(name)
        if val is not None and str(val).strip() != "":
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return default


def detect_breaches(
    debug_rows: list[dict],
    thresholds: dict,
) -> list[dict]:
    """Detect trigger events accounting for dynamic threshold, peer
    relief, and cooldown.

    Accepts either domain_rows or debug_rows — column lookups try both
    naming conventions (e.g. average_cpu_percent / median_cpu_percent).

    Returns trigger events only — windows suppressed by cooldown or
    peer relief are excluded.  Each trigger:
      {network_id, window_end, tier, score, threshold,
       peer_relief, dynamic_count}
    """
    triggers = []
    sorted_rows = sorted(debug_rows,
                         key=lambda r: float(r.get("window_end", 0)))

    by_lan: dict[str, list[dict]] = {}
    for row in sorted_rows:
        by_lan.setdefault(row.get("network_id", ""), []).append(row)

    compute_count = 0
    storage_count = 0
    last_compute_trigger = -999999.0
    last_storage_trigger = -999999.0

    for row in sorted_rows:
        network_id = row.get("network_id", "")
        window_end = float(row.get("window_end", 0))
        peer_lan = "lan2" if network_id == "lan1" else "lan1"

        avg_cpu = _col(row, "average_cpu_percent", "median_cpu_percent")
        avg_proc = _col(row, "avg_time_proc_ms", "median_time_proc_ms")
        avg_storage_cpu = _col(row, "avg_storage_cpu_percent",
                               "median_storage_cpu_percent")
        avg_db = _col(row, "avg_time_db_ms", "median_time_db_ms")
        p95_db = _col(row, "p95_time_db_ms", "t_db_p95_ms_owner_lan")

        compute_score = degradation_score(
            avg_cpu, avg_proc,
            thresholds["W_CPU"], thresholds["W_T_PROC"],
            thresholds["CPU_FLOOR"], thresholds["CPU_SPAN"],
            thresholds["T_PROC_FLOOR"], thresholds["T_PROC_SPAN"],
        )

        peer_score = None
        peer_row = _find_peer_row(window_end, by_lan.get(peer_lan, []))
        if peer_row:
            peer_score = degradation_score(
                _col(peer_row, "average_cpu_percent", "median_cpu_percent"),
                _col(peer_row, "avg_time_proc_ms", "median_time_proc_ms"),
                thresholds["W_CPU"], thresholds["W_T_PROC"],
                thresholds["CPU_FLOOR"], thresholds["CPU_SPAN"],
                thresholds["T_PROC_FLOOR"], thresholds["T_PROC_SPAN"],
            )

        compute_threshold = _effective_compute_threshold(
            thresholds, compute_count, peer_score)

        cooldown_ok = (window_end - last_compute_trigger
                       >= thresholds["COMPUTE_COOLDOWN_S"])

        if compute_score >= compute_threshold and cooldown_ok:
            peer_relief = 0.0
            if (peer_score is not None
                    and peer_score <= thresholds["COMPUTE_PEER_HEALTH_THRESHOLD"]):
                peer_relief = thresholds["COMPUTE_PEER_RELIEF"]
            triggers.append({
                "network_id": network_id,
                "window_end": window_end,
                "tier": "compute",
                "score": round(compute_score, 4),
                "threshold": round(compute_threshold, 4),
                "peer_relief": peer_relief,
                "dynamic_count": compute_count,
            })
            compute_count += 1
            last_compute_trigger = window_end

        db_latency = storage_latency_signal(avg_db, p95_db)
        storage_score = degradation_score(
            avg_storage_cpu, db_latency,
            thresholds["W_STORAGE_CPU"], thresholds["W_T_DB"],
            thresholds["STORAGE_CPU_FLOOR"], thresholds["STORAGE_CPU_SPAN"],
            thresholds["T_DB_FLOOR"], thresholds["T_DB_SPAN"],
        )

        storage_threshold = _effective_storage_threshold(
            thresholds, storage_count)

        cooldown_ok = (window_end - last_storage_trigger
                       >= thresholds["STORAGE_COOLDOWN_S"])

        if storage_score >= storage_threshold and cooldown_ok:
            triggers.append({
                "network_id": network_id,
                "window_end": window_end,
                "tier": "storage",
                "score": round(storage_score, 4),
                "threshold": round(storage_threshold, 4),
                "peer_relief": 0.0,
                "dynamic_count": storage_count,
            })
            storage_count += 1
            last_storage_trigger = window_end

    return triggers


def load_env_snapshot(run_dir: str) -> dict[str, str]:
    """Read controller_env_snapshot.env into a key-value dict."""
    import os
    env = {}
    path = os.path.join(run_dir, "controller_env_snapshot.env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env
