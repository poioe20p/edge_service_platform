from __future__ import annotations

import os
from dataclasses import dataclass

from compute import TREND_WINDOW_SIZE

# VIP flow DNAT can land reads on a SECONDARY, where primary-pref is rejected
# (NotPrimaryOrSecondary 13436); entries must be mode strings, not _ServerMode.
_READ_PREF_MAP = {
    "primary": "primary",
    "secondarypreferred": "secondaryPreferred",
    "secondary": "secondary",
}

# ~1.04 us/round measured => ~26 ms/request at this default. The shells and the
# controller spawner forward HASH_WORK_FACTOR only when it is explicitly set.
HASH_WORK_FACTOR_DEFAULT = 25000


def resolve_mongo_read_preference() -> str:
    """Map the EDGE_MONGO_READ_PREFERENCE string (lowercased, underscores
    stripped) to a pymongo read-preference mode string. Unknown values fall
    back to "secondaryPreferred" (the default read path); set
    EDGE_MONGO_READ_PREFERENCE=primary to pin reads to the primary. Kept in
    the config module so both the VIP epoch client and the data-path
    clients can import it without a circular dependency."""
    key = (os.environ.get("EDGE_MONGO_READ_PREFERENCE", "secondaryPreferred").strip()
           .lower().replace("_", ""))
    return _READ_PREF_MAP.get(key, "secondaryPreferred")


@dataclass(frozen=True)
class EdgeServerConfig:
    bind_host: str
    bind_port: int
    db_name: str
    lan_id: str
    db_port: int
    max_idle_ms: int
    tau_dados_ms: float
    drain_poll_interval_s: float
    drain_quiet_period_s: float
    service_pressure_default_window_min: float
    local_request_buffer_target_rps: float
    local_request_buffer_max_events: int
    local_request_per_content_window: int
    service_pressure_default_limit: int
    mongo_client_retire_grace_s: float
    feed_candidate_limit: int
    feed_integrity_work_factor: int
    hash_work_factor: int
    mongo_retry_backoff_ms: int
    mongo_retry_max_attempts: int
    mongo_server_selection_timeout_ms: int
    mongo_read_preference: str
    mongo_max_pool_size: int
    mongo_primary_lan1: str
    mongo_primary_lan2: str
    # Edge half of a double gate: the controller POSTs /pool_renew after a storage
    # admission only when its STORAGE_POOL_RENEW_ENABLED is on; default 0 = off.
    pool_renew_enabled: bool
    pool_renew_min_interval_s: float
    pool_renew_waves: int
    pool_renew_wave_gap_s: float

    @classmethod
    def from_env(cls) -> "EdgeServerConfig":
        service_pressure_default_window_min = float(
            os.environ.get("SERVICE_PRESSURE_DEFAULT_WINDOW_MIN", "10")
        )
        local_request_buffer_target_rps = float(
            os.environ.get("LOCAL_REQUEST_BUFFER_TARGET_RPS", "120")
        )
        return cls(
            bind_host=os.environ.get("BIND_HOST", "0.0.0.0"),
            bind_port=int(os.environ.get("BIND_PORT", "5000")),
            db_name=os.environ.get("DB_NAME", "edge_platform"),
            lan_id=os.environ.get("LAN_ID", "lan1"),
            db_port=int(os.environ.get("DB_PORT", "27018")),
            max_idle_ms=int(
                os.environ.get(
                    "MAX_IDLE_MS",
                    str(int(os.environ.get("VIP_IDLE_TIMEOUT", "30")) * 1000),
                )
            ),
            tau_dados_ms=float(os.environ.get("TAU_DADOS_MS", "65")),
            drain_poll_interval_s=float(os.environ.get("DRAIN_POLL_INTERVAL_S", "0.5")),
            drain_quiet_period_s=float(os.environ.get("DRAIN_QUIET_PERIOD_S", "1.0")),
            service_pressure_default_window_min=service_pressure_default_window_min,
            local_request_buffer_target_rps=local_request_buffer_target_rps,
            local_request_buffer_max_events=int(
                os.environ.get(
                    "LOCAL_REQUEST_BUFFER_MAX_EVENTS",
                    str(
                        max(
                            5000,
                            int(
                                service_pressure_default_window_min
                                * 60
                                * local_request_buffer_target_rps
                            ),
                        )
                    ),
                )
            ),
            local_request_per_content_window=int(
                os.environ.get(
                    "LOCAL_REQUEST_PER_CONTENT_WINDOW",
                    str(max(TREND_WINDOW_SIZE * 4, 50)),
                )
            ),
            service_pressure_default_limit=int(
                os.environ.get("SERVICE_PRESSURE_DEFAULT_LIMIT", "10")
            ),
            mongo_client_retire_grace_s=float(
                os.environ.get("MONGO_CLIENT_RETIRE_GRACE_S", "30")
            ),
            feed_candidate_limit=int(
                os.environ.get("FEED_CANDIDATE_LIMIT", "500")
            ),
            feed_integrity_work_factor=int(
                os.environ.get("FEED_INTEGRITY_WORK_FACTOR", "200")
            ),
            hash_work_factor=int(
                os.environ.get("HASH_WORK_FACTOR", str(HASH_WORK_FACTOR_DEFAULT))
            ),
            mongo_retry_backoff_ms=int(
                os.environ.get("MONGO_RETRY_BACKOFF_MS", "100")
            ),
            mongo_retry_max_attempts=int(
                os.environ.get("MONGO_RETRY_MAX_ATTEMPTS", "3")
            ),
            mongo_server_selection_timeout_ms=int(
                os.environ.get("MONGO_SERVER_SELECTION_TIMEOUT_MS", "3000")
            ),
            mongo_read_preference=os.environ.get(
                "EDGE_MONGO_READ_PREFERENCE", "secondaryPreferred"
            ),
            # 1 = one connection pinned to one storage backend; >1 fans out across backends
            # only when the controller runs with VIP_DATA_PER_CONNECTION_FLOWS=1.
            mongo_max_pool_size=int(
                os.environ.get("EDGE_MONGO_MAX_POOL_SIZE", "1")
            ),
            mongo_primary_lan1=os.environ.get(
                "EDGE_MONGO_PRIMARY_LAN1", "mongodb://10.0.0.4:27018/"
            ),
            mongo_primary_lan2=os.environ.get(
                "EDGE_MONGO_PRIMARY_LAN2", "mongodb://10.0.1.4:27018/"
            ),
            pool_renew_enabled=(
                os.environ.get("EDGE_POOL_RENEW_ENABLED", "0").strip() == "1"
            ),
            pool_renew_min_interval_s=float(
                os.environ.get("EDGE_POOL_RENEW_MIN_INTERVAL_S", "15")
            ),
            pool_renew_waves=int(os.environ.get("EDGE_POOL_RENEW_WAVES", "2")),
            pool_renew_wave_gap_s=float(
                os.environ.get("EDGE_POOL_RENEW_WAVE_GAP_S", "45")
            ),
        )


CONFIG = EdgeServerConfig.from_env()