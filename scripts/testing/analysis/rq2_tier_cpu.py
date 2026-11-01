"""Extract episode-phase tier CPU per RQ2 run, using the campaign's own statistic.

Mirrors ``rq2v5_cell_stats.py``: episode windows are the window-log entries
whose ``domain_summary.total_requests`` reaches the regime threshold (100 for
the mixed press, 200 for the data-access press), and each tier CPU is the
median over those windows. The storage CPU is reproduced alongside as a
validation column against the committed ``rq2v5_cell_stats.csv``.

Usage:
    python3 rq2_tier_cpu.py <metrics_root>
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from pathlib import Path

RUN_RE = re.compile(r"_rq2v5_(none|comp|stor|both)_(mx|db)_(\d+)(_r\d+)?$")
EPISODE_MIN_REQ = {"db": 200, "mx": 100}


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def window_stats(run_dir: Path, lane: int, regime: str) -> dict:
    path = run_dir / f"window_log_lan{lane}.jsonl"
    if not path.exists():
        return {}
    summaries = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                w = json.loads(line)
            except json.JSONDecodeError:
                continue
            if w.get("domain_summary"):
                summaries.append(w["domain_summary"])
    thr = EPISODE_MIN_REQ[regime]
    ep = [d for d in summaries if (d.get("total_requests") or 0) >= thr]
    return {
        f"lan{lane}_compute_cpu": med([d.get("average_cpu_percent") for d in ep]),
        f"lan{lane}_sto_cpu": med([d.get("avg_storage_cpu_percent") for d in ep]),
        f"lan{lane}_t_proc": med([d.get("avg_time_proc_ms") for d in ep]),
        f"lan{lane}_n": len(ep),
    }


def main() -> None:
    root = Path(sys.argv[1]).expanduser()
    cols = [
        "label", "mode", "regime", "seed",
        "lan1_compute_cpu", "lan2_compute_cpu", "compute_cpu",
        "lan1_sto_cpu", "lan2_sto_cpu", "sto_cpu",
        "lan1_t_proc", "lan2_t_proc", "n1", "n2",
    ]
    w = csv.writer(sys.stdout)
    w.writerow(cols)
    for d in sorted(root.glob("*_rq2v5_*")):
        if not d.is_dir() or "pilot" in d.name:
            continue
        m = RUN_RE.search(d.name)
        if not m:
            continue
        mode, regime, seed, repl = m.groups()
        s = {}
        for lane in (1, 2):
            s.update(window_stats(d, lane, regime))
        pair = lambda k: [s[f"lan1_{k}"], s[f"lan2_{k}"]]
        comp = [v for v in pair("compute_cpu") if v is not None]
        sto = [v for v in pair("sto_cpu") if v is not None]
        w.writerow([
            f"{d.name.split('_', 2)[2]}{repl or ''}", mode, regime, seed,
            s.get("lan1_compute_cpu"), s.get("lan2_compute_cpu"),
            statistics.fmean(comp) if comp else "",
            s.get("lan1_sto_cpu"), s.get("lan2_sto_cpu"),
            statistics.fmean(sto) if sto else "",
            s.get("lan1_t_proc"), s.get("lan2_t_proc"),
            s.get("lan1_n"), s.get("lan2_n"),
        ])


if __name__ == "__main__":
    main()
