#!/usr/bin/env python3
"""Compact figure-data export for the RQ3 timing campaign (companion tool).

Runs on the experiment VM against the 36 frozen E-stage run dirs of
`rq3_timing`; read-only. Emits ONE small JSON consumed by
`timing_figures.py` on the workstation, which renders the four campaign
figures (block contrast, slow-share series, relief tails, admission
accounting).

Usage (on the VM, under the analysis package parent):
    cd <pkg_parent>
    sudo -n env PYTHONPATH=. python3 -m rq3.timing_figures_export \
        --out /tmp/rq3tim_figures_data.json <36 run dirs>

All quantities reuse the campaign analyzer definitions (slow = timeout or
latency > QOE_SLOW_S on service rows; tail = last TIMING_TAIL_S of the
plateau; onset = first TIMING_ONSET_S; admitted = dynamic compute backends
with result == "admitted" in the admission logs).
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from pathlib import Path
from typing import Any

from rq3 import readiness_low_headroom as H
from rq3 import readiness_robustness as R


def bin_series(run_dir: Path, bin_s: float, pre_s: float) -> list[dict[str, Any]]:
    """slow_rate per fixed bin over [plateau_start - pre_s, plateau_end)."""
    rows = H.request_rows(run_dir)
    rows.sort(key=lambda row: row["_sent"])
    sent = [row["_sent"] for row in rows]
    start, end = H.phase_bounds(run_dir)[H.PHASE]
    out: list[dict[str, Any]] = []
    t = start - pre_s
    while t < end:
        t2 = t + bin_s
        lo = bisect.bisect_left(sent, t)
        hi = bisect.bisect_left(sent, t2)
        status = R._qoe_status(rows[lo:hi])
        out.append({
            "t_rel": round(t - start, 1),
            "service": status["service_requests"],
            "slow_rate": status["slow_rate"],
        })
        t = t2
    return out


def admission_summary(run_dir: Path) -> dict[str, Any]:
    rows = R.admission_rows(run_dir)
    dynamic = [row for row in rows
               if R.TIMING_DYNAMIC_RE.search(row.get("container", "") or "")]
    admitted = [row for row in dynamic if row.get("result") == "admitted"]
    abandoned = [row for row in dynamic if row.get("result") == "abandoned"]
    sources: dict[str, int] = {}
    by_lan: dict[str, int] = {}
    for row in admitted:
        key = row.get("admit_source") or "unknown"
        sources[key] = sources.get(key, 0) + 1
        lan = f"lan{row.get('lan')}"
        by_lan[lan] = by_lan.get(lan, 0) + 1
    return {
        "admitted": len(admitted),
        "abandoned": len(abandoned),
        "by_source": sources,
        "by_lan": by_lan,
        "containers": sorted({row.get("container") for row in admitted}),
    }


def run_record(run_dir: Path, bin_s: float, pre_s: float) -> dict[str, Any]:
    parts = R.timing_label_parts(run_dir.name)
    if parts is None:
        raise ValueError(f"cannot parse timing label: {run_dir.name}")
    cell, arm, block = parts
    rows = H.request_rows(run_dir)
    plateau = [row for row in rows if row.get("phase") == H.PHASE]
    return {
        "block": int(block),
        "cell": cell,
        "arm": arm,
        "slow_full": R._qoe_status(plateau)["slow_rate"],
        "slow_onset": R.timing_onset_slow(run_dir),
        "slow_tail": R.timing_tail_slow(run_dir),
        "admissions": admission_summary(run_dir),
        "series": bin_series(run_dir, bin_s, pre_s),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--bin", type=float, default=10.0,
                        help="series bin width in seconds (default 10)")
    parser.add_argument("--pre", type=float, default=60.0,
                        help="seconds of pre-onset context in the series")
    parser.add_argument("run_dirs", nargs="+")
    args = parser.parse_args()

    runs: dict[str, Any] = {}
    for path_str in args.run_dirs:
        path = Path(path_str)
        record = run_record(path, args.bin, args.pre)
        key = f"b{record['block']}_{record['cell']}_{record['arm']}"
        if key in runs:
            raise ValueError(f"duplicate run for {key}")
        runs[key] = record

    blocks = sorted({record["block"] for record in runs.values()})
    output = {
        "family": "rq3_timing",
        "kind": "figure-data export (companion; do not edit the frozen records)",
        "bin_s": args.bin,
        "pre_s": args.pre,
        "runs": len(runs),
        "blocks": blocks,
        "data": runs,
    }
    Path(args.out).write_text(json.dumps(output, indent=2, sort_keys=True),
                              encoding="utf-8")

    for key in sorted(runs):
        record = runs[key]
        adm = record["admissions"]
        print(f"{key}: slow_full={record['slow_full']!r} "
              f"onset={record['slow_onset']!r} tail={record['slow_tail']!r} "
              f"admitted={adm['admitted']} abandoned={adm['abandoned']} "
              f"src={adm['by_source']}")
    print(f"wrote {args.out} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
