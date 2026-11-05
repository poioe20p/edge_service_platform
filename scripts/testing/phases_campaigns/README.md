# Campaign phase profiles

Each file is the exact phase profile one campaign ran, copied from the
`phases_snapshot.json` that its runs recorded. Pass it with `PHASES_CONFIG` to
reproduce that campaign's workload.

| Use this file | To reproduce |
|---|---|
| `rq1_load_rise.json` | RQ1 telemetry modes — baseline, a 180 s load rise, recovery, drop, idle tail |
| `rq2_mixed_press.json` | RQ2 mixed press — 600 s carrying content lookups and compute challenges |
| `rq2_data_access_press.json` | RQ2 data-access press — 480 s of read-heavy content serving |
| `rq2_compute_bound_press.json` | RQ2 compute-bound press — 600 s of hash challenges only |
| `../phases.json` | RQ3 admission modes — 600 s compute plateau at 2.0 requests per client per second |
| `rq3_higher_rate.json` | RQ3 higher-rate set — same shape, plateau at 1.5 |
| `rq3_poll_interval.json` | RQ3 poll-interval set — 180 s compute spike |

`phases.json` is the canonical file and already carries the RQ3 profile, so no copy
of it is kept here.

These files are records, not templates. To run a new workload, edit `../phases.json`
in place; do not add a variant here.