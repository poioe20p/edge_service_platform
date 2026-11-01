#!/usr/bin/env bash
# RQ1 v2 run launcher — runs on the cloud VM at ~/efficient-storage-in-edge-scenarios.
# usage: bash rq1_launch_run.sh <env_file> <label> <seed> [extra_make_vars]
#
# Arm C (poll) must pass POLL_INTERVAL_S=30 as extra_make_vars — build_network
# reads it from the shell and Docker -e overrides the env file.
#
# sudo has env_reset: every launch knob MUST be a make command-line var (exported
# env vars do not survive sudo).
#
# EDGE_MONGO_MAX_POOL_SIZE MUST be on the make line too: build_network_1/2.sh read
# ${EDGE_MONGO_MAX_POOL_SIZE:-1} at setup_network, so base edges otherwise get pool 1.
#
# Pool 6, not 12: 4 edges x 12 = 48 concurrent Mongo ops thrash the storage tier at
# STORAGE_CPUS=0.08 (p50 16-17 s collapse); 6 conns/edge is RQ2's proven config.
#
# Plateau: at rate 1.5 demand was 54 DB ops/s/LAN (feed_ranking = 3 DB ops), ~29%
# above RQ2's ~42 cliff; rate 1.2 + EDGE_CPUS 0.25 (99% CPU peaks at 0.15) are the fix.
set -u
cd ~/efficient-storage-in-edge-scenarios || exit 1
ENV_FILE="$1"
LABEL="$2"
SEED="${3:-2001}"
EXTRA="${4:-}"
ulimit -n 65535
exec sudo -n make -C source/scripts setup_network create_clients setup_test_data run_experiment \
  OSKEN_ENV_OVERRIDE_FILE="testing/controller_env_overrides/$ENV_FILE" \
  RUN_LABEL="$LABEL" \
  PHASES_CONFIG=testing/phases_override/phases_rq1_stress_plateau.json \
  CLIENTS=24 CONTENT_ITEMS=3000 USERS=100 DATA_SEED=42 \
  TRAFFIC_DRIVER_MODE=open_loop CURL_MAX_TIME=300 AIOHTTP_SOCK_CONNECT_TIMEOUT=300 INFLIGHT_WINDOW=1024 DRAIN_S=30 \
  CLIENT_TCP_SYN_RETRIES=9 \
  STORAGE_CPUS=0.08 EDGE_CPUS=0.25 WAN_RTT_MS=185 RANDOM_SEED="$SEED" \
  EDGE_MONGO_READ_PREFERENCE=secondaryPreferred EDGE_MONGO_MAX_POOL_SIZE=6 VIP_DATA_PER_CONNECTION_FLOWS=1 \
  $EXTRA \
  SKIP_CLIENTS=1 SKIP_SEED=1 SKIP_SNAPSHOT=1
