#!/usr/bin/env bash

set -euo pipefail

MONGO_PORT="${MONGO_PORT:-27018}"

# mongod args: standalone (no --replSet, unlike edge_storage_server).
MONGOD_ARGS="--bind_ip_all --port ${MONGO_PORT}"

# Derive MONGO_URI from MONGO_PORT so the telemetry sidecar connects
# to the right port (same contract as edge_storage_server/entrypoint.sh).
export MONGO_URI="${MONGO_URI:-mongodb://localhost:${MONGO_PORT}/}"

# Ensure the resume-token directory exists before the supervisor starts.
mkdir -p /var/lib/selective_sync

# Forward SIGTERM to mongod so it gets a clean shutdown (quiesce).
trap 'kill -TERM "$MONGOD_PID" 2>/dev/null || true; wait "$MONGOD_PID"; exit $?' SIGTERM

# shellcheck disable=SC2086
mongod $MONGOD_ARGS &
MONGOD_PID=$!

until mongosh --port "${MONGO_PORT}" --quiet \
        --eval "db.runCommand({ping:1})" >/dev/null 2>&1; do
    sleep 1
done

# Telemetry sidecar (host + STANDALONE_CACHE state).
# If it crashes, mongod (and therefore the container) keeps running.
python3 /mongo_telemetry.py &

python3 /selective_sync_supervisor.py &

wait "$MONGOD_PID"
