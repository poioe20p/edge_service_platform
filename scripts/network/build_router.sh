#!/bin/bash

# Host-side WAN link for the inter-LAN router: a point-to-point veth (veth5) plus the
# host routes that make 10.0.0.0/24 and 10.0.1.0/24 reachable through it. There is no
# Internet uplink in this topology.

set -euo pipefail

echo "Configuring NAT router WAN interface..."

PID_ROUTER=${PID_ROUTER:-$(docker inspect -f '{{.State.Pid}}' nat-router)}

# NOTE: veth1-4 are reserved for build_network_1.sh (OVS bridge ports for LAN1).
#       veth21-24 are reserved for build_network_2.sh (OVS bridge ports for LAN2).
echo "Creating veth pair for router WAN..."
for IFACE in veth5 veth5-peer; do
  if ip link show "$IFACE" >/dev/null 2>&1; then
    sudo ip link del "$IFACE" >/dev/null 2>&1 || true
  fi
done

sudo ip link add veth5 type veth peer name veth5-peer # router WAN side

sudo ip link set veth5-peer netns $PID_ROUTER

sudo nsenter -t $PID_ROUTER -n ip link set veth5-peer name eth0
sudo nsenter -t $PID_ROUTER -n ip link set eth0 address 00:00:00:00:00:BB  # router WAN MAC
sudo nsenter -t $PID_ROUTER -n ip link set eth0 up
sudo nsenter -t $PID_ROUTER -n ip addr add 192.168.100.2/24 dev eth0  # router’s WAN IP

sudo ip link set veth5 up
sudo ip addr add 192.168.100.1/24 dev veth5  # host acts as router's gateway


sudo nsenter -t $PID_ROUTER -n bash -c "
  echo 1 > /proc/sys/net/ipv4/ip_forward
"

echo "Ensuring host routes to 10.0.0.0/24 and 10.0.1.0/24 via 192.168.100.2..."
if ! sudo ip route replace 10.0.0.0/24 via 192.168.100.2 dev veth5 >/dev/null 2>&1; then
  echo "WARNING: failed to program route to 10.0.0.0/24; check host networking." >&2
else
  ip route show 10.0.0.0/24
fi
if ! sudo ip route replace 10.0.1.0/24 via 192.168.100.2 dev veth5 >/dev/null 2>&1; then
  echo "WARNING: failed to program route to 10.0.1.0/24; check host networking." >&2
else
  ip route show 10.0.1.0/24
fi


sudo nsenter -t $PID_ROUTER -n iptables -A FORWARD -i eth0 -o eth1 -j ACCEPT
sudo nsenter -t $PID_ROUTER -n iptables -A FORWARD -i eth1 -o eth0 -j ACCEPT
sudo nsenter -t $PID_ROUTER -n iptables -A FORWARD -i eth0 -o eth2 -j ACCEPT
sudo nsenter -t $PID_ROUTER -n iptables -A FORWARD -i eth2 -o eth0 -j ACCEPT
sudo nsenter -t $PID_ROUTER -n iptables -A FORWARD -i eth1 -o eth2 -j ACCEPT
sudo nsenter -t $PID_ROUTER -n iptables -A FORWARD -i eth2 -o eth1 -j ACCEPT

# edge_storage_server_n1 (10.0.0.4:27018) exposed as 192.168.100.2:27018
sudo nsenter -t $PID_ROUTER -n iptables -t nat -A PREROUTING -i eth0 -p tcp \
  -d 192.168.100.2 --dport 27018 -j DNAT --to-destination 10.0.0.4:27018
sudo nsenter -t $PID_ROUTER -n iptables -t nat -A POSTROUTING -o eth1 -p tcp \
  -s 10.0.0.4 --sport 27018 -j SNAT --to-source 192.168.100.2:27018

# edge_storage_server_n2 (10.0.1.4:27018) exposed as 192.168.100.2:27118
sudo nsenter -t $PID_ROUTER -n iptables -t nat -A PREROUTING -i eth0 -p tcp \
  -d 192.168.100.2 --dport 27118 -j DNAT --to-destination 10.0.1.4:27018
sudo nsenter -t $PID_ROUTER -n iptables -t nat -A POSTROUTING -o eth2 -p tcp \
  -s 10.0.1.4 --sport 27018 -j SNAT --to-source 192.168.100.2:27118

echo "============================================================================"
echo "NAT router configuration complete."
echo "============================================================================"

# WAN emulation: tc netem on inter-LAN egress; reads the WAN_* knobs sourced
# from scripts/wan.env in build_network_setup.sh (no-op when all are 0).
SCRIPT_DIR_BR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_ROUTER="${PID_ROUTER}" bash "${SCRIPT_DIR_BR}/inject_wan_latency.sh"
