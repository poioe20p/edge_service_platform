#!/usr/bin/env python3
"""Synthetic selftest for the RQ3 robustness analyzer."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source/scripts/testing/analysis"))

from rq3 import readiness_robustness as robustness  # noqa: E402


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def make_run(base: Path, name: str) -> Path:
    run = base / name
    run.mkdir(parents=True)
    return run


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        run = make_run(base, "rq3rob_loss_all_event_only_1")
        start = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
        events = []
        for index, container in enumerate(("edge_server_lan1_dyn2", "edge_server_lan1_dyn3")):
            at = start + timedelta(seconds=index * 5)
            events.append({
                "timestamp_iso": at.isoformat(),
                "event": "started",
                "container": container,
                "state": "running",
            })
        (run / "container_events.csv").write_text(
            "timestamp_iso,event,container,state\n" + "\n".join(
                f"{row['timestamp_iso']},{row['event']},{row['container']},{row['state']}"
                for row in events) + "\n", encoding="utf-8")
        (run / "admission_log_lan1.csv").write_text(
            "ts,network_id,lan,container,mac,ip,mode,result,spawn_started_ts,"
            "spawn_complete_ts,probe_first_ts,app_ready_ts,admitted_ts,admit_source\n"
            f"0,lan1,1,edge_server_lan1_dyn2,mac,ip,direct,abandoned,"
            f"{start.timestamp():.3f},{(start + timedelta(seconds=1)).timestamp():.3f},"
            f",,{(start + timedelta(seconds=120)).timestamp():.3f},\n", encoding="utf-8")
        result = robustness.classify(run)
        assert result["spawned"] == 2, result
        assert result["abandoned"] == 1, result
        assert result["orphaned_dark"] == 1, result
        assert result["preservation"] == 0.0, result
        parts = robustness.label_parts(run.name)
        assert parts == ("loss_all", "event_only", "1"), parts
        mwu = robustness.exact_mwu([1.0, 2.0], [3.0, 4.0])
        assert 0.0 <= mwu <= 1.0, mwu
        assert robustness.cliffs([3.0, 4.0], [1.0, 2.0]) == 1.0
        print(json.dumps({"classify": "PASS", "label": "PASS", "stats": "PASS"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
