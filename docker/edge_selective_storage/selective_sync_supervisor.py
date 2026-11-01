"""Selective-sync supervisor entry point for the Tier 1 edge_selective_storage container.

Wires together :mod:`config`, :mod:`forwarder`, and :mod:`admin`. Each
``ForwarderWorker`` is one Python thread running one MongoDB Change Stream
against exactly one hot collection on the owner replica set. The supervisor
itself does no tailing; its responsibilities are bootstrapping workers from
the initial environment config, exposing the ``/forwarder_config`` admin
endpoint for live reconfiguration, and keeping the admin server in the
foreground so the container stays alive.

Design reference: ``docs/operation/elasticy_manager/implementation/
tier1_selective_sync/`` (README + telemetry_and_config).
"""

from __future__ import annotations

import logging
import threading

from pymongo import MongoClient

import admin
from config import DB_NAME, LOCAL_PORT, LOG_LEVEL, TOKEN_DIR, load_env
from forwarder import ForwarderWorker

logger = logging.getLogger("selective_sync_supervisor")


def run() -> None:
    """Supervisor entry point.

    Loads the initial config from the environment, spawns one
    ForwarderWorker thread per hot collection, hands the shared state to
    the admin module, then blocks in the Flask server.
    """
    logging.basicConfig(level=LOG_LEVEL,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_env()
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    local_db = MongoClient(f"mongodb://localhost:{LOCAL_PORT}/")[DB_NAME]
    # Primary-pinning: directConnection=true forces pymongo into single-server
    # SDAM, so a step-down surfaces as lag past SS_STALENESS_LIMIT_S, not failover.
    remote_db = (MongoClient(f"mongodb://{cfg.owner_host}/?directConnection=true")[DB_NAME]
                 if cfg.owner_host else None)

    workers: dict[str, ForwarderWorker] = {}
    workers_lock = threading.Lock()

    if remote_db is not None:
        with workers_lock:
            for coll, ids in cfg.collections.items():
                w = ForwarderWorker(local_db, remote_db, coll, ids, cfg.max_ttl_s)
                w.start()
                workers[coll] = w
        logger.info("Started %d forwarder worker(s): %s",
                    len(workers), sorted(workers))
    else:
        logger.warning("OWNER_HOST unset — supervisor starting with no workers")

    admin.init_state(
        workers=workers,
        workers_lock=workers_lock,
        local_db=local_db,
        remote_db=remote_db,
        max_ttl_s=cfg.max_ttl_s,
    )
    admin.run_admin_server()


if __name__ == "__main__":
    run()
