"""Run loader — reads all artifacts from a run directory into a Run dataclass."""
from __future__ import annotations

import csv
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .events import ElasticityEvent, parse_logs

# Status-aware helpers for client_requests.csv rows (open-loop `status`
# column). Re-exported here so consumers can import them from either module.
from .client_status import (  # noqa: F401
    CANCELED,
    COMPLETED,
    DROPPED,
    TIMEOUT,
    completed_count,
    completed_rows,
    failure_count,
    is_canceled,
    is_completed,
    is_dropped,
    is_failure,
    is_timeout,
    is_timeout_legacy,
    latency_values,
    offered_count,
    row_status,
    timeout_count,
)


@dataclass
class PhaseSpec:
    name: str
    duration_s: int
    rate_per_client: float
    cross_region_ratio: float
    mix: dict
    start_offset_s: float = 0.0


@dataclass
class Run:
    run_dir: Path
    phases: list[PhaseSpec]
    domain_rows: list[dict]              # resource_stats.csv (trimmed main view)
    node_rows: list[dict]                # empty if per_node_stats.csv missing
    debug_rows: list[dict]               # resource_stats_debug.csv (broad view)
    policy_rows: list[dict]              # policy_state.csv (post-run reconstructed)
    clients: dict[str, list[dict]]       # phase name -> rows
    all_client_rows: list[dict]          # aggregate client_requests.csv rows
    container_event_rows: list[dict]     # container_events.csv rows
    fault_event_rows: list[dict]         # experiment_fault_events.csv rows
    controller_stats_rows: list[dict]    # controller_stats.csv (Phase 5 sampler)
    events: list[ElasticityEvent]
    t0: float                            # earliest window_end, for time normalisation



def _read_csv(path: Path, optional: bool = False) -> list[dict]:
    if not path.exists():
        if not optional:
            warnings.warn(f"Expected CSV not found: {path}")
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_phases(path: Path) -> list[PhaseSpec]:
    """Load phases from phases_snapshot.json.

    Returns an empty list (with a warning) when the file is missing or
    malformed — callers that need phases check for an empty list.
    """
    if not path.exists():
        warnings.warn(
            f"phases_snapshot.json not found at {path}. "
            "Phase-dependent features (cli_tdb_drivers cross_region_ratio) "
            "will fall back to hard-coded defaults."
        )
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        warnings.warn(f"Could not parse {path}: {exc}")
        return []

    # Support both legacy (list) and current ({"phases": [...]}) snapshot formats
    if isinstance(data, dict):
        data = data.get("phases", [])

    phases: list[PhaseSpec] = []
    offset = 0.0
    for item in data:
        p = PhaseSpec(
            name=item.get("name", ""),
            duration_s=int(item.get("duration_s", 0)),
            rate_per_client=float(item.get("rate_per_client", 0.0)),
            cross_region_ratio=float(item.get("cross_region_ratio", 0.0)),
            mix=item.get("mix", {}),
            start_offset_s=offset,
        )
        phases.append(p)
        offset += p.duration_s
    return phases



def load_run(run_dir: Path) -> Run:
    """Load all run artifacts from *run_dir* and return a :class:`Run`."""
    run_dir = Path(run_dir)

    phases = _load_phases(run_dir / "phases_snapshot.json")

    domain_rows = _read_csv(run_dir / "resource_stats.csv")
    node_rows = _read_csv(run_dir / "per_node_stats.csv", optional=True)
    debug_rows = _read_csv(run_dir / "resource_stats_debug.csv", optional=True)
    policy_rows = _read_csv(run_dir / "policy_state.csv", optional=True)

    if not domain_rows:
        warnings.warn(
            "resource_stats.csv not found or empty — "
            "domain-level charts will be skipped."
        )

    if not node_rows:
        warnings.warn(
            "per_node_stats.csv not found — per-node charts will be skipped. "
            "(Run was produced before the per-node CSV was added.)"
        )

    all_client_rows = _read_csv(run_dir / "client_requests.csv", optional=True)
    clients: dict[str, list[dict]] = {p.name: [] for p in phases}
    for row in all_client_rows:
        phase_name = str(row.get("phase", "unknown") or "unknown")
        clients.setdefault(phase_name, []).append(row)

    container_event_rows = _read_csv(run_dir / "container_events.csv", optional=True)
    fault_event_rows = _read_csv(run_dir / "experiment_fault_events.csv", optional=True)
    controller_stats_rows = _read_csv(run_dir / "controller_stats.csv", optional=True)

    log_paths = [
        run_dir / "controller_lan1.log",
        run_dir / "controller_lan2.log",
    ]
    events = parse_logs(log_paths)

    t0 = min(float(r["window_end"]) for r in domain_rows) if domain_rows else 0.0

    return Run(
        run_dir=run_dir,
        phases=phases,
        domain_rows=domain_rows,
        node_rows=node_rows,
        debug_rows=debug_rows,
        policy_rows=policy_rows,
        clients=clients,
        all_client_rows=all_client_rows,
        container_event_rows=container_event_rows,
        fault_event_rows=fault_event_rows,
        controller_stats_rows=controller_stats_rows,
        events=events,
        t0=t0,
    )
