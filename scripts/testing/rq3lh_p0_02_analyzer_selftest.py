#!/usr/bin/env python3
"""Small synthetic selftest for the RQ3 low-headroom analyzer."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source/scripts/testing/analysis"))

from rq3 import readiness_low_headroom as analysis  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    check(analysis.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5,
          "linear percentile")
    rows = [
        {"status": "completed", "http_status": "200", "latency_s": 0.010},
        {"status": "completed", "http_status": "500", "latency_s": 0.001},
        {"status": "timeout", "http_status": "000", "latency_s": None},
        {"status": "canceled", "http_status": "000", "latency_s": None},
    ]
    check(sum(analysis.is_success(row) for row in rows) == 1, "status classification")
    check(analysis.label_key("rq3lh_direct_1") == "1", "run label pairing")
    check(analysis.label_key("rq3lh_event_absent_2") == "2", "absence label pairing")
    check(analysis.label_key("unrelated") is None, "unrelated label rejection")
    print("RQ3 low-headroom analyzer selftest: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
