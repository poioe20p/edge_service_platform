#!/usr/bin/env python3
"""Figure-data export for the RQ3 admission-modes completion (Family 6).

Runs on the experiment VM against the completion-era run dirs; read-only.
Emits ONE small JSON consumed by the thesis figure generator on the
workstation:

  * ``intact`` -- per-admitted-backend ``spawn -> first successful
    response`` records with start-up class, for the four modes
    (`rq3tim_none_*_1..7` and `rq3snd_none_wake_verify_8..14`, whose
    ordinals 8-14 mirror blocks 1-7);
  * ``lost`` -- per-run slow-share series and admission accounting for the
    four modes (`rq3tim_loss_all_*_1..7` and
    `rq3snd_loss_all_wake_verify_8..14`), reusing the timing exporter's
    ``bin_series``/``admission_summary`` definitions.

Usage (on the VM, under the analysis package parent):
    cd <pkg_parent>
    sudo -n env PYTHONPATH=. python3 -m rq3.completion_figures_export \
        --out /tmp/completion_figures_data.json <run dirs...>

All quantities reuse the campaign analyzer definitions (slow = timeout or
latency > QOE_SLOW_S on service rows; start-up class from the per-backend
bind record).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rq3 import readiness_low_headroom as H
from rq3 import readiness_robustness as R
from rq3 import timing_figures_export as X

# wake `none`/`loss_all` ordinals 8..14 mirror completion blocks 1..7
WAKE_ORDINAL_BLOCK = {8: 1, 9: 2, 10: 3, 11: 4, 12: 5, 13: 6, 14: 7}


def _mode_block(run_dir: Path) -> tuple[str, str, int]:
    """(cell, arm, block) for both label families (timing and soundness)."""
    parts = R.timing_label_parts(run_dir.name)
    if parts is not None:
        cell, arm, block = parts
        return cell, arm, int(block.rstrip("r"))
    wake_parts = R.soundness_label_parts(run_dir.name)
    if wake_parts is not None:
        cell, arm, ordinal = wake_parts
        block = WAKE_ORDINAL_BLOCK.get(int(ordinal.rstrip("r")))
        if block is None:
            raise ValueError(f"wake ordinal outside 8..14: {run_dir.name}")
        return cell, arm, block
    raise ValueError(f"cannot parse label: {run_dir.name}")


def intact_record(run_dir: Path) -> dict[str, Any]:
    """Per-backend spawn -> first-success values with start-up class."""
    cell, arm, block = _mode_block(run_dir)
    if cell != "none":
        raise ValueError(f"not an intact (none-cell) run: {run_dir.name}")
    backends: list[dict[str, Any]] = []
    for record in R.completion_backend_records(run_dir):
        value = record.get("spawn_to_first_success_s")
        if value is None:
            continue
        backends.append({
            "container": record["container"],
            "stratum": record["stratum"],
            "bind_s": record["bind_s"],
            "s": value,
        })
    return {"block": block, "cell": cell, "arm": arm,
            "run": run_dir.name, "backends": backends}


def lost_record(run_dir: Path, bin_s: float, pre_s: float) -> dict[str, Any]:
    """Slow-share series plus admission accounting for one loss run."""
    cell, arm, block = _mode_block(run_dir)
    if cell != "loss_all":
        raise ValueError(f"not a loss_all run: {run_dir.name}")
    rows = H.request_rows(run_dir)
    plateau = [row for row in rows if row.get("phase") == H.PHASE]
    return {
        "block": block, "cell": cell, "arm": arm, "run": run_dir.name,
        "slow_full": R._qoe_status(plateau)["slow_rate"],
        "slow_onset": R.timing_onset_slow(run_dir),
        "slow_tail": R.timing_tail_slow(run_dir),
        "admissions": X.admission_summary(run_dir),
        "series": X.bin_series(run_dir, bin_s, pre_s),
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

    intact: dict[str, Any] = {}
    lost: dict[str, Any] = {}
    for path_str in args.run_dirs:
        path = Path(path_str)
        cell, arm, block = _mode_block(path)
        key = f"b{block}_{arm}"
        if cell == "none":
            if key in intact:
                raise ValueError(f"duplicate intact run for {key}")
            intact[key] = intact_record(path)
        else:
            if key in lost:
                raise ValueError(f"duplicate loss run for {key}")
            lost[key] = lost_record(path, args.bin, args.pre)
    output = {
        "family": "rq3_admission_modes",
        "kind": "figure-data export (companion; do not edit the frozen records)",
        "bin_s": args.bin,
        "pre_s": args.pre,
        "intact_runs": len(intact),
        "lost_runs": len(lost),
        "intact": intact,
        "lost": lost,
    }
    Path(args.out).write_text(json.dumps(output, indent=2, sort_keys=True),
                              encoding="utf-8")

    for mode in ("event_only", "hybrid", "reconcile", "wake_verify"):
        n = sum(len(r["backends"]) for k, r in intact.items()
                if r["arm"] == mode)
        print(f"intact {mode}: {n} backends")
        for k in sorted((k for k, r in lost.items() if r["arm"] == mode),
                        key=lambda k: lost[k]["block"]):
            adm = lost[k]["admissions"]
            print(f"  lost {k}: slow_full={lost[k]['slow_full']!r} "
                  f"admitted={adm['admitted']} abandoned={adm['abandoned']} "
                  f"src={adm['by_source']}")
    print(f"wrote {args.out} ({len(intact)} intact, {len(lost)} lost)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
