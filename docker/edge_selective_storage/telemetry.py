"""Telemetry emission for the selective-sync supervisor.

Shares the aggregator PUSH socket contract with ``mongo_telemetry.py``: one
ZMQ frame per Change Stream event, tagged with this container's MAC and
``member_state="STANDALONE_CACHE"``.
"""

from __future__ import annotations

import logging
import os
import time

import zmq

logger = logging.getLogger("selective_sync.telemetry")

_zmq_ctx = zmq.Context.instance()
_telemetry_sock: zmq.Socket | None = None


def _aggregator_addr_from_lan() -> str:
    lan_id = os.environ.get("LAN_ID", "")
    if not lan_id.startswith("lan"):
        return ""
    third = int(lan_id[3:]) - 1  # lan1 → 10.0.0.5, lan2 → 10.0.1.5
    return f"tcp://10.0.{third}.5:5555"


def _get_telemetry_sock() -> zmq.Socket | None:
    """Return a lazily-connected PUSH socket to the local aggregator."""
    global _telemetry_sock
    if _telemetry_sock is not None:
        return _telemetry_sock
    addr = os.environ.get("AGGREGATOR_PULL_ADDR", "") or _aggregator_addr_from_lan()
    if not addr:
        logger.info("No aggregator address configured — telemetry disabled")
        return None
    sock = _zmq_ctx.socket(zmq.PUSH)
    sock.connect(addr)
    _telemetry_sock = sock
    logger.info("Telemetry PUSH socket connected to %s", addr)
    return _telemetry_sock


def _discover_mac() -> str:
    preferred = os.environ.get("IFACE", "eth0")
    candidates = [preferred]
    try:
        candidates += sorted(os.listdir("/sys/class/net"))
    except OSError:
        pass
    for iface in candidates:
        if iface == "lo":
            continue
        try:
            with open(f"/sys/class/net/{iface}/address") as f:
                mac = f.read().strip()
            if mac and mac != "00:00:00:00:00:00":
                return mac
        except OSError:
            continue
    return "unknown"


_SERVER_MAC = _discover_mac()


def get_server_mac() -> str:
    """Return the cached MAC, re-discovering if eth0 wasn't available yet."""
    global _SERVER_MAC
    if _SERVER_MAC == "unknown":
        _SERVER_MAC = _discover_mac()
    return _SERVER_MAC


def emit_telemetry(collection: str, *, lag_s: float,
                   token_age_s: float, hot_doc_count: int) -> None:
    """Push a ``selective_sync_per_collection`` frame for one collection.

    Shape matches the aggregator contract in
    ``docs/operation/elasticy_manager/implementation/tier1_selective_sync/
    telemetry_and_config.md`` §2.2.
    """
    sock = _get_telemetry_sock()
    if sock is None:
        return
    frame = {
        "server_mac":   get_server_mac(),
        "member_state": "STANDALONE_CACHE",
        "selective_sync_per_collection": {
            collection: {
                "lag_s":              float(lag_s),
                "resume_token_age_s": float(token_age_s),
                "hot_doc_count":      int(hot_doc_count),
            },
        },
        "ts": time.time(),
    }
    try:
        sock.send_json(frame, zmq.NOBLOCK)
    except zmq.Again:
        pass  # drop under backpressure; next event will carry fresh values


def compute_lag_s(change: dict) -> float:
    """Return wall-clock seconds between ``change.clusterTime`` and now."""
    ct = change.get("clusterTime")
    if ct is None:
        return 0.0
    # bson.Timestamp exposes ``.time`` (epoch seconds, int).
    try:
        return max(0.0, time.time() - float(ct.time))
    except AttributeError:
        return 0.0


def emit_control_event(event_type: str, **fields) -> None:
    """Push a mini-telemetry frame carrying a single control event.

    Shape matches the ``control_events: list[dict]`` piggyback the aggregator
    already forwards (same channel compute containers use for
    ``drain_complete`` / ``rs_secondary_ready``). Non-blocking: drops under
    backpressure like :func:`emit_telemetry`.
    """
    sock = _get_telemetry_sock()
    if sock is None:
        return
    event = {"event_type": event_type, "ts": time.time(), **fields}
    frame = {
        "server_mac":     get_server_mac(),
        "member_state":   "STANDALONE_CACHE",
        "control_events": [event],
        "ts":             event["ts"],
    }
    try:
        sock.send_json(frame, zmq.NOBLOCK)
    except zmq.Again:
        logger.warning("control event %s dropped under backpressure", event_type)
