# Edge service platform — reproduction artefact

Code accompanying the Master's thesis *Coordinating Telemetry, Scaling and Readiness Admission in Edge Services: When Added Capacity Meets User Demand* (ISCTE — Master's in Telecommunications and Computer
Engineering), by Pedro Ojo Oriakhi.

The platform is an SDN-controlled set of Dockerised edge servers. A double-VIP
routing model places requests on edge replicas, a metadata-driven controller
scales compute and storage nodes and decides what to replicate towards the edge,
and MongoDB replica sets provide the storage tier.

This repository contains the **code and the configuration needed to rebuild the
platform and run the experiments**. It does not contain campaign outputs.

## Layout

| Path | Contents |
|---|---|
| `docker/` | Images: edge server, storage server, OVS switch, NAT router, os-ken controller |
| `sdn_controller/` | Controller: VIP routing, telemetry sources, elasticity managers, admission and release gates |
| `scripts/` | Entry points: `build_images.sh`, `build_network_setup.sh`, `cleanup.sh`, `Makefile` |
| `scripts/network/` | Topology construction, node add/remove, WAN latency shaping |
| `scripts/testing/` | Launch drivers, phase definitions, env overrides, workload corpus, seeding |
| `scripts/testing/analysis/` | Offline analysers and figure generators; each reads a run directory |
| `scripts/tools/` | Standalone metric and log utilities |

## Documentation

| Page | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Deployment, addressing, control and data planes |
| [docs/request-path.md](docs/request-path.md) | Double-VIP routing, backend selection, conntrack NAT |
| [docs/telemetry-and-scaling.md](docs/telemetry-and-scaling.md) | Telemetry sources, scaling decisions, admission and release |
| [docs/storage-and-selective-sync.md](docs/storage-and-selective-sync.md) | Tiered placement and Tier 1 selective sync |
| [docs/running-experiments.md](docs/running-experiments.md) | Phases, configuration, launching, run outputs |

## Requirements

- Linux host with Docker, `docker-ce-cli`, `iproute2`, `util-linux`,
  `openvswitch-switch`
- Passwordless `sudo` (`sudo -n`) — the scripts create network namespaces,
  veth pairs and containers
- Python 3 (`python3`) on the host for seeding and analysis; the controller's
  own interpreter lives in `docker/os-ken` (Python 3.10, `os-ken==3.1.1`)
- `matplotlib` for `scripts/testing/analysis/`

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

A run writes its outputs under `scripts/testing/metrics/<timestamp>/`, including a
snapshot of the controller environment in force. To reproduce a campaign arm, pass
the matching override:

```bash
OSKEN_ENV_OVERRIDE_FILE=testing/controller_env_overrides/rq2_storage_first.env \
  make setup_network create_clients setup_test_data run_experiment RUN_LABEL=rq2-storage
```

## Configuration

`scripts/osken-controller.env` is the base controller environment and
`scripts/testing/controller_env_overrides/current_state_integrated.env` the tuned
override used by every campaign. Both are merged with one arm file from
`scripts/testing/controller_env_overrides/` via `OSKEN_ENV_OVERRIDE_FILE`; only one
override is honoured at a time.

`scripts/testing/phases.json` is the canonical workload phase profile. The extra
profiles in `scripts/testing/phases_override/` are the ones the `rq1`, `rq3sat` and
`rq3stor` launchers select explicitly through `PHASES_CONFIG`.

`scripts/testing/data/workload_snapshot/` holds the request corpus
(`content_items.json`, `user_profiles.json`) that `traffic_generator.py` reads. It
can be regenerated from a populated database with
`make export_snapshot`, but shipping it makes a cold start possible.

## What is not here

- **Campaign outputs.** No metrics directories, run folders or per-client request
  logs are included. Re-running a campaign regenerates them.
- **Campaign records.** The experiment plans and post-run analyses that lived under
  `docs/operation/testing/experiment/` in the development repository are not
  shipped. A number of scripts still carry that path as a default for a campaign
  record directory or a documentation cross-check; those references resolve to
  nothing here and the corresponding checks are expected to report a missing
  reference rather than fail a build.
- **The thesis sources and figures.** Those are part of the thesis document, not
  of this artefact.
- **Development-host paths in a few analysers.** Some of the RQ1 and RQ2 scripts
  under `scripts/testing/analysis/` carry the development machine's absolute path
  as the default for their input metrics directory or output graph directory. They
  take no other configuration, so point those constants at your own run folder
  before using them. The launchers and the platform itself do not depend on them.

## Analysis

The analysers take a run directory and are independent of the platform:

```bash
cd scripts
python3 testing/analysis/cli_overview.py --run-dir <run-dir>
python3 testing/analysis/cli_phase_summary.py --run-dir <run-dir>
```

`scripts/tools/metrics_stats.py <run-dir>` summarises a metrics folder and is the
entry point used for per-run statistics.

## Licence

MIT — see [LICENSE](LICENSE).
