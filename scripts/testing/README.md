# Testing

The run driver and its helpers. One run is one call to `run_experiment.sh`, which starts
the collectors, drives the phases, stops everything and copies the artefacts into
`metrics/`. The end-to-end flow is in
[../../docs/running-experiments.md](../../docs/running-experiments.md).

| File | Stage | Does |
|---|---|---|
| `run_experiment.sh` | run | The orchestrator: collectors, phases, snapshots, artefact copy |
| `traffic_generator.py` | run | Phased request load from the client namespaces |
| `collect_resource_stats.py` | collector | Aggregator windows to `resource_stats.csv` |
| `sample_controller_stats.py` | collector | Controller container CPU/RAM to `controller_stats.csv` |
| `poll_container_events.py` | collector | `docker ps` diffs to `container_events.csv` |
| `capture_service_logs.py` | collector | `docker logs -f` followers into `service_logs/` |
| `parse_elasticity_logs.py` | collector | Controller log to `elasticity_events.csv` |
| `seed_content_items.py` | seed | The content corpus, into both replica sets |
| `seed_user_profiles.py` | seed | The user profiles, into both replica sets |
| `create_indexes.py` | seed | The indexes, once after seeding |
| `export_workload_snapshot.py` | seed | Dumps the corpus into `data/workload_snapshot/` |

The collectors are started and stopped by `run_experiment.sh`, and the seed stage is
what `make setup_test_data` expands to, so neither is meant to be run by hand.
`parse_elasticity_logs.py` is the exception: it takes captured controller logs and
re-emits the event table, so it can be re-run over a finished run.

| Path | Contents |
|---|---|
| `phases.json` | The canonical phase profile — edit this one in place |
| `phases_campaigns/` | The profile each campaign ran, one file per campaign |
| `controller_env_overrides/` | The arms and overlays, one file per treatment |
| `data/workload_snapshot/` | The corpus the traffic generator reads |
| `metrics/` | Created by the first run; not shipped |

Both subdirectories carry a README mapping file to campaign; start there before choosing
a profile or an arm.
