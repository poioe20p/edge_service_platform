# Edge service platform — reproduction artefact

Code accompanying the Master's thesis *Coordinating Telemetry, Scaling and Readiness
Admission in Edge Services: When Added Capacity Meets User Demand* (ISCTE — Master's in
Telecommunications and Computer Engineering), by Pedro Ojo Oriakhi.

The platform is an SDN-controlled set of Dockerised edge servers. A double-VIP routing
model places requests on edge replicas, a metadata-driven controller scales compute and
storage nodes, and MongoDB replica sets provide the storage tier.

This repository contains the **code and the configuration needed to rebuild the
platform and run the experiments**. It does not contain campaign outputs.

## Layout

| Path | Contents |
|---|---|
| `docker/` | Images: edge server, storage server, telemetry aggregator, OVS switch, NAT router, os-ken controller |
| `sdn_controller/` | Controller: VIP routing, topology, telemetry sources, scaling policy, admission gates |
| `scripts/` | Entry points: `build_images.sh`, `build_network_setup.sh`, `test_conectivity.sh`, `cleanup.sh`, `Makefile`, plus the base controller env `osken-controller.env` and the WAN knobs `wan.env` |
| `scripts/network/` | Topology construction, node add/remove, WAN latency shaping — see its README |
| `scripts/testing/` | Launch driver, phase definitions, env overrides, workload corpus, seeding — see its README |
| `scripts/testing/phases_campaigns/` | The exact phase profile each thesis campaign ran |
| `scripts/testing/controller_env_overrides/` | The arms and fault overlays each campaign was launched with |
| `scripts/testing/data/` | The request corpus the traffic generator reads |

## Documentation

| Page | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Deployment, addressing, control and data planes |
| [docs/request-path.md](docs/request-path.md) | Double-VIP routing, backend selection, conntrack NAT |
| [docs/telemetry-and-scaling.md](docs/telemetry-and-scaling.md) | Telemetry sources, scaling decisions, admission gates |
| [docs/running-experiments.md](docs/running-experiments.md) | Phases, configuration, launching, run outputs |

## Requirements

- Linux host with Docker, `docker-ce-cli`, `iproute2`, `util-linux`,
  `openvswitch-switch`
- Passwordless `sudo` (`sudo -n`) — the scripts create network namespaces,
  veth pairs and containers
- Python 3 (`python3`) on the host for seeding and for the collectors, with
  `pymongo`, `pyzmq` and `aiohttp`; the controller's own interpreter
  lives in `docker/os-ken` (Python 3.10, `os-ken==3.1.1`)

## Build and run

```bash
cd scripts

./build_images.sh                          # build every image
make setup_network                         # two LANs, switches, router, replica sets
make create_clients CLIENT_TCP_SYN_RETRIES=9
make setup_test_data CONTENT_ITEMS=3000 USERS=100 DATA_SEED=42
make run_experiment RUN_LABEL=baseline PHASES_CONFIG=testing/phases.json
make teardown_clients                      # remove clients and tear down
```

`make setup_network`, `make create_clients`, `make setup_test_data` and
`make run_experiment` are the individual stages; `make setup_test_data` expands to
`seed_content_items seed_user_profiles create_indexes export_snapshot`.

A run writes its outputs under `scripts/testing/metrics/<timestamp>_<label>/`, including
a snapshot of the controller environment in force. To reproduce a campaign arm, pass
the matching override:

```bash
OSKEN_ENV_OVERRIDE_FILE=testing/controller_env_overrides/rq2v4_mode_stor.env \
  make setup_network create_clients setup_test_data run_experiment RUN_LABEL=rq2-storage
```

## Configuration

`scripts/osken-controller.env` is the base controller environment and
`scripts/testing/controller_env_overrides/current_state_integrated.env` the tuned
override used by every campaign. Both are merged with one arm file from
`scripts/testing/controller_env_overrides/` via `OSKEN_ENV_OVERRIDE_FILE`, and one
fault overlay as well on the RQ3 runs that carry one; see the README there for which
file belongs to which campaign.

`scripts/testing/phases.json` is the canonical workload phase profile. The profiles
the thesis campaigns actually ran are in `scripts/testing/phases_campaigns/`, one file
per campaign, each copied verbatim from the `phases_snapshot.json` its runs recorded;
see the README there for the campaign-to-file mapping.

`scripts/testing/data/workload_snapshot/` holds the request corpus
(`content_items.json`, `user_profiles.json`) that `traffic_generator.py` reads. It
can be regenerated from a populated database with
`make export_snapshot`, but shipping it makes a cold start possible.

## What is not here

- **Campaign outputs.** No metrics directories, run folders or per-client request
  logs are included. Re-running a campaign regenerates them.
- **Analysis scripts.** The code that computes the reported statistics and the
  figures is not part of this repository. Each run folder carries the inputs those
  computations need — per-request latency, per-container samples, the controller's
  decision, admission and telemetry-delivery logs, and the merged environment — and the
  metric and test definitions are given in the thesis.
- **Campaign records.** The experiment plans and post-run analyses that lived under
  `docs/operation/testing/experiment/` in the development repository are not
  shipped.
- **The thesis sources and figures.** Those are part of the thesis document, not
  of this artefact.

## Analysis

The run folder is self-describing, so an analysis can be rebuilt from it:
`client_requests.csv` carries one row per request with its phase, client and status,
`resource_stats.csv` the per-LAN window summaries, `decision_log_lan{1,2}.csv` and
`elasticity_events.csv` what the controller saw and decided, and
`controller_env_snapshot.env` and `phases_snapshot.json` the configuration and phase
schedule in force. The per-question evidence sits alongside: `window_log_lan{1,2}.jsonl`
and `telemetry_delivery_log_lan{1,2}.csv` for the telemetry comparisons, and
`admission_log_lan{1,2}.csv` for the readiness-admission ones. The outcome definitions and the statistical tests applied to these
files are given in the thesis.

## Licence

MIT — see [LICENSE](LICENSE).
