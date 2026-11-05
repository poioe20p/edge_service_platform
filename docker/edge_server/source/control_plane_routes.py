from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from vip_data_mongo_runtime import (
    apply_vip_update,
    find_unknown_vip_update_lans,
    prepare_vip_update_payload,
    renew_current_epoch,
    snapshot_normal_vip_config,
)

log = logging.getLogger(__name__)


def register_control_plane_routes(app: Flask, process_state) -> None:
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

    @app.route("/ready", methods=["GET"])
    def ready():
        """RQ3 application-readiness probe: 200 iff the app is ready to serve.

        ``app_ready`` is set True only after a real MongoDB round-trip
        succeeds (see app.py's readiness probe). Returns 503 before that.
        """
        if process_state.app_ready:
            return jsonify({"status": "ready"}), 200
        return jsonify({"status": "starting"}), 503

    @app.route("/drain", methods=["POST"])
    def drain():
        """Change the local drain state.

        Supported commands:
          - start  (default when omitted)
          - cancel

        start moves the server into quiesce mode without rejecting workload
        requests. Repeated start refreshes the quiet-period timer.
        """

        body = request.get_json(silent=True) or {}
        command = body.get("command", "start")

        if command not in ("start", "cancel"):
            return jsonify({"error": "invalid command"}), 400

        if command == "cancel":
            remaining = process_state.cancel_drain()
            log.info(
                "Drain canceled — server returned to active state with %d in-flight",
                remaining,
            )
            return jsonify({"state": "active", "active_requests": remaining}), 200

        remaining = process_state.activate_drain()
        process_state.ensure_drain_monitor()
        log.info("Drain activated — quiescing with %d in-flight", remaining)
        return jsonify({"state": "draining", "active_requests": remaining}), 200

    @app.route("/vip_data", methods=["PUT"])
    def set_vip_data():
        payload = prepare_vip_update_payload(request.get_json(silent=True))
        unknown_lans = find_unknown_vip_update_lans(payload)
        if unknown_lans:
            return jsonify(
                {
                    "error": "unknown LANs in vip_data update",
                    "unknown_lans": unknown_lans,
                }
            ), 400

        vip_data, changed_lans = apply_vip_update(payload)
        return jsonify(
            {
                "message": "VIP data updated",
                "vip_data": vip_data,
                "changed_lans": changed_lans,
            }
        ), 200

    @app.route("/pool_renew", methods=["POST"])
    def pool_renew():
        """Renew the read epoch for this edge's own LAN (RQ2).

        Called by the controller after a storage admission. Lane mismatch
        (another LAN's request), unknown LAN, and malformed bodies are all
        rejected without touching any epoch state.
        """
        body = request.get_json(silent=True) or {}
        lan = body.get("lan")
        known = set(snapshot_normal_vip_config().keys())
        if not isinstance(lan, str) or lan not in known or lan != process_state.config.lan_id:
            return jsonify({"error": "unknown or mismatched lan", "lan": lan}), 400
        result = renew_current_epoch(lan, reason=str(body.get("reason", "unspecified")))
        return jsonify(result), 200