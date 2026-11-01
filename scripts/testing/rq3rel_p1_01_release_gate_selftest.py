#!/usr/bin/env python3
"""rq3rel_p1_01_release_gate_selftest.py — research_q3 release gate + log selftest.

Exercises the research_q3 release-mechanism gate contract
(``source/sdn_controller/release_gate.py``) and the release-event CSV logger
(``source/sdn_controller/release_log.py``):

- ``ReleaseGate("off")`` is inactive; unknown modes fall back to ``"off"``.
- Quarantine state machine (stabilized): begin → active → expired boundary
  (479.9 False / 480.0 True) → mark_finalized → dormant → notify_compute_scale_up
  → idle. ``recall()`` cancels only while ACTIVE (returns the canceled
  ``(mac, container)`` tuple), never while DORMANT (returns None).
- ``feed_window`` hysteresis: 5 False → False; 2 True → True; a single True
  among 5 slots → False.
- ``release_log.append_row`` header/row formation: temp path + "drained"
  mechanism → header written once + 2 rows; mechanism "off" → no file created.

Exit non-zero on any assertion failure. Runs on the Windows host with .venv
(plain stdlib + sys.path insert). Gate: local validation only.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Import path: this file lives at <repo>/source/scripts/testing/, so
# parents[2] is <repo>/source; append sdn_controller to it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdn_controller"))

import release_gate  # noqa: E402
import release_log  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


def main() -> int:
    # Pin the production defaults so a host env override cannot skew the
    # clock/boundary assertions below (RELEASE_STABILIZE_S / RELEASE_RECALL_K
    # are read from the environment by scaling_config).
    release_gate._RELEASE_STABILIZE_S = 480.0
    release_gate._RELEASE_RECALL_K = 2

    # ── mode selection ──
    _assert(release_gate.ReleaseGate("off").active is False,
            "ReleaseGate('off') is inactive")
    _assert(release_gate.ReleaseGate("drained").active is True,
            "ReleaseGate('drained') is active")
    _assert(release_gate.ReleaseGate("bogus").mode == "off",
            "ReleaseGate('bogus') falls back to mode 'off'")

    # ── quarantine state machine (stabilized) ──
    gate = release_gate.ReleaseGate("stabilized")
    t0 = 1000.0
    _assert(gate.quarantine_begin("aa:bb:cc:dd:ee:01", "edge_server_lan1_dyn1", t0),
            "quarantine_begin IDLE -> ACTIVE returns True")
    _assert(gate.quarantine_active(), "quarantine ACTIVE after begin")
    _assert(gate.quarantine_expired(t0 + 479.9) is False,
            "quarantine NOT expired at +479.9 s")
    _assert(gate.quarantine_expired(t0 + 480.0) is True,
            "quarantine expired at +480.0 s")
    gate.mark_finalized()
    _assert(gate.dormant(), "DORMANT after mark_finalized")
    _assert(gate.quarantine_active() is False, "not ACTIVE while DORMANT")
    gate.notify_compute_scale_up()
    _assert(gate.dormant() is False and gate.quarantine_active() is False,
            "IDLE after notify_compute_scale_up")

    # recall while ACTIVE cancels; recall while DORMANT does not.
    _assert(gate.quarantine_begin("aa:bb:cc:dd:ee:02", "edge_server_lan1_dyn2",
                                  t0 + 100.0), "second quarantine begins")
    _recalled = gate.recall()
    _assert(_recalled is not None and _recalled == ("aa:bb:cc:dd:ee:02",
                                                    "edge_server_lan1_dyn2"),
            "recall() cancels an ACTIVE quarantine and returns its identity")
    _assert(gate.quarantine_active() is False, "IDLE after recall")
    _assert(gate.quarantine_begin("aa:bb:cc:dd:ee:03", "edge_server_lan1_dyn3",
                                  t0 + 200.0), "third quarantine begins")
    gate.mark_finalized()
    _assert(gate.recall() is None, "recall() refuses while DORMANT")
    _assert(gate.dormant(), "still DORMANT after refused recall")
    gate.notify_compute_scale_up()

    # ── feed_window hysteresis ──
    gw = release_gate.ReleaseGate("stabilized")
    _assert(all(gw.feed_window(False) is False for _ in range(5)),
            "feed_window: 5 False -> never triggers")
    _assert(gw.feed_window(True) is False,
            "feed_window: first True among 5 -> still below K")
    _assert(gw.feed_window(True) is True,
            "feed_window: second True -> recall signal")
    gw2 = release_gate.ReleaseGate("stabilized")
    for _ in range(5):
        gw2.feed_window(False)
    _assert(gw2.feed_window(True) is False,
            "feed_window: a single True among 5 -> no recall signal")

    # ── release_log header/row formation ──
    tmp = tempfile.mkdtemp(prefix="rq3rel_gate_selftest_")
    try:
        _orig_path = release_log._RELEASE_LOG_PATH
        _orig_mech = release_log._RELEASE_MECHANISM
        try:
            log_path = os.path.join(tmp, "release_log.csv")
            release_log._RELEASE_LOG_PATH = log_path
            release_log._RELEASE_MECHANISM = "drained"
            release_log.append_row(
                network_id="lan1", mechanism="drained", tier="compute",
                container="edge_server_lan1_dyn1", mac="aa:bb:cc:dd:ee:01",
                trigger="scale_down", event="quarantine_begin", success="1",
                reason="")
            release_log.append_row(
                network_id="lan1", mechanism="drained", tier="compute",
                container="edge_server_lan1_dyn1", mac="aa:bb:cc:dd:ee:01",
                trigger="overload_recall", event="recall", success="1",
                reason="")
            _assert(os.path.exists(log_path), "release log file created")
            with open(log_path, encoding="utf-8") as fh:
                lines = fh.readlines()
            _assert(sum(1 for l in lines if l.startswith("ts,network_id")) == 1,
                    "header written exactly once")
            _assert(len(lines) == 3, "header + 2 data rows")
            _assert(lines[1].count(",") >= 10 and lines[2].count(",") >= 10,
                    "rows carry the full column set")

            # mechanism "off" -> append_row is a no-op, no file created.
            off_path = os.path.join(tmp, "release_log_off.csv")
            release_log._RELEASE_LOG_PATH = off_path
            release_log._RELEASE_MECHANISM = "off"
            release_log.append_row(
                network_id="lan1", mechanism="off", tier="compute",
                container="edge_server_lan1_dyn1", mac="aa:bb:cc:dd:ee:01",
                trigger="scale_down", event="end", success="1", reason="")
            _assert(not os.path.exists(off_path),
                    "no file created when mechanism is 'off'")
        finally:
            release_log._RELEASE_LOG_PATH = _orig_path
            release_log._RELEASE_MECHANISM = _orig_mech
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nSELFTEST PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
