"""Pydantic models for inter-component telemetry messages.

All per-node dicts (``servers``, ``storage_servers``) are keyed by the
node's **MAC address** (e.g. ``"00:00:00:00:00:02"``).  Each container
discovers its own MAC from ``/sys/class/net/eth0/address`` and includes it
in every telemetry event it emits; the aggregator preserves the key as-is.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ServerSummary(BaseModel):
    avg_time_total_ms: float
    avg_time_db_ms: float
    avg_time_proc_ms: float
    request_count: int
    error_rate: float
    avg_cpu_percent: float
    avg_ram_used_mb: float
    last_report_ts: float = 0.0
    # Defaulted for backward compatibility with pre-decomposition aggregators.
    avg_time_db_read_ms: float = 0.0
    avg_time_db_write_ms: float = 0.0
    avg_time_db_cmd_count: float = 0.0
    state: Literal["active", "draining"] = "active"
    # True for liveness-only heartbeat entries (zeroed latency fields); routing
    # consumers refresh only the freshness stamp / drain state from these.
    heartbeat_only: bool = False


class StorageServerSummary(BaseModel):
    avg_repl_lag_s: float | None = None
    avg_connections: float = 0.0
    avg_cpu_percent: float = 0.0
    avg_ram_used_mb: float = 0.0
    sample_count: int = 0
    last_report_ts: float = 0.0
    member_state: str | None = None


class DomainSummary(BaseModel):
    total_requests: int
    avg_time_proc_ms: float
    avg_time_db_ms: float
    # Optional so pre-median aggregators still validate; the signal functions
    # fall back to the mean when absent (LATENCY_SIGNAL_MODE=median uses these).
    median_time_proc_ms: float | None = None
    median_time_db_ms: float | None = None
    p95_time_proc_ms: float = 0.0
    p95_time_db_ms: float = 0.0
    average_cpu_percent: float
    peak_time_total_ms: float
    avg_storage_cpu_percent: float = 0.0  # domain average across all storage servers
    avg_time_db_read_ms: float = 0.0
    avg_time_db_write_ms: float = 0.0
    avg_time_db_cmd_count: float = 0.0
    # Keys: "lan1", "lan2"; values {outcome: count} with outcome one of
    # "success_normal", "success_after_rebind", "failure_terminal".
    request_lease_outcomes_per_lan: dict[str, dict[str, int]] = {}


class TelemetrySummary(BaseModel):
    network_id: str
    window_end: float
    # Window identity: monotonic per-network sequence.  None for control-event
    # mini-summaries (drain_complete, rs_secondary_ready).
    window_seq: int | None = None
    window_id: str | None = None      # f"{network_id}:{window_seq}"
    overload: bool = False            # producer-side label
    # False when the window carried no HTTP request samples; controller-side
    # consumers must not treat such windows as lull evidence.
    request_evidence: bool = True
    servers: dict[str, ServerSummary]
    storage_servers: dict[str, StorageServerSummary] = {}
    domain_summary: DomainSummary | None = None  # absent in mini-summaries (drain_complete)
    control_events: list[dict] = []  # drain_complete and other control-plane events
