"""Flask admin endpoint for live reconfiguration of the forwarder worker set.

State is injected via ``init_state`` from the supervisor so that routes can
access the shared worker registry without the module owning lifecycle.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from flask import Flask, jsonify, request
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from config import ADMIN_PORT, LOCAL_PORT
from forwarder import ForwarderWorker
from telemetry import emit_control_event, get_server_mac

logger = logging.getLogger("selective_sync.admin")

admin = Flask(__name__)

_workers: dict[str, ForwarderWorker] = {}
_workers_lock: threading.Lock = threading.Lock()
_local_db = None  # type: ignore[assignment]
_remote_db = None  # type: ignore[assignment]
_max_ttl_s: int = 0
_worker_factory: Callable[..., ForwarderWorker] = ForwarderWorker


def init_state(*, workers: dict[str, ForwarderWorker],
               workers_lock: threading.Lock,
               local_db, remote_db, max_ttl_s: int,
               worker_factory: Callable[..., ForwarderWorker] = ForwarderWorker) -> None:
    """Bind shared state owned by the supervisor to this module's routes."""
    global _workers, _workers_lock, _local_db, _remote_db, _max_ttl_s, _worker_factory
    _workers = workers
    _workers_lock = workers_lock
    _local_db = local_db
    _remote_db = remote_db
    _max_ttl_s = max_ttl_s
    _worker_factory = worker_factory


@admin.route("/forwarder_config", methods=["POST"])
def forwarder_config():
    """Reconcile the running worker set against the requested config.

    Three cases per collection, all handled under ``_workers_lock`` so
    the worker map stays consistent with concurrent admin requests:
      * collection in body but not in ``_workers``  -> spawn a new worker
        (new thread, new Change Stream) and seed it;
      * collection in ``_workers`` but not in body  -> stop the worker,
        persist its final token, drop the local collection;
      * collection in both with a changed id set -> keep the worker,
        ``reconfigure()`` it in place so its stream reopens from the
        saved token with the new ``$match``, seeding only the added IDs.
    """
    if _remote_db is None:
        return jsonify({"error": "supervisor has no remote DB configured"}), 503

    body: Any = request.get_json(force=True, silent=True) or {}
    incoming = body.get("collections") or {}
    if not isinstance(incoming, dict):
        return jsonify({"error": "collections must be an object"}), 400

    with _workers_lock:
        for coll, ids in incoming.items():
            if not isinstance(coll, str) or not isinstance(ids, list):
                continue
            ids_t = tuple(ids)
            worker = _workers.get(coll)
            if worker is None:
                w = _worker_factory(_local_db, _remote_db, coll, ids_t, _max_ttl_s)
                w.start()
                _workers[coll] = w
            elif worker.hot_ids != ids_t:
                worker.reconfigure(ids_t)
        for coll in list(_workers):
            if coll not in incoming:
                w = _workers.pop(coll)
                w.stop()
                _local_db[coll].drop()

    return jsonify({c: len(w.hot_ids) for c, w in _workers.items()})


@admin.route("/healthz", methods=["GET"])
def healthz():
    with _workers_lock:
        return jsonify({
            "workers": {c: len(w.hot_ids) for c, w in _workers.items()},
            "ok": True,
        })


@admin.route("/drain", methods=["POST"])
def drain():
    """Graceful shutdown for Tier 1 two-phase teardown.

    Returns ``202 Accepted`` immediately; the real work runs in a background
    daemon thread so the controller's short HTTP timeout (~2 s) is never a
    factor. Ordering inside the worker thread (critical for the clean-shutdown
    guarantee — every collection's resume-token file must reflect the last
    applied change):

      1. Under ``_workers_lock``, call ``worker.stop()`` on every
         :class:`ForwarderWorker`. Each worker closes its Change Stream,
         applies any in-flight event, and persists its final resume token.
      2. Emit a ``drain_complete`` control event over the telemetry PUSH
         socket. The SDN controller's
         :class:`ControlEventDispatcher.process_drain_events` sees this and
         submits the Phase B :class:`CleanupSelectiveAlert`.
      3. Run ``db.adminCommand({shutdown: 1})`` against ``localhost``.
         ``mongod`` exits; the entrypoint's ``wait $MONGOD_PID`` returns
         and the container terminates.

    Body: ignored (optional ``{"reason": str}`` logged for traceability).
    """
    reason = ""
    try:
        body = request.get_json(force=True, silent=True) or {}
        if isinstance(body, dict):
            reason = str(body.get("reason", ""))
    except Exception:  
        pass
    logger.info("drain requested reason=%r", reason)

    def _worker() -> None:
        try:
            with _workers_lock:
                for coll in list(_workers):
                    try:
                        _workers.pop(coll).stop()
                    except Exception:  
                        logger.exception("failed to stop worker for %s", coll)
        except Exception:  
            logger.exception("drain: workers teardown failed")

        try:
            emit_control_event("drain_complete", server_id=get_server_mac(), reason=reason)
        except Exception:  
            logger.exception("drain: emit_control_event failed")

        try:
            MongoClient(f"mongodb://localhost:{LOCAL_PORT}/",
                        serverSelectionTimeoutMS=2000).admin.command("shutdown")
        except PyMongoError:
            # shutdown command terminates the connection mid-flight — raise is expected.
            pass
        except Exception:  
            logger.exception("drain: mongod shutdown failed")

    threading.Thread(target=_worker, daemon=True, name="drain").start()
    return "", 202


def run_admin_server() -> None:
    """Block in the Flask dev server on 0.0.0.0:ADMIN_PORT."""
    # threaded=True so /forwarder_config reconfigures don't block /healthz.
    admin.run(host="0.0.0.0", port=ADMIN_PORT, threaded=True, use_reloader=False)
