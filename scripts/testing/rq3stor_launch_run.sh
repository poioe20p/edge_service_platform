#!/usr/bin/env bash
# RQ3 v3 storage-replica benefit launcher (runs on the cloud VM at
# ~/efficient-storage-in-edge-scenarios).
# usage: bash rq3stor_launch_run.sh <env_file> <label> <seed> [edge_cpus] [storage_cpus] [extra_make_vars]
#   e.g. bash rq3stor_launch_run.sh rq3stor_direct.env rq3stor_probe_sb 3001 0.75 0.04
# Resource shaping (RQ3 v3): EDGE_CPUS high (compute never the bottleneck) and
# STORAGE_CPUS low (storage tier is the constrained resource) so that storage
# replica scale-up is the only relief lever.
set -u
cd ~/efficient-storage-in-edge-scenarios || exit 1
ENV_FILE="$1"
LABEL="$2"
SEED="${3:-3001}"
EDGE_CPUS="${4:-0.75}"
STORAGE_CPUS="${5:-0.04}"
EXTRA="${6:-}"
ulimit -n 65535
exec sudo -n make -C source/scripts setup_network create_clients setup_test_data run_experiment \
  OSKEN_ENV_OVERRIDE_FILE="testing/controller_env_overrides/$ENV_FILE" \
  RUN_LABEL="$LABEL" \
  PHASES_CONFIG=testing/phases_override/phases_rq3_storage.json \
  CLIENTS=24 CONTENT_ITEMS=3000 USERS=100 DATA_SEED=42 \
  TRAFFIC_DRIVER_MODE=open_loop CURL_MAX_TIME=300 INFLIGHT_WINDOW=1024 DRAIN_S=30 \
  STORAGE_CPUS="$STORAGE_CPUS" EDGE_CPUS="$EDGE_CPUS" WAN_RTT_MS=185 RANDOM_SEED="$SEED" \
  EDGE_MONGO_READ_PREFERENCE=secondaryPreferred EDGE_MONGO_MAX_POOL_SIZE=6 \
  VIP_DATA_PER_CONNECTION_FLOWS=1 EDGE_FLOW_ISOLATION=1 \
  $EXTRA \
  SKIP_CLIENTS=1 SKIP_SEED=1 SKIP_SNAPSHOT=1
