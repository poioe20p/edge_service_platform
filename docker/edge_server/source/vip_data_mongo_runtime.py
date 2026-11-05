from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, TypeVar

from flask import g, request
from pymongo import MongoClient
from pymongo.command_cursor import CommandCursor
from pymongo.cursor import Cursor
from pymongo.errors import AutoReconnect, PyMongoError
from werkzeug.exceptions import BadRequest

from edge_server_config import CONFIG, resolve_mongo_read_preference

_write_clients: dict[str, MongoClient] = {}
_write_clients_lock = threading.Lock()


def _get_write_client(lan: str) -> MongoClient:
    """Return a lazily-initialised direct-to-primary MongoClient for *lan*.

    The connection uses ``directConnection=True`` so it never performs
    replica-set discovery.  The URI comes from ``CONFIG.mongo_primary_lan*``
    which is seeded from environment variables that the controller sets when
    it spawns the edge-server container.

    Double-checked locking protects the lazy-init path from TOCTOU races
    when multiple Flask request threads first enter the write path
    simultaneously.
    """
    if lan in _write_clients:
        return _write_clients[lan]
    with _write_clients_lock:
        if lan in _write_clients:
            return _write_clients[lan]
        uri = CONFIG.mongo_primary_lan1 if lan == "lan1" else CONFIG.mongo_primary_lan2
        _write_clients[lan] = MongoClient(
            uri,
            directConnection=True,
            maxPoolSize=2,
            serverSelectionTimeoutMS=3000,
        )
        log = logging.getLogger("vip_data_mongo_runtime")
        log.info("write_client created for %s -> %s (pool=2)", lan, uri)
        return _write_clients[lan]

log = logging.getLogger(__name__)

DB_NAME = CONFIG.db_name
DB_PORT = CONFIG.db_port
MAX_IDLE_MS = CONFIG.max_idle_ms
MONGO_CLIENT_RETIRE_GRACE_S = CONFIG.mongo_client_retire_grace_s


class StorageVipConfigurationError(RuntimeError):
    """Raised when the fixed startup VIP configuration is incomplete."""


@dataclass
class _MongoEpoch:
    """Global Mongo client generation scoped to a LAN and VIP selection.

    This runtime keeps the epoch object, its client, and all transition state
    together because they are guarded by the same per-LAN lifecycle lock.
    """

    epoch_id: int
    mode: str
    vip_ip: str
    client: MongoClient | None = None
    client_created_at: float | None = None
    first_lease_at: float | None = None
    lease_count: int = 0
    retiring: bool = False
    retire_requested_at: float | None = None
    drain_deadline: float | None = None
    last_failure_at: float = 0.0
    recent_failures: int = 0  # Observability only: does not gate rotation


class RequestLeaseLifecycle(Enum):
    ACTIVE = auto()
    FAILED = auto()
    COMPLETED = auto()


@dataclass
class RequestLease:
    """Request-scoped binding between one Flask request and one Mongo epoch."""

    lan: str
    epoch: _MongoEpoch
    first_bound_at: float
    lifecycle: RequestLeaseLifecycle = RequestLeaseLifecycle.ACTIVE
    replay_safe: bool = True
    terminal_reason: str | None = None


@dataclass
class _LanEpochState:
    lifecycle_lock: threading.Lock = field(default_factory=threading.Lock)
    normal_vip_ip: str | None = None
    current: _MongoEpoch | None = None
    retiring: list[_MongoEpoch] = field(default_factory=list)
    next_epoch_id: int = 1


vip_data_per_domain = {
    "lan1": "10.0.0.254",
    "lan2": "10.0.1.254",
}

_epoch_states_registry_lock = threading.Lock()
_epoch_states: dict[str, _LanEpochState] = {}
_epoch_housekeeping_start_lock = threading.Lock()
_epoch_housekeeping_started = False

# Lock order: _pool_renew_lock may be held while taking state.lifecycle_lock,
# never the reverse; _get_lan_epoch_state is called before any lock.
_pool_renew_lock = threading.Lock()
_pool_renew_last_at: dict[str, float] = {}
_pool_renew_timers: dict[str, list[threading.Timer]] = {}


def _seed_epoch_states_from_config() -> None:
    configured_lans = set(vip_data_per_domain)

    seeded: dict[str, _LanEpochState] = {}
    for lan in sorted(configured_lans):
        seeded[lan] = _LanEpochState(
            normal_vip_ip=vip_data_per_domain[lan],
        )

    with _epoch_states_registry_lock:
        _epoch_states.clear()
        _epoch_states.update(seeded)


def _get_lan_epoch_state(lan: str) -> _LanEpochState:
    with _epoch_states_registry_lock:
        state = _epoch_states.get(lan)
    if state is None:
        raise StorageVipConfigurationError(f"unknown configured LAN: {lan}")
    return state


def _new_epoch_locked(
    state: _LanEpochState,
    mode: str,
    vip_ip: str,
) -> _MongoEpoch:
    epoch = _MongoEpoch(
        epoch_id=state.next_epoch_id,
        mode=mode,
        vip_ip=vip_ip,
    )
    state.next_epoch_id += 1
    return epoch


def _mark_epoch_retiring_locked(state: _LanEpochState, epoch: _MongoEpoch) -> None:
    now = time.monotonic()
    epoch.retiring = True
    epoch.retire_requested_at = now
    epoch.drain_deadline = now + MONGO_CLIENT_RETIRE_GRACE_S
    state.retiring.append(epoch)


def _get_or_create_epoch_client(lan: str, epoch: _MongoEpoch) -> MongoClient:
    state = _get_lan_epoch_state(lan)
    with state.lifecycle_lock:
        if epoch.client is not None:
            return epoch.client

        url = f"mongodb://{epoch.vip_ip}:{DB_PORT}/"
        epoch.client = MongoClient(
            url,
            maxPoolSize=CONFIG.mongo_max_pool_size,
            maxIdleTimeMS=MAX_IDLE_MS,
            serverSelectionTimeoutMS=CONFIG.mongo_server_selection_timeout_ms,
            socketTimeoutMS=15000,
            directConnection=True,
            readPreference=resolve_mongo_read_preference(),
        )
        epoch.client_created_at = time.monotonic()
        log.info(
            "Created MongoClient for %s epoch=%s mode=%s via %s "
            "(maxIdleTimeMS=%d readPref=%s)",
            lan,
            epoch.epoch_id,
            epoch.mode,
            url,
            MAX_IDLE_MS,
            CONFIG.mongo_read_preference,
        )
        return epoch.client


def _release_epoch(lan: str, epoch: _MongoEpoch) -> None:
    state = _get_lan_epoch_state(lan)
    with state.lifecycle_lock:
        if epoch.lease_count > 0:
            epoch.lease_count -= 1


def _get_request_lease_registry() -> dict[str, RequestLease]:
    registry = getattr(g, "db_request_leases", None)
    if registry is None:
        registry = {}
        g.db_request_leases = registry
    return registry


def _bind_new_request_lease(lan: str) -> RequestLease:
    registry = _get_request_lease_registry()
    state = _get_lan_epoch_state(lan)

    with state.lifecycle_lock:
        current = state.current
        if current is None:
            vip_ip = state.normal_vip_ip
            if vip_ip is None:
                raise StorageVipConfigurationError(f"missing normal VIP mapping for {lan}")
            current = _new_epoch_locked(
                state,
                mode="normal",
                vip_ip=vip_ip,
            )
            state.current = current

        now = time.monotonic()
        current.lease_count += 1
        if current.first_lease_at is None:
            current.first_lease_at = now

        lease = RequestLease(
            lan=lan,
            epoch=current,
            first_bound_at=now,
        )
        registry[lan] = lease
        return lease


def _get_or_bind_request_lease(lan: str) -> RequestLease:
    registry = _get_request_lease_registry()
    lease = registry.get(lan)
    if lease is not None:
        return lease
    return _bind_new_request_lease(lan)


def _rotate_current_epoch_locked(
    lan: str,
    state: _LanEpochState,
    reason: str,
    next_mode: str,
    next_vip_ip: str,
) -> _MongoEpoch:
    current = state.current
    if current is not None:
        _mark_epoch_retiring_locked(state, current)

    state.current = _new_epoch_locked(
        state,
        mode=next_mode,
        vip_ip=next_vip_ip,
    )
    log.info(
        "Replaced current epoch for %s via %s: %s -> %s (mode=%s vip=%s)",
        lan,
        reason,
        getattr(current, "epoch_id", None),
        state.current.epoch_id,
        state.current.mode,
        state.current.vip_ip,
    )
    return state.current


def _rotate_renewed_epoch(lan: str, reason: str, wave: int) -> dict:
    state = _get_lan_epoch_state(lan)
    with state.lifecycle_lock:
        if state.normal_vip_ip is None:
            raise StorageVipConfigurationError(f"missing normal VIP mapping for {lan}")
        new_epoch = _rotate_current_epoch_locked(
            lan, state, reason=f"pool_renew:{reason}:w{wave}",
            next_mode="normal", next_vip_ip=state.normal_vip_ip)
        epoch_id = new_epoch.epoch_id
    log.info("pool_renew: lan=%s reason=%s wave=%d new_epoch=%s", lan, reason, wave, epoch_id)
    return {"rotated": True, "reason": reason, "lane": lan, "wave": wave, "epoch_id": epoch_id}


def renew_current_epoch(lan: str, reason: str) -> dict:
    """Wave A now + wave B after the warm-lease TTL (admission-time renewal)."""
    if not CONFIG.pool_renew_enabled:
        return {"rotated": False, "reason": "disabled", "lane": lan, "wave": 0, "epoch_id": None}
    state = _get_lan_epoch_state(lan)
    superseded = 0
    with _pool_renew_lock:                      # atomic cancel + debounce + store
        now = time.monotonic()
        if now - _pool_renew_last_at.get(lan, 0.0) < CONFIG.pool_renew_min_interval_s:
            with state.lifecycle_lock:
                current_id = state.current.epoch_id if state.current else None
            return {"rotated": False, "reason": "debounced", "lane": lan, "wave": 0,
                    "epoch_id": current_id}
        _pool_renew_last_at[lan] = now
        superseded = len(_pool_renew_timers.get(lan, []))
        for t in _pool_renew_timers.pop(lan, []):
            t.cancel()
        if superseded:
            log.info("pool_renew: lan=%s superseded=%d pending wave(s)", lan, superseded)
        result = _rotate_renewed_epoch(lan, reason, wave=1)
        timers: list[threading.Timer] = []
        for wave in range(2, CONFIG.pool_renew_waves + 1):
            t = threading.Timer(
                CONFIG.pool_renew_wave_gap_s * (wave - 1),
                _rotate_renewed_epoch, args=(lan, reason, wave))
            t.daemon = True
            timers.append(t)
        _pool_renew_timers[lan] = timers
        for t in timers:
            t.start()
    return result


def _close_drained_epochs() -> None:
    ready: list[tuple[str, int, MongoClient]] = []
    overdue: list[tuple[str, int, int]] = []
    now = time.monotonic()

    with _epoch_states_registry_lock:
        items = list(_epoch_states.items())

    for lan, state in items:
        with state.lifecycle_lock:
            still_retiring: list[_MongoEpoch] = []
            for epoch in state.retiring:
                deadline_passed = (
                    epoch.drain_deadline is not None and now >= epoch.drain_deadline
                )
                if epoch.lease_count == 0:
                    if epoch.client is not None:
                        ready.append((lan, epoch.epoch_id, epoch.client))
                    continue
                if deadline_passed:
                    overdue.append((lan, epoch.epoch_id, epoch.lease_count))
                still_retiring.append(epoch)
            state.retiring = still_retiring

    for lan, epoch_id, lease_count in overdue:
        log.warning(
            "retiring epoch %s for %s still has %s active leases after drain deadline",
            epoch_id,
            lan,
            lease_count,
        )

    for lan, epoch_id, client in ready:
        try:
            client.close()
        except Exception as exc:  # pragma: no cover - defensive close path
            log.warning("drained epoch close failed for %s epoch=%s: %s", lan, epoch_id, exc)
        else:
            log.info("retiring epoch %s for %s closed (leases=0)", epoch_id, lan)


def _epoch_housekeeping_loop() -> None:
    sweep_interval = max(1.0, min(MONGO_CLIENT_RETIRE_GRACE_S / 2.0, 5.0))
    while True:
        time.sleep(sweep_interval)
        try:
            _close_drained_epochs()
        except Exception:
            log.exception("epoch housekeeping sweep failed")


def start_epoch_housekeeping() -> None:
    global _epoch_housekeeping_started

    # Must be single-start: a second thread would run duplicate close/rollback
    # sweeps against the same epoch registry.
    with _epoch_housekeeping_start_lock:
        if _epoch_housekeeping_started:
            return
        threading.Thread(
            target=_epoch_housekeeping_loop,
            daemon=True,
            name="mongo-epoch-housekeeping",
        ).start()
        _epoch_housekeeping_started = True


def _snapshot_epoch(epoch: _MongoEpoch | None) -> dict[str, int | str | bool | None]:
    if epoch is None:
        return {
            "epoch_id": None,
            "mode": "unknown",
            "vip_ip": None,
            "retiring": None,
        }
    return {
        "epoch_id": epoch.epoch_id,
        "mode": epoch.mode,
        "vip_ip": epoch.vip_ip,
        "retiring": epoch.retiring,
    }


def _get_current_epoch_snapshot(lan: str) -> dict[str, int | str | bool | None]:
    with _epoch_states_registry_lock:
        state = _epoch_states.get(lan)
    if state is None:
        return _snapshot_epoch(None)

    with state.lifecycle_lock:
        return _snapshot_epoch(state.current)


def snapshot_normal_vip_config() -> dict[str, str]:
    snapshot: dict[str, str] = {}

    with _epoch_states_registry_lock:
        items = list(_epoch_states.items())

    for lan, state in items:
        with state.lifecycle_lock:
            if state.normal_vip_ip is not None:
                snapshot[lan] = state.normal_vip_ip

    return snapshot


def _parse_vip_update_payload(body: object) -> dict[str, str]:
    if not isinstance(body, dict):
        raise BadRequest("vip_data payload must be a JSON object")

    normalized: dict[str, str] = {}
    for lan, raw_vip_ip in body.items():
        if not isinstance(lan, str) or not lan:
            raise BadRequest("vip_data payload contains an invalid LAN key")
        if not isinstance(raw_vip_ip, str):
            raise BadRequest(f"vip_data value for {lan} must be a string")

        vip_ip = raw_vip_ip.strip()
        if not vip_ip:
            raise BadRequest(f"vip_data value for {lan} must be a non-empty string")

        normalized[lan] = vip_ip

    return normalized


def prepare_vip_update_payload(body: object) -> dict[str, str]:
    return _parse_vip_update_payload(body)


def find_unknown_vip_update_lans(payload: dict[str, str]) -> list[str]:
    with _epoch_states_registry_lock:
        return sorted(lan for lan in payload if lan not in _epoch_states)


def apply_vip_update(payload: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    changed_lans: list[str] = []
    for lan, vip_ip in payload.items():
        state = _get_lan_epoch_state(lan)
        with state.lifecycle_lock:
            if state.normal_vip_ip == vip_ip:
                continue
            state.normal_vip_ip = vip_ip
            _rotate_current_epoch_locked(
                lan,
                state,
                reason="vip_update",
                next_mode="normal",
                next_vip_ip=vip_ip,
            )
            changed_lans.append(lan)

    return snapshot_normal_vip_config(), changed_lans


def _accumulate_tdados(lan: str, elapsed: float) -> None:
    g.time_db_elapsed = getattr(g, "time_db_elapsed", 0.0) + elapsed
    per_lan = getattr(g, "time_db_per_lan", None)
    if per_lan is None:
        per_lan = {}
        g.time_db_per_lan = per_lan
    per_lan[lan] = per_lan.get(lan, 0.0) + elapsed


def log_db_failure(route_name: str, exc: Exception, lan: str | None = None) -> None:
    failure_lan = lan or getattr(g, "db_last_lan", None) or "unknown"
    request_epoch = getattr(g, "db_epoch_context", None) or _snapshot_epoch(None)
    current_epoch = (
        _get_current_epoch_snapshot(failure_lan)
        if failure_lan != "unknown"
        else _snapshot_epoch(None)
    )
    log.error(
        "db_failure route=%s request_id=%s method=%s path=%s lan=%s "
        "request_epoch_id=%s request_epoch_mode=%s request_epoch_vip=%s request_epoch_retiring=%s "
        "current_epoch_id=%s current_epoch_mode=%s current_epoch_vip=%s current_epoch_retiring=%s "
        "exc_type=%s exc=%s last_cmd=%s last_cmd_db=%s last_cmd_target=%s "
        "last_cmd_failed=%s last_cmd_s=%s tdados_s=%.6f tdb_read_s=%.6f "
        "tdb_write_s=%.6f tdb_cmd_count=%d",
        route_name,
        getattr(g, "request_id", "unknown"),
        request.method,
        request.path,
        failure_lan,
        request_epoch["epoch_id"],
        request_epoch["mode"],
        request_epoch["vip_ip"],
        request_epoch["retiring"],
        current_epoch["epoch_id"],
        current_epoch["mode"],
        current_epoch["vip_ip"],
        current_epoch["retiring"],
        type(exc).__name__,
        exc,
        getattr(g, "db_last_command", None),
        getattr(g, "db_last_command_db", None),
        getattr(g, "db_last_command_target", None),
        getattr(g, "db_last_command_failed", None),
        getattr(g, "db_last_command_duration_s", None),
        getattr(g, "time_db_elapsed", 0.0),
        getattr(g, "time_db_read_s", 0.0),
        getattr(g, "time_db_write_s", 0.0),
        getattr(g, "time_db_cmd_count", 0),
    )


def _project_request_lease_outcome(lease: RequestLease) -> dict[str, Any]:
    projected_lifecycle = (
        RequestLeaseLifecycle.FAILED
        if lease.lifecycle == RequestLeaseLifecycle.FAILED
        else RequestLeaseLifecycle.COMPLETED
    )

    if projected_lifecycle == RequestLeaseLifecycle.FAILED:
        outcome = "failure_terminal"
    else:
        outcome = "success_normal"

    return {
        "lan": lease.lan,
        "epoch_id": lease.epoch.epoch_id,
        "epoch_mode": lease.epoch.mode,
        "lifecycle": projected_lifecycle.name,
        "outcome": outcome,
        "replay_safe": lease.replay_safe,
        "terminal_reason": lease.terminal_reason,
    }


def collect_request_lease_outcomes() -> list[dict[str, Any]]:
    registry = getattr(g, "db_request_leases", None) or {}
    return [
        _project_request_lease_outcome(lease)
        for _lan, lease in sorted(registry.items())
    ]


def log_request_lease_outcome(entry: dict[str, Any]) -> None:
    log.info(
        "request lease outcome request_id=%s lan=%s lifecycle=%s outcome=%s "
        "epoch_id=%s epoch_mode=%s replay_safe=%s "
        "terminal_reason=%s",
        getattr(g, "request_id", "unknown"),
        entry["lan"],
        entry["lifecycle"],
        entry["outcome"],
        entry["epoch_id"],
        entry["epoch_mode"],
        entry["replay_safe"],
        entry["terminal_reason"],
    )


def release_request_leases() -> None:
    registry = getattr(g, "db_request_leases", None)
    if not registry:
        return

    for lease in registry.values():
        if lease.lifecycle != RequestLeaseLifecycle.FAILED:
            lease.lifecycle = RequestLeaseLifecycle.COMPLETED
        _release_epoch(lease.lan, lease.epoch)
    registry.clear()


T = TypeVar("T")


def _ensure_materialized_result(op_name: str, result: T) -> T:
    if isinstance(result, (Cursor, CommandCursor)):
        raise TypeError(
            f"{op_name} returned a live cursor; materialize it inside run_with_request_lease(...)"
        )
    return result


def _run_db_op_once(
    lan: str,
    lease: RequestLease,
    op_name: str,
    fn: Callable[[Any], T],
) -> T:
    g.db_last_lan = lan
    t0 = time.monotonic()
    epoch = lease.epoch
    try:
        g.db_epoch_context = _snapshot_epoch(epoch)
        g.db_used_epoch_client = False
        client = _get_or_create_epoch_client(lan, epoch)
        result = fn(client[DB_NAME])
        result = _ensure_materialized_result(op_name, result)
        epoch.recent_failures = 0
        return result
    except AutoReconnect:
        epoch.last_failure_at = time.monotonic()
        epoch.recent_failures += 1
        raise
    finally:
        _accumulate_tdados(lan, time.monotonic() - t0)


def run_with_request_lease(
    lan: str,
    *,
    op_name: str,
    replay_safe: bool,
    fn: Callable[[Any], T],
) -> T:
    registry = _get_request_lease_registry()
    lease = registry.get(lan)
    if lease is not None and lease.lifecycle == RequestLeaseLifecycle.FAILED:
        raise PyMongoError(f"request lease already failed for {lan}")
    if lease is not None and lease.lifecycle == RequestLeaseLifecycle.COMPLETED:
        raise PyMongoError(f"request lease already completed for {lan}")

    lease = lease or _get_or_bind_request_lease(lan)
    lease.replay_safe = lease.replay_safe and replay_safe

    attempts = 0
    while True:
        attempts += 1
        try:
            return _run_db_op_once(lan, lease, op_name, fn)
        except AutoReconnect:
            # _run_db_op_once already incremented lease.epoch.recent_failures
            if attempts <= CONFIG.mongo_retry_max_attempts:
                backoff_ms = CONFIG.mongo_retry_backoff_ms * (2 ** (attempts - 1))
                time.sleep(backoff_ms / 1000.0)
                continue

            lease.lifecycle = RequestLeaseLifecycle.FAILED
            lease.terminal_reason = f"{op_name}:retries_exhausted"
            raise


_seed_epoch_states_from_config()