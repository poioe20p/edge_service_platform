"""ForwarderWorker: one thread, one Change Stream, one collection."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from pymongo import ReplaceOne
from pymongo.errors import PyMongoError

from resume_token import load_resume_token, save_resume_token, token_file_age_s
from telemetry import compute_lag_s, emit_telemetry

logger = logging.getLogger("selective_sync.forwarder")


class ForwarderWorker(threading.Thread):
    """One thread, one Change Stream, one collection.

    Invariant: at most one ForwarderWorker instance exists per hot
    collection on this node. The worker owns:
      * the set of hot document IDs currently mirrored locally,
      * the Change Stream cursor tailing the owner replica set,
      * the on-disk resume token file for this collection.

    Lifecycle: _seed() -> (optional) _ensure_ttl_index() -> tail loop.
    Reconfiguration (new hot set for the same collection) replaces the
    stream in place via reconfigure(); it does not replace the thread
    and does not re-seed unchanged IDs.
    """

    def __init__(self, local_db, remote_db, collection: str,
                 hot_ids: tuple[str, ...], max_ttl_s: int) -> None:
        super().__init__(name=f"forwarder-{collection}", daemon=True)
        self.local = local_db
        self.remote = remote_db
        self.collection = collection
        self.hot_ids: tuple[str, ...] = tuple(hot_ids)
        self.max_ttl_s = max_ttl_s
        self._stop = threading.Event()
        self._seeded: set[str] = set()
        self._reconfigure_event = threading.Event()
        self._pending_hot: tuple[str, ...] | None = None
        self._reconfigure_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()
        self._reconfigure_event.set()

    def reconfigure(self, new_hot: tuple[str, ...]) -> None:
        """Schedule a hot-set swap; the tail loop picks it up on next event.

        The loop closes its current Change Stream, re-seeds only the newly
        added IDs, deletes dropped IDs from the local mongod, and reopens
        the stream with the new ``$match`` from the persisted resume token.
        """
        with self._reconfigure_lock:
            self._pending_hot = tuple(new_hot)
            self._reconfigure_event.set()

    def run(self) -> None:
        """Seed hot docs, then tail the Change Stream until stopped.

        The resume token is persisted after every applied change so a
        crash or clean stop resumes without gaps. On Change Stream
        failure the worker logs and exits; the supervisor does not
        auto-restart (restart is a container concern).
        """
        while not self._stop.is_set():
            try:
                self._run_once()
            except PyMongoError as exc:
                logger.error("[%s] Change Stream error: %s", self.collection, exc)
                return
            except Exception:  # pragma: no cover - defensive
                logger.exception("[%s] unexpected error in tail loop",
                                 self.collection)
                return
            if self._stop.is_set():
                return

    def _run_once(self) -> None:
        self._drain_reconfigure()

        self._seed()
        if self.max_ttl_s:
            self._ensure_ttl_index()

        token = load_resume_token(self.collection)
        pipeline = [{"$match": {"documentKey._id": {"$in": list(self.hot_ids)}}}]

        logger.info("[%s] opening Change Stream (hot=%d, resume=%s)",
                    self.collection, len(self.hot_ids), bool(token))
        with self.remote[self.collection].watch(
            pipeline, resume_after=token, full_document="updateLookup"
        ) as stream:
            while not self._stop.is_set() and not self._reconfigure_event.is_set():
                # try_next() returns None on timeout without raising, which
                # lets us periodically poll _stop / _reconfigure_event.
                change = stream.try_next()
                if change is None:
                    time.sleep(0.5)
                    continue
                self._apply(change)
                save_resume_token(self.collection, stream.resume_token)
                emit_telemetry(
                    self.collection,
                    lag_s=compute_lag_s(change),
                    token_age_s=token_file_age_s(self.collection),
                    hot_doc_count=len(self.hot_ids),
                )
        logger.info("[%s] Change Stream closed (stop=%s reconfigure=%s)",
                    self.collection, self._stop.is_set(),
                    self._reconfigure_event.is_set())

    def _drain_reconfigure(self) -> None:
        with self._reconfigure_lock:
            if self._pending_hot is not None:
                self.hot_ids = self._pending_hot
                self._pending_hot = None
            self._reconfigure_event.clear()

    def _seed(self) -> None:
        """Bring the local copy into sync with the owner's hot set.

        Idempotent and incremental: only IDs in ``self.hot_ids`` that are
        not yet in ``self._seeded`` are fetched and bulk-upserted
        locally. IDs that used to be hot but no longer are get deleted
        from the local mongod. Called at worker start and before each
        stream reopen.
        """
        new_ids = [i for i in self.hot_ids if i not in self._seeded]
        if new_ids:
            src = self.remote[self.collection].find({"_id": {"$in": new_ids}})
            ops = []
            for d in src:
                if self.max_ttl_s:
                    d["_cache_ts"] = datetime.now(timezone.utc)
                ops.append(ReplaceOne({"_id": d["_id"]}, d, upsert=True))
            if ops:
                self.local[self.collection].bulk_write(ops, ordered=False)
            self._seeded.update(new_ids)

        stale = self._seeded - set(self.hot_ids)
        if stale:
            self.local[self.collection].delete_many({"_id": {"$in": list(stale)}})
            self._seeded.difference_update(stale)

    def _ensure_ttl_index(self) -> None:
        """Create the TTL index on ``_cache_ts`` when MAX_TTL_S is set.

        ``createIndex`` is a no-op when an identical index already exists,
        so this is safe to call on every worker start.
        """
        self.local[self.collection].create_index(
            "_cache_ts", expireAfterSeconds=self.max_ttl_s)

    def _apply(self, change: dict) -> None:
        """Apply a single Change Stream event to the local mongod."""
        op = change["operationType"]
        doc_key = change.get("documentKey") or {}
        _id = doc_key.get("_id")
        if _id is None:
            return
        if op in ("insert", "replace", "update"):
            doc = change.get("fullDocument")
            if doc is None:  # update with lookup miss — matching delete will follow
                return
            if self.max_ttl_s:
                doc["_cache_ts"] = datetime.now(timezone.utc)
            self.local[self.collection].replace_one({"_id": _id}, doc, upsert=True)
        elif op == "delete":
            self.local[self.collection].delete_one({"_id": _id})
