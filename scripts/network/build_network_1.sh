#!/bin/bash
set -euo pipefail

echo "[v6] STORAGE_CPUS=${STORAGE_CPUS:-UNSET} EDGE_CPUS=${EDGE_CPUS:-UNSET}" >&2

get_container_pid() {
    local container="$1"
    local timeout="${2:-5}"
    local elapsed=0
    local pid=0
    local status

    while [[ $elapsed -lt $timeout ]]; do
        status=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)
        if [[ "$status" == "exited" || "$status" == "dead" ]]; then
            echo "Error: container '$container' exited unexpectedly." >&2
            echo "--- logs for $container ---" >&2
            docker logs "$container" >&2 || true
            exit 1
        fi
        pid=$(docker inspect -f '{{.State.Pid}}' "$container" 2>/dev/null || echo 0)
        if [[ "$pid" -gt 0 ]]; then
            echo "$pid"
            return 0
        fi
        sleep 1
        (( elapsed++ )) || true
    done

    echo "Error: timed out waiting for PID of container '$container'." >&2
    echo "--- logs for $container ---" >&2
    docker logs "$container" >&2 || true
    exit 1
}

echo "Creating OVS bridge ovs-br0..."
docker exec ovs ovs-vsctl add-br ovs-br0

echo "Creating veth pairs..."
for IFACE in veth1 veth2 veth3 veth4 \
             veth1-peer veth2-peer veth3-peer veth4-peer; do
  if ip link show "$IFACE" >/dev/null 2>&1; then
    sudo ip link del "$IFACE" >/dev/null 2>&1 || true
  fi
done

# NOTE: veth5 and veth6 are reserved for build_router.sh (router WAN and Internet uplink).
sudo ip link add veth1 type veth peer name veth1-peer # edge_server_n1
sudo ip link add veth2 type veth peer name veth2-peer # edge_storage_server_n1
sudo ip link add veth3 type veth peer name veth3-peer # router LAN side
sudo ip link add veth4 type veth peer name veth4-peer # aggregator_n1

echo "Moving veth interfaces into OVS container's namespace..."
PID_OVS=$(docker inspect -f '{{.State.Pid}}' ovs)
sudo mkdir -p /var/run/netns
sudo ln -sf /proc/$PID_OVS/ns/net /var/run/netns/ovs
sudo ip link set veth1 netns ovs
sudo ip link set veth2 netns ovs
sudo ip link set veth3 netns ovs
sudo ip link set veth4 netns ovs

echo "Attaching veth interfaces to OVS bridge inside container..."
docker exec ovs ovs-vsctl add-port ovs-br0 veth1
docker exec ovs ovs-vsctl add-port ovs-br0 veth2
docker exec ovs ovs-vsctl add-port ovs-br0 veth3
docker exec ovs ovs-vsctl add-port ovs-br0 veth4

echo "Bringing up veth interfaces inside OVS namespace..."
docker exec ovs ip link set veth1 up # interface connected to edge_server_n1
docker exec ovs ip link set veth2 up # interface connected to edge_storage_server_n1
docker exec ovs ip link set veth3 up # interface connected to router LAN side
docker exec ovs ip link set veth3 txqueuelen 10000
docker exec ovs ip link set veth4 up # interface connected to aggregator_n1

echo "Launching application containers..."
docker run -dit --name edge_server_n1 --network none --restart=on-failure \
  --log-opt max-size=100m --log-opt max-file=3 \
  --cpus=${EDGE_CPUS:-0.30} --memory=${EDGE_MEMORY:-256m} \
  --sysctl net.core.somaxconn=1024 \
  --ulimit nofile=65536:65536 \
  -e LAN_ID=lan1 \
  -e OWN_MAC=00:00:00:00:00:02 \
  -e VIP_DATA_RECOVERY_N1_IP=10.0.0.252 \
  -e VIP_DATA_RECOVERY_N2_IP=10.0.1.252 \
  -e VIP_DATA_RECOVERY_SESSION_MAX_AGE_S=35 \
  -e AGGREGATOR_PULL_ADDR=tcp://10.0.0.5:5555 \
  -e LOG_LEVEL=INFO \
  -e HEARTBEAT_ENABLED=true \
  -e FEED_INTEGRITY_WORK_FACTOR=${FEED_INTEGRITY_WORK_FACTOR:-200} \
  ${HASH_WORK_FACTOR:+-e HASH_WORK_FACTOR="${HASH_WORK_FACTOR}"} \
  -e EDGE_FLOW_ISOLATION=${EDGE_FLOW_ISOLATION:-0} \
  -e EDGE_MONGO_READ_PREFERENCE=${EDGE_MONGO_READ_PREFERENCE:-secondaryPreferred} \
  -e EDGE_MONGO_MAX_POOL_SIZE=${EDGE_MONGO_MAX_POOL_SIZE:-1} \
  -e EDGE_POOL_RENEW_ENABLED=${EDGE_POOL_RENEW_ENABLED:-0} \
  -e EDGE_POOL_RENEW_MIN_INTERVAL_S=${EDGE_POOL_RENEW_MIN_INTERVAL_S:-15} \
  -e EDGE_POOL_RENEW_WAVES=${EDGE_POOL_RENEW_WAVES:-2} \
  -e EDGE_POOL_RENEW_WAVE_GAP_S=${EDGE_POOL_RENEW_WAVE_GAP_S:-45} \
  edge_server

echo "Starting edge_storage_server_n1 container..."
docker run -dit --name edge_storage_server_n1 --network none \
  --log-opt max-size=100m --log-opt max-file=3 \
  --cpus=${STORAGE_CPUS:-0.15} --memory=${STORAGE_MEMORY:-512m} \
  --no-healthcheck \
  -e LAN_ID=lan1 \
  -e AGGREGATOR_PULL_ADDR=tcp://10.0.0.5:5555 \
  -e MONGO_REPLSET=rs_net1 \
  -e MONGO_PORT=27018 \
  -e TELEMETRY_INTERVAL_S=10 \
  -e LOG_LEVEL=INFO \
  -e HEARTBEAT_ENABLED=true \
  -v edge_storage_server_n1-data:/data/db edge_storage_server

echo "Starting aggregator_n1 container..."
docker run -dit --name aggregator_n1 --network none --restart=on-failure \
  --log-opt max-size=100m --log-opt max-file=3 \
  -e NETWORK_ID=lan1 \
  -e PULL_ADDR=tcp://0.0.0.0:5555 \
  -e PUB_ADDR=tcp://0.0.0.0:5556 \
  -e WINDOW_S=10 \
  -e LOG_LEVEL=${AGG_LOG_LEVEL:-INFO} \
  -e OVERLOAD_CPU_PCT="${OVERLOAD_CPU_PCT:-5.0}" \
  -e OVERLOAD_PEAK_LATENCY_MS="${OVERLOAD_PEAK_LATENCY_MS:-1000}" \
  -e OVERLOAD_ERROR_RATE="${OVERLOAD_ERROR_RATE:-0.05}" \
  -e OVERLOAD_MIN_REQUESTS="${OVERLOAD_MIN_REQUESTS:-0}" \
  -e WINDOW_LOG_RETENTION="${WINDOW_LOG_RETENTION:-10000}" \
  -e WINDOW_LOG_PATH="${WINDOW_LOG_PATH:-/tmp/window_log.jsonl}" \
  -e ACK_LOG_PATH="${ACK_LOG_PATH:-/tmp/ack_log.jsonl}" \
  local_state_server

if [[ $? -ne 0 ]]; then
  echo "Failed to start application containers. Aborting."
  exit 1
fi

PID1=$(get_container_pid edge_server_n1)
PID_ROUTER=$(get_container_pid nat-router)
PID_MONGO=$(get_container_pid edge_storage_server_n1)
PID_AGG=$(get_container_pid aggregator_n1)

echo "Moving veth peer interfaces into application containers..."
sudo ip link set veth1-peer netns $PID1
sudo ip link set veth2-peer netns $PID_MONGO
sudo ip link set veth3-peer netns $PID_ROUTER
sudo ip link set veth4-peer netns $PID_AGG

echo "Configuring network interfaces inside containers..."

sudo nsenter -t $PID1 -n ip link set veth1-peer name eth0
sudo nsenter -t $PID1 -n ip link set eth0 address 00:00:00:00:00:02   # static MAC
sudo nsenter -t $PID1 -n ip link set eth0 up
sudo nsenter -t $PID1 -n ip addr add 10.0.0.2/24 dev eth0
sudo nsenter -t $PID1 -n ip route add default via 10.0.0.1  # router as gateway

sudo nsenter -t $PID_MONGO -n ip link set veth2-peer name eth0
sudo nsenter -t $PID_MONGO -n ip link set eth0 address 00:00:00:00:00:04   # static MAC
sudo nsenter -t $PID_MONGO -n ip link set eth0 up
sudo nsenter -t $PID_MONGO -n ip addr add 10.0.0.4/24 dev eth0
sudo nsenter -t $PID_MONGO -n ip route add default via 10.0.0.1

sudo nsenter -t $PID_AGG -n ip link set veth4-peer name eth0
sudo nsenter -t $PID_AGG -n ip link set eth0 address 00:00:00:00:00:03
sudo nsenter -t $PID_AGG -n ip link set eth0 up
sudo nsenter -t $PID_AGG -n ip addr add 10.0.0.5/24 dev eth0
sudo nsenter -t $PID_AGG -n ip route add default via 10.0.0.1

echo "Configuring NAT router interfaces..."
sudo nsenter -t $PID_ROUTER -n ip link set veth3-peer name eth1 # port number 2 on OVS (veth3 <-> ovs-br0)
sudo nsenter -t $PID_ROUTER -n ip link set eth1 address 00:00:00:00:00:AA  # router LAN MAC
sudo nsenter -t $PID_ROUTER -n ip link set eth1 up
sudo nsenter -t $PID_ROUTER -n ip link set eth1 txqueuelen 10000
sudo nsenter -t $PID_ROUTER -n ip addr add 10.0.0.1/24 dev eth1  # default GW for LAN


docker exec ovs ovs-vsctl show