import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import zmq
from flask import Flask, g, request


# psutil reads /proc, which Docker does not namespace — it reports host-wide
# values; read this container's cgroup files instead (v2 preferred, v1 fallback).

_CGROUP_ROOT = "/sys/fs/cgroup"
_cpu_state: dict[str, float | None] = {"usec": None, "mono": None}
_cpu_cache: dict[str, float] = {"value": 0.0, "ts": 0.0}
_CPU_CACHE_TTL_S = 1.0
_BOOTSTRAP_RETRY_S = 1.0


def _is_cgroup_v2() -> bool:
    return os.path.exists(f"{_CGROUP_ROOT}/cgroup.controllers")


def _read_cpu_usage_usec() -> int | None:
    try:
        if _is_cgroup_v2():
            with open(f"{_CGROUP_ROOT}/cpu.stat") as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        else:
            with open(f"{_CGROUP_ROOT}/cpuacct/cpuacct.usage") as f:
                return int(f.read().strip()) // 1000  # ns → µs
    except (OSError, ValueError):
        return None
    return None


def _effective_cpu_count() -> float:
    try:
        if _is_cgroup_v2():
            with open(f"{_CGROUP_ROOT}/cpu.max") as f:
                quota, period = f.read().split()
            if quota != "max":
                return int(quota) / int(period)
        else:
            with open(f"{_CGROUP_ROOT}/cpu/cpu.cfs_quota_us") as f:
                quota = int(f.read().strip())
            if quota > 0:
                with open(f"{_CGROUP_ROOT}/cpu/cpu.cfs_period_us") as f:
                    period = int(f.read().strip())
                return quota / period
    except (OSError, ValueError):
        pass
    return float(os.cpu_count() or 1)


def _container_cpu_percent_uncached() -> float:
    """CPU% normalised to the container's quota (100% = full quota).
    First call returns 0.0 (no baseline yet)."""
    now_t = time.monotonic()
    now_u = _read_cpu_usage_usec()
    if now_u is None:
        return 0.0
    last_u = _cpu_state["usec"]
    last_t = _cpu_state["mono"]
    _cpu_state["usec"] = float(now_u)
    _cpu_state["mono"] = now_t
    if last_u is None or last_t is None:
        return 0.0
    elapsed_us = (now_t - last_t) * 1_000_000
    if elapsed_us <= 0:
        return 0.0
    cpus = _effective_cpu_count() or 1.0
    return min(100.0 * (now_u - last_u) / (elapsed_us * cpus), 100.0)


def container_cpu_percent() -> float:
    """Cached CPU% reading. Refreshes at most once per `_CPU_CACHE_TTL_S`
    so per-request emissions don't sample sub-millisecond windows."""
    now = time.monotonic()
    if now - _cpu_cache["ts"] >= _CPU_CACHE_TTL_S:
        _cpu_cache["value"] = _container_cpu_percent_uncached()
        _cpu_cache["ts"] = now
    return _cpu_cache["value"]


def container_ram_used_mb() -> float:
    try:
        if _is_cgroup_v2():
            with open(f"{_CGROUP_ROOT}/memory.current") as f:
                return int(f.read().strip()) / 1_048_576
        with open(f"{_CGROUP_ROOT}/memory/memory.usage_in_bytes") as f:
            return int(f.read().strip()) / 1_048_576
    except (OSError, ValueError):
        return 0.0


_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


def _validated_env_mac() -> str:
    """Return a validated ``OWN_MAC`` env override, or "" when unset/invalid.

    Mirrors ``edge_storage_server/mongo_telemetry.py``: the controller passes
    the static MAC it assigns to this container via ``OWN_MAC``. Honouring it
    makes the edge's self-reported ``server_id`` deterministic even when the
    veth-attach race would otherwise latch a random veth MAC at discovery time.
    """
    configured_mac = os.environ.get("OWN_MAC", "").strip().lower()
    if not configured_mac:
        return ""
    if _MAC_RE.fullmatch(configured_mac):
        return configured_mac
    return ""


def _discover_mac() -> str:
    """Return the MAC address of the first non-loopback interface found in sysfs.

    Prefers the interface named by the IFACE env var (default: eth0), but falls
    back to scanning all interfaces so that containers whose primary interface is
    named differently (e.g. eth1, ens3) still report a real MAC. A validated
    ``OWN_MAC`` env override always wins (the controller assigns the static MAC).
    """
    configured_mac = _validated_env_mac()
    if configured_mac:
        return configured_mac

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


SERVER_MAC: str = _discover_mac()

HEARTBEAT_INTERVAL_S: float = float(os.environ.get("HEARTBEAT_INTERVAL_S", "60"))
# Static nodes opt in with HEARTBEAT_ENABLED=true; dynamic nodes rely on
# scale-down and the telemetry-window absence timeout instead.
HEARTBEAT_ENABLED: bool = (
    os.environ.get("HEARTBEAT_ENABLED", "false").strip().lower() == "true"
)
_sent_bootstrap_heartbeat = False


def _get_server_mac() -> str:
    """Return the cached MAC, re-discovering if the interface wasn't available yet."""
    global SERVER_MAC
    if SERVER_MAC == "unknown":
        SERVER_MAC = _discover_mac()
    return SERVER_MAC


def _aggregator_addr_from_lan() -> str:
    """Derive the aggregator ZMQ PULL address from the LAN_ID env var (e.g. "lan1")."""
    lan_id = os.environ.get("LAN_ID", "")
    if not lan_id.startswith("lan"):
        return ""
    subnet_third_octet = int(lan_id[3:]) - 1  # lan1 → 10.0.0.x, lan2 → 10.0.1.x
    return f"tcp://10.0.{subnet_third_octet}.5:5555"


class MetricSender(ABC):
    @abstractmethod
    def send(self, event: dict) -> None: ...


class ZmqMetricSender(MetricSender):
    def __init__(self) -> None:
        addr = os.environ.get("AGGREGATOR_PULL_ADDR", "") or _aggregator_addr_from_lan()
        self._sock: zmq.Socket | None = None
        self._send_lock = threading.Lock()
        if addr:
            ctx = zmq.Context.instance()
            self._sock = ctx.socket(zmq.PUSH)
            self._sock.connect(addr)

    def send(self, event: dict) -> None:
        if self._sock is None:
            return
        # ZMQ sockets are not thread-safe: heartbeat, drain-monitor and per-request
        # request_complete threads share this sender, so sends are serialized.
        with self._send_lock:
            try:
                self._sock.send_json(event, zmq.NOBLOCK)
            except zmq.Again:
                pass


def _heartbeat_loop(
    sender: MetricSender,
    last_sent: list[float],
    get_drain_state: Callable[[], str],
) -> None:
    while True:
        time.sleep(1.0)
        if time.monotonic() - last_sent[0] >= HEARTBEAT_INTERVAL_S:
            last_sent[0] = time.monotonic()
            sender.send(_build_heartbeat_event(_get_server_mac(), get_drain_state))


def _build_heartbeat_event(server_id: str, get_drain_state: Callable[[], str]) -> dict:
    return {
        "event_type": "heartbeat",
        "server_id": server_id,
        "ts": time.time(),
        "cpu_percent": container_cpu_percent(),
        "ram_used_mb": container_ram_used_mb(),
        "state": get_drain_state(),
    }


def _bootstrap_heartbeat_loop(
    sender: MetricSender,
    last_sent: list[float],
    get_drain_state: Callable[[], str],
) -> None:
    global _sent_bootstrap_heartbeat

    while not _sent_bootstrap_heartbeat:
        server_id = _get_server_mac()
        if server_id != "unknown":
            sender.send(_build_heartbeat_event(server_id, get_drain_state))
            last_sent[0] = time.monotonic()
            _sent_bootstrap_heartbeat = True
            return
        time.sleep(_BOOTSTRAP_RETRY_S)


def _build_event(
    time_total_ms: float,
    time_db_ms: float,
    time_db_read_ms: float,
    time_db_write_ms: float,
    time_db_cmd_count: int,
    status_code: int,
    request_type: str,
    get_drain_state: Callable[[], str],
) -> dict:
    return {
        "server_id": _get_server_mac(),
        "ts": time.time(),
        "time_total_ms": time_total_ms,
        "time_db_ms": time_db_ms,
        "time_db_read_ms": time_db_read_ms,
        "time_db_write_ms": time_db_write_ms,
        "time_db_cmd_count": time_db_cmd_count,
        "status_code": status_code,
        "request_type": request_type,
        "cpu_percent": container_cpu_percent(),
        "ram_used_mb": container_ram_used_mb(),
        "state": get_drain_state(),
    }


def init_telemetry(
    app: Flask,
    sender: MetricSender | None = None,
    get_drain_state: Callable[[], str] | None = None,
) -> None:
    _sender = sender or ZmqMetricSender()
    _last_sent: list[float] = [time.monotonic()]
    _get_state = get_drain_state or (lambda: "active")

    if HEARTBEAT_ENABLED:
        threading.Thread(
            target=_heartbeat_loop,
            args=(_sender, _last_sent, _get_state),
            daemon=True,
        ).start()

    threading.Thread(
        target=_bootstrap_heartbeat_loop,
        args=(_sender, _last_sent, _get_state),
        daemon=True,
    ).start()

    @app.before_request
    def _start_timer() -> None:
        g.time_start = time.monotonic()
        g.time_db_elapsed = 0.0
        g.time_db_read_s = 0.0
        g.time_db_write_s = 0.0
        g.time_db_cmd_count = 0
        # Pre-initialised so handlers can append without nil-checks.
        g.request_lease_outcomes: list[dict] = []

    @app.after_request
    def _emit_metric(response):
        time_total = (time.monotonic() - g.time_start) * 1000
        event = _build_event(
            time_total_ms=time_total,
            time_db_ms=g.time_db_elapsed * 1000,
            time_db_read_ms=getattr(g, "time_db_read_s", 0.0) * 1000,
            time_db_write_ms=getattr(g, "time_db_write_s", 0.0) * 1000,
            time_db_cmd_count=getattr(g, "time_db_cmd_count", 0),
            status_code=response.status_code,
            request_type="write" if request.method in ("POST", "PUT", "PATCH", "DELETE") else "read",
            get_drain_state=_get_state,
        )
        # Per-LAN database time, accumulated by the data path per LAN.
        event["time_db_ms_per_lan"] = {
            lan: elapsed_s * 1000.0
            for lan, elapsed_s in getattr(g, "time_db_per_lan", {}).items()
        }
        event["request_lease_outcomes"] = getattr(g, "request_lease_outcomes", [])
        _sender.send(event)
        _last_sent[0] = time.monotonic()
        return response
