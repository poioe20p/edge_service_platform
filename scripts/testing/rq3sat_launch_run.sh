#!/usr/bin/env bash
# RQ3 saturation re-run launcher — runs on the cloud VM at ~/efficient-storage-in-edge-scenarios.
# usage: bash rq3sat_launch_run.sh <env_file> <label> <seed> <edge_cpus> [extra_make_vars]
# Campaign: docs/operation/testing/experiment/rq3_saturation/experiment_plan.md
#
# sudo has env_reset: every launch knob MUST be a make command-line var, including
# EDGE_MONGO_MAX_POOL_SIZE (build_network_1/2.sh default it to 1 at setup_network).
#
# EDGE_FLOW_ISOLATION=1 MUST be on the make line: build_network_1/2.sh default it to 0,
# and the static edges must emit request_complete for the flow-isolation preflight (§4.1).
set -u
cd ~/efficient-storage-in-edge-scenarios || exit 1
ENV_FILE="$1"
LABEL="$2"
SEED="${3:-3001}"
EDGE_CPUS="${4:-0.25}"
EXTRA="${5:-}"
ulimit -n 65535
exec sudo -n make -C source/scripts setup_network create_clients setup_test_data run_experiment \
  OSKEN_ENV_OVERRIDE_FILE="testing/controller_env_overrides/$ENV_FILE" \
  RUN_LABEL="$LABEL" \
  PHASES_CONFIG=testing/phases_override/phases_rq3_saturation.json \
  CLIENTS=24 CONTENT_ITEMS=3000 USERS=100 DATA_SEED=42 \
  TRAFFIC_DRIVER_MODE=open_loop CURL_MAX_TIME=300 INFLIGHT_WINDOW=1024 DRAIN_S=30 \
  STORAGE_CPUS=0.08 EDGE_CPUS="$EDGE_CPUS" WAN_RTT_MS=185 RANDOM_SEED="$SEED" \
  EDGE_MONGO_READ_PREFERENCE=secondaryPreferred EDGE_MONGO_MAX_POOL_SIZE=6 \
  VIP_DATA_PER_CONNECTION_FLOWS=1 EDGE_FLOW_ISOLATION=1 \
  $EXTRA \
  SKIP_CLIENTS=1 SKIP_SEED=1 SKIP_SNAPSHOT=1
