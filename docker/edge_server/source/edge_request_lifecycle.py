from __future__ import annotations

import logging
import time
from uuid import uuid4

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import BadRequest

from vip_data_mongo_runtime import (
    StorageVipConfigurationError,
    collect_request_lease_outcomes,
    log_db_failure,
    log_request_lease_outcome,
    release_request_leases,
)

log = logging.getLogger(__name__)


def stage_local_request_event(
    *,
    request_kind: str,
    content_id: str | None,
    user_id: str,
    relevance: str,
    status: str,
    tags: tuple[str, ...] | list[str],
) -> None:
    g.local_request_event = {
        "request_kind": request_kind,
        "content_id": content_id,
        "user_id": user_id,
        "relevance": relevance,
        "status": status,
        "tags": tuple(tags),
    }


def _current_request_latency_ms() -> float:
    started_at = getattr(g, "time_start", None)
    if started_at is None:
        return round(getattr(g, "time_db_elapsed", 0.0) * 1000, 2)
    return round((time.monotonic() - started_at) * 1000, 2)


def register_pre_telemetry_request_hooks(app: Flask, config, process_state) -> None:
    @app.before_request
    def _drain_guard():
        g.request_id = uuid4().hex[:12]
        g.db_last_lan = None
        g.db_epoch_context = None
        g.db_last_command = None
        g.db_last_command_db = None
        g.db_last_command_target = None
        g.db_last_command_failed = None
        g.db_last_command_duration_s = None
        g.db_used_epoch_client = False
        g.local_request_event = None
        # Request-scoped: _drain_counter reads g.counted to mirror the increment.
        g.counted = process_state.begin_counted_request(path=request.path)
        return None

    @app.after_request
    def _drain_counter(response):
        process_state.end_counted_request(counted=getattr(g, "counted", False))
        return response

    @app.teardown_request
    def _release_request_leases(_exc: BaseException | None) -> None:
        release_request_leases()

    @app.errorhandler(BadRequest)
    def _handle_bad_request(exc):
        return jsonify({"error": exc.description}), 400

    @app.errorhandler(StorageVipConfigurationError)
    def _handle_storage_vip_configuration_error(exc):
        route_name = request.endpoint or request.path or "unknown"
        log_db_failure(route_name, exc, lan=getattr(g, "db_last_lan", None))
        return jsonify({"error": "storage VIP configuration error"}), 500

    @app.after_request
    def _check_tdados_threshold(response):
        per_lan = getattr(g, "time_db_per_lan", None)
        if not per_lan:
            return response
        for lan, elapsed in per_lan.items():
            time_ms = elapsed * 1000
            if time_ms > config.tau_dados_ms:
                log.debug(
                    "T_dados[%s]=%.1fms > tau=%.1fms -- observed only, no forced reconnection",
                    lan,
                    time_ms,
                    config.tau_dados_ms,
                )
        return response


def register_post_telemetry_request_hooks(app: Flask, process_state) -> None:
    # Flask runs after_request hooks in reverse registration order, so these run
    # before telemetry emission — final lease data and the event are recorded.
    @app.after_request
    def _record_local_request_activity(response):
        staged = getattr(g, "local_request_event", None)
        if not staged or response.status_code >= 400:
            return response

        process_state.local_request_state.record(
            process_state.build_local_request_event(
                timestamp_epoch=time.time(),
                request_kind=staged["request_kind"],
                content_id=staged["content_id"],
                user_id=staged["user_id"],
                latency_ms=_current_request_latency_ms(),
                relevance=staged["relevance"],
                status=staged["status"],
                tags=staged["tags"],
            )
        )
        return response

    @app.after_request
    def _finalize_request_lease_outcomes(response):
        outcomes = collect_request_lease_outcomes()
        g.request_lease_outcomes = outcomes
        for entry in outcomes:
            log_request_lease_outcome(entry)
        return response