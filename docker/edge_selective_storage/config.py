"""Environment-backed configuration for the selective-sync supervisor.

Kept free of pymongo/flask/zmq imports so it can be unit-tested without the
runtime stack installed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("selective_sync.config")

TOKEN_DIR = Path("/var/lib/selective_sync")
DB_NAME = os.environ.get("SS_DB_NAME", "edge_platform")
LOCAL_PORT = int(os.environ.get("MONGO_PORT", "27018"))
ADMIN_PORT = int(os.environ.get("SS_ADMIN_PORT", "5001"))
TELEMETRY_INTERVAL_S = float(os.environ.get("TELEMETRY_INTERVAL_S", "0.5"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


class _Config:
    __slots__ = ("owner_host", "collections", "max_ttl_s")

    def __init__(self, owner_host: str, collections: dict[str, tuple[str, ...]],
                 max_ttl_s: int) -> None:
        self.owner_host = owner_host
        self.collections = collections
        self.max_ttl_s = max_ttl_s


def load_env() -> _Config:
    """Read OWNER_HOST / COLLECTIONS_JSON / MAX_TTL_S from the environment.

    COLLECTIONS_JSON is a JSON object of ``{collection: [doc_id, ...]}``.
    Missing or malformed values produce an empty worker set (supervisor
    still runs so the admin endpoint can seed workers later).
    """
    owner_host = os.environ.get("OWNER_HOST", "").strip()
    raw = os.environ.get("COLLECTIONS_JSON", "").strip()
    max_ttl_s = int(os.environ.get("MAX_TTL_S", "0"))

    collections: dict[str, tuple[str, ...]] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for coll, ids in parsed.items():
                    if isinstance(coll, str) and isinstance(ids, list):
                        collections[coll] = tuple(i for i in ids)
        except ValueError:
            logger.error("COLLECTIONS_JSON is not valid JSON — starting with empty set")

    if not owner_host:
        logger.warning("OWNER_HOST is unset — workers will not be able to connect")
    return _Config(owner_host=owner_host, collections=collections, max_ttl_s=max_ttl_s)
