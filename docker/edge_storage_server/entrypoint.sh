#!/usr/bin/env bash
set -euo pipefail

MONGOD_ARGS="--bind_ip_all --wiredTigerCacheSizeGB 0.25"
if [ -n "${MONGO_REPLSET:-}" ]; then
    MONGOD_ARGS="$MONGOD_ARGS --replSet $MONGO_REPLSET"
fi
if [ -n "${MONGO_PORT:-}" ]; then
    MONGOD_ARGS="$MONGOD_ARGS --port $MONGO_PORT"
fi

# Derive MONGO_URI from MONGO_PORT so the sidecar connects to the right port.
export MONGO_URI="${MONGO_URI:-mongodb://localhost:${MONGO_PORT:-27018}/}"

# Forward SIGTERM to mongod for a clean shutdown (quiesce); bash as PID 1
# would otherwise swallow it and mongod only sees SIGKILL at stop timeout.
trap 'kill -TERM $MONGOD_PID; wait $MONGOD_PID; exit $?' SIGTERM

# shellcheck disable=SC2086
mongod $MONGOD_ARGS &
MONGOD_PID=$!

until mongosh --port "${MONGO_PORT:-27018}" --quiet --eval "db.runCommand({ping:1})" >/dev/null 2>&1; do
    sleep 1
done

# -u: unbuffered so experiment log capture sees state transitions promptly;
# sidecar failure is non-fatal, mongod keeps the container alive.
python3 -u /mongo_telemetry.py &

wait $MONGOD_PID
