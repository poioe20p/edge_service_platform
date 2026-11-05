# Network

Everything that puts a node on the topology. None of it is run by hand in the normal
flow: `../build_network_setup.sh` builds the whole topology in one call, and the three
node-lifecycle scripts are how the controller reaches the host network.

| Script | Called by | Does |
|---|---|---|
| `build_router.sh` | `build_network_setup.sh` | Router WAN link (`veth5`), host routes to both LANs, Mongo DNAT |
| `build_network_1.sh` | `build_network_setup.sh` | LAN1: bridge, switch, namespaces, static edge / storage / aggregator |
| `build_network_2.sh` | `build_network_setup.sh` | The same for LAN2 |
| `inject_wan_latency.sh` | `build_network_setup.sh` | `tc netem` on the router NICs, driven by `../wan.env` |
| `clients/create_test_clients.sh` | `make create_clients` | Client namespaces and their veth pairs |
| `clients/remove_test_clients.sh` | `make teardown_clients` | Removes them |
| `add_network_node.sh` | `compute_node_manager.py`, `storage_node_manager.py` | Attaches a spawned container to a bridge |
| `remove_network_node.sh` | `compute_node_manager.py` | Detaches a retired edge node |
| `remove_network_storage_node.sh` | `storage_node_manager.py` | Detaches a retired storage node, running `rs.remove()` unless the controller already did |

The last three are what makes a scaling action real: a scale-up is only complete once
`add_network_node.sh` has run, and a scale-down only once the matching `remove_*` has.
`../cleanup.sh` clears whatever they leave behind.

Veth indices come from disjoint ranges so a client and a spawned node can never claim
the same interface: `100-149` and `250-299` for spawned service nodes, `150-245` for LAN1
clients, `300-395` for LAN2. Clients take addresses from the `.56-151` slice of their
LAN's /24.
