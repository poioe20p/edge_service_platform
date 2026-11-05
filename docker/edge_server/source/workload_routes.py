"""Workload API — the endpoints the traffic generator drives.

Application work only: content lookup and aggregation, feed ranking, the local
service-pressure summary and the CPU hash challenge. The control API that the
controller calls (health, readiness, drain, VIP updates, pool renewal) lives in
``control_plane_routes``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import time

from flask import Flask, jsonify, request
from pymongo.errors import PyMongoError
from werkzeug.exceptions import BadRequest

from compute import (
    TREND_WINDOW_SIZE,
    compute_feed_summary,
    compute_service_pressure,
    compute_trend,
    hash_chain,
    score_content_relevance,
    score_feed_relevance,
    verify_feed_integrity,
)
from edge_request_lifecycle import stage_local_request_event
from vip_data_mongo_runtime import (
    _get_write_client,
    log_db_failure,
    run_with_request_lease,
    snapshot_normal_vip_config,
)


# O(retained events) per call — a full 72k buffer cost tens of ms of CPU on the
# 0.6-core edge. Single-flight TTL cache; 0 disables.
_SP_CACHE_TTL_S = float(os.environ.get("SERVICE_PRESSURE_CACHE_TTL_S", "5.0"))
_sp_cache: dict = {}
_sp_refresh_lock = threading.Lock()
_sp_refreshing: set = set()


def register_workload_routes(app: Flask, config, process_state) -> None:

    @app.route("/content", methods=["POST"])
    def content_update():
        """Write an engagement update for a content item to the primary MongoDB.

        This endpoint bypasses the VIP-based read path and connects directly
        to the replica-set primary via a dedicated MongoClient.  Writes
        generate oplog traffic that stresses all replica-set members, making
        storage scale-up measurable.
        """
        data = request.get_json(force=True)
        content_id = data["content_id"]
        engagement = data.get("engagement", 0)
        lan = data.get("lan", "lan1")
        update_fields = {
            "payload.engagement": engagement,
            "last_updated": datetime.now(timezone.utc),
        }
        if "update_padding" in data:
            update_fields["update_padding"] = data["update_padding"]

        client = _get_write_client(lan)
        db = client[config.db_name]
        result = db.content_items.update_one(
            {"_id": content_id},
            {"$set": update_fields},
            upsert=True,
        )
        return jsonify({
            "matched": result.matched_count,
            "modified": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
        })

    @app.route("/content/aggregate", methods=["POST"])
    def content_aggregate():
        """Run an aggregation pipeline on the MongoDB collection via VIP.

        This endpoint uses the standard VIP-based read path.  The
        aggregation performs a full-collection scan + grouping + sort,
        which generates real MongoDB CPU work (not just quick point reads).
        """
        data = request.get_json(force=True)
        lan = data.get("lan", "lan1")
        engagement_threshold = data.get("engagement_threshold", 50)

        pipeline = [
            {"$match": {"payload.engagement": {"$gt": engagement_threshold}}},
            {"$group": {
                "_id": "$content_type",
                "avg_engagement": {"$avg": "$payload.engagement"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"avg_engagement": -1}},
        ]

        try:
            results = run_with_request_lease(
                lan,
                op_name="content_items.aggregate",
                replay_safe=False,
                fn=lambda db: list(
                    db["content_items"].aggregate(pipeline)
                ),
            )
            for doc in results:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
            return jsonify({"results": results, "count": len(results)})
        except PyMongoError as exc:
            log_db_failure("content_aggregate", exc)
            return jsonify({"error": "aggregation failed", "detail": str(exc)}), 500

    @app.route("/content/<path:content_id>", methods=["GET"])
    def content_lookup(content_id: str):
        """Return the latest content item for a requester."""

        requester = request.args.get("requester", "unknown")
        content_lan = content_id.split("::")[0]

        try:
            doc = run_with_request_lease(
                content_lan,
                op_name="content_items.find_one",
                replay_safe=True,
                fn=lambda db: db["content_items"].find_one({"_id": content_id}),
            )

            if doc is None:
                return jsonify({"error": "content not found", "content_id": content_id}), 404

            threshold_override = None
            if requester != "unknown":
                requester_lan = requester.split("::")[0]
                registry = run_with_request_lease(
                    requester_lan,
                    op_name="user_profiles.find_one",
                    replay_safe=True,
                    fn=lambda db: db["user_profiles"].find_one(
                        {"_id": requester},
                        {"profile_config.relevance_override": 1},
                    ),
                )
                if registry:
                    content_type = doc.get("content_type", "")
                    threshold_override = (
                        registry.get("profile_config", {})
                        .get("relevance_override", {})
                        .get(content_type)
                    )

            engagement = doc.get("payload", {}).get("engagement")
            baseline = threshold_override or doc.get("metadata", {}).get("relevance_baseline")
            relevance_result = score_content_relevance(
                engagement,
                baseline,
                doc.get("content_type", ""),
                content_id,
            )

            recent_events = process_state.local_request_state.recent_for_content(
                content_id,
                TREND_WINDOW_SIZE,
            )
            trend_result = compute_trend(recent_events)

            stage_local_request_event(
                request_kind="content_lookup",
                content_id=content_id,
                user_id=requester,
                relevance=relevance_result["relevance"],
                status=doc.get("payload", {}).get("status", "unknown"),
                tags=tuple(doc.get("tags") or ()),
            )

            doc["_id"] = str(doc["_id"])
            doc["above_baseline"] = relevance_result["above_baseline"]
            doc["relevance"] = relevance_result
            doc["trend"] = trend_result
            return jsonify(doc), 200

        except PyMongoError as exc:
            log_db_failure("content_lookup", exc)
            return jsonify({"error": "database error"}), 503

    @app.route("/service_pressure", methods=["GET"])
    def service_pressure():
        """Return a local summary of recent request activity seen by this edge."""

        try:
            window_min = float(
                request.args.get(
                    "window_min",
                    str(config.service_pressure_default_window_min),
                )
            )
            limit = int(
                request.args.get(
                    "limit",
                    str(config.service_pressure_default_limit),
                )
            )
        except ValueError as exc:
            raise BadRequest("window_min must be numeric and limit must be an integer") from exc

        if window_min <= 0:
            raise BadRequest("window_min must be greater than 0")
        if limit <= 0:
            raise BadRequest("limit must be greater than 0")

        limit = min(limit, 50)
        cache_key = (round(window_min, 6), limit)
        am_refreshing = False
        if _SP_CACHE_TTL_S > 0:
            entry = _sp_cache.get(cache_key)
            if entry is not None and (time.monotonic() - entry[0]) < _SP_CACHE_TTL_S:
                return jsonify(entry[1]), 200
            with _sp_refresh_lock:
                if cache_key not in _sp_refreshing:
                    _sp_refreshing.add(cache_key)
                    am_refreshing = True
            if not am_refreshing:
                entry = _sp_cache.get(cache_key)
                if entry is not None:
                    return jsonify(entry[1]), 200
        try:
            requested_window_seconds = window_min * 60
            now_epoch = time.time()
            cutoff_epoch = now_epoch - requested_window_seconds
            events, truncated = process_state.local_request_state.events_since_with_truncation(
                cutoff_epoch
            )
            retained_window_seconds = requested_window_seconds
            if truncated and events:
                oldest_retained_epoch = min(float(ev.get("timestamp", now_epoch)) for ev in events)
                retained_window_seconds = max(1.0, now_epoch - oldest_retained_epoch)

            response = compute_service_pressure(
                events,
                limit=limit,
                region=config.lan_id,
                window_seconds=retained_window_seconds,
            )
            response["window_min"] = window_min
            response["window_truncated"] = truncated
            response["retained_window_seconds"] = round(retained_window_seconds, 3)
            response["buffer_capacity_events"] = config.local_request_buffer_max_events
            if _SP_CACHE_TTL_S > 0:
                _sp_cache[cache_key] = (time.monotonic(), response)
            return jsonify(response), 200
        finally:
            if _SP_CACHE_TTL_S > 0 and am_refreshing:
                with _sp_refresh_lock:
                    _sp_refreshing.discard(cache_key)

    @app.route("/compute/hash", methods=["GET"])
    def compute_hash():
        """Request-bound CPU hashing — the RQ2 v4 computational-demand press.

        Runs ``config.hash_work_factor`` chained SHA-256 rounds over the
        caller's nonce and returns the digest prefix as a correctness witness.
        Pure CPU, no database access, no caching. The nonce is required so the
        digest stays a per-request witness.
        """
        nonce = request.args.get("nonce", "")
        if not nonce:
            raise BadRequest("nonce is required")
        rounds = config.hash_work_factor
        digest = hash_chain(nonce, rounds)
        stage_local_request_event(
            request_kind="hash_challenge",
            content_id=None,
            user_id="",
            relevance="",
            status="ok",
            tags=(),
        )
        return jsonify({
            "rounds": rounds,
            "nonce": nonce,
            "digest": digest.hex()[:16],
        }), 200

    @app.route("/feed/<user_id>", methods=["GET"])
    def feed_ranking(user_id: str):
        """Return the most relevant content for a user profile."""

        limit = int(request.args.get("limit", "10"))
        user_lan = user_id.split("::")[0]

        try:
            registry = run_with_request_lease(
                user_lan,
                op_name="user_profiles.find_one",
                replay_safe=True,
                fn=lambda db: db["user_profiles"].find_one({"_id": user_id}),
            )

            if registry is None:
                return jsonify({"error": "user not found", "user_id": user_id}), 404

            subscribed_tags = registry.get("subscribed_tags", [])

            content_items: list[dict] = []
            for lan in snapshot_normal_vip_config():
                content_items.extend(
                    run_with_request_lease(
                        lan,
                        op_name="content_items.find.feed",
                        replay_safe=True,
                        fn=lambda db, subscribed_tags=subscribed_tags: list(
                            db["content_items"].find(
                                {"tags": {"$in": subscribed_tags}},
                                {
                                    "_id": 1,
                                    "content_type": 1,
                                    "tags": 1,
                                    "payload": 1,
                                    "metadata": 1,
                                    "region_origin": 1,
                                    "last_updated": 1,
                                },
                                batch_size=config.feed_candidate_limit,
                            ).sort("last_updated", -1).limit(config.feed_candidate_limit)
                        ),
                    )
                )

            content_items = score_feed_relevance(content_items)
            content_items = content_items[:limit]
            verify_feed_integrity(content_items, config.feed_integrity_work_factor)
            summary = compute_feed_summary(content_items)

            stage_local_request_event(
                request_kind="feed_ranking",
                content_id=None,
                user_id=user_id,
                relevance="quiet",
                status="feed_ranking",
                tags=tuple(subscribed_tags),
            )

            for content_item in content_items:
                content_item["_id"] = str(content_item["_id"])

            return jsonify(
                {
                    "user_id": user_id,
                    "subscribed_tags": subscribed_tags,
                    "content_items": content_items,
                    "summary": summary,
                }
            ), 200

        except PyMongoError as exc:
            log_db_failure("feed_ranking", exc)
            return jsonify({"error": "database error"}), 503