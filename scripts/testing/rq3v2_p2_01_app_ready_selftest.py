#!/usr/bin/env python3
"""rq3v2_p2_01_app_ready_selftest.py — RQ3 v2 direct-arm app_ready selftest.

Exercises ``source/sdn_controller/readiness_gate.py`` (event-driven direct):

- event arrives -> admission with ``admit_source="event"`` + row written;
- event lost -> safety-net probing admits on /ready after
  ``READINESS_EVENT_FALLBACK_S`` with ``admit_source="probe_fallback"``;
- within the event grace, direct mode does NOT probe (no probe before
  admission);
- no readiness within ``READINESS_PROBE_MAX_S`` -> abandoned (no leak);
- post-admission confirming probe runs and returns 200 (identity OK);
- discovery mode probes without the event grace (``admit_source="probe"``);
- admission-log schema carries the new ``admit_source`` column.

Exit non-zero on any assertion failure. Gate: `make rq3_app_ready_selftest`.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import sys
import tempfile
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
GATE_MOD = os.path.join(REPO, "source", "sdn_controller", "readiness_gate.py")


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _ProbeRecorder:
    """Replaces requests.get; records /ready probe URLs with a fake 200."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
        self.urls.append(url)
        return _FakeResponse(200)


def _load_gate():
    spec = importlib.util.spec_from_file_location("readiness_gate_under_test", GATE_MOD)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the @dataclass can resolve its own module.
    sys.modules["readiness_gate_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


def _rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    mod = _load_gate()
    recorder = _ProbeRecorder()
    mod.requests.get = recorder.get  # type: ignore[attr-defined]

    admitted: list = []
    abandoned: list = []

    def on_admit(pb):
        admitted.append(pb)

    def on_abandon(pb):
        abandoned.append(pb)

    tmp = tempfile.mkdtemp(prefix="rq3v2_app_ready_selftest_")
    adm_log = os.path.join(tmp, "admission_log.csv")

    def make_gate(propagation: str):
        return mod.ReadinessGate(
            propagation=propagation,
            probe_timeout_s=5.0,
            probe_max_s=120.0,
            probe_retry_s=1.0,
            discovery_interval_s=10.0,
            ready_port=5000,
            admission_log_path=adm_log,
            on_admit=on_admit,
            on_abandon=on_abandon,
            event_fallback_s=5.0,
        )

    now = time.time()

    def pb(mac: str, ip: str, spawn_complete_wall: float):
        return mod.PendingBackend(
            mac=mac, ip=ip, name=f"edge_{mac}", lan=1, network_id="lan1",
            ready_port=5000, spawn_started_wall_s=spawn_complete_wall - 15.0,
            spawn_complete_wall_s=spawn_complete_wall,
            spawn_started_mono_s=time.monotonic() - 15.0,
        )

    # ── 1. direct mode: event arrives -> event-driven admission ──
    gate = make_gate("direct")
    p1 = pb("aa:01", "10.0.0.50", now - 10.0)
    gate.enqueue(p1)
    ok = gate.admit_on_event("aa:01")
    _assert(ok, "admit_on_event admits a pending backend")
    _assert(p1.admit_source == "event", "admit_source = 'event' on event admission")
    _assert(p1.app_ready_wall_s is not None and p1.admitted_wall_s is not None,
            "event admission stamps app_ready + admitted")
    _assert(len(admitted) == 1, "on_admit invoked exactly once")
    # Confirm probe runs on the next worker pass and returns 200 (identity OK).
    gate._scan([])
    _assert(any("/ready" in u and "10.0.0.50" in u for u in recorder.urls),
            "post-admission confirming /ready probe executed")
    rows = _rows(adm_log)
    _assert(any(r["result"] == "admitted" and r["admit_source"] == "event"
                and r["container"] == "edge_aa:01" for r in rows),
            "admission row carries result=admitted + admit_source=event")
    _assert("admit_source" in rows[0], "admission-log schema includes admit_source")

    # ── 2. direct mode: within the event grace -> NO probing ──
    recorder.urls.clear()
    gate2 = make_gate("direct")
    p2 = pb("aa:02", "10.0.0.51", now - 1.0)   # 1 s after spawn_complete (< 5)
    gate2.enqueue(p2)
    gate2._scan([p2])
    _assert(p2.admitted_wall_s is None, "within grace: not admitted by probing")
    _assert(not any("aa" in u for u in recorder.urls),
            "within grace: no /ready probe before the event (no probe before admission)")

    # ── 3. direct mode: event lost -> safety net probes after the grace ──
    recorder.urls.clear()
    gate3 = make_gate("direct")
    p3 = pb("aa:03", "10.0.0.52", now - 10.0)  # 10 s after spawn_complete (> 5)
    gate3.enqueue(p3)
    gate3._scan([p3])
    _assert(p3.admitted_wall_s is not None, "safety net admits beyond the grace")
    _assert(p3.admit_source == "probe_fallback",
            "admit_source = 'probe_fallback' on safety-net admission")
    _assert(any("10.0.0.52" in u for u in recorder.urls),
            "safety net probed /ready")

    # ── 4. discovery mode: probes without the event grace ──
    recorder.urls.clear()
    gate_d = make_gate("discovery")
    p_d = pb("aa:04", "10.0.0.53", now - 1.0)
    gate_d.enqueue(p_d)
    gate_d._scan([p_d])
    _assert(p_d.admitted_wall_s is not None,
            "discovery admits immediately on the /ready scan (no grace)")
    _assert(p_d.admit_source == "probe", "admit_source = 'probe' in discovery mode")

    # ── 5. abandonment: never ready within probe_max -> no leak ──
    recorder.urls.clear()
    gate5 = make_gate("direct")
    p5 = pb("aa:05", "10.0.0.54", now - 130.0)  # > probe_max (120)
    gate5.enqueue(p5)
    gate5._scan([p5])
    _assert(len(abandoned) == 1, "on_abandon invoked for never-ready backend")
    _assert(p5.admitted_wall_s is None, "abandoned backend is never admitted")
    rows5 = _rows(adm_log)
    _assert(any(r["result"] == "abandoned" and not r.get("app_ready_ts")
                for r in rows5),
            "abandoned row written with empty app_ready_ts")

    # ── 6. event for an unknown MAC is ignored ──
    gate6 = make_gate("direct")
    _assert(gate6.admit_on_event("aa:99") is False,
            "admit_on_event for unknown MAC is a no-op")

    # ── 7. a late app_ready after abandonment does NOT admit ──
    _assert(gate5.admit_on_event("aa:05") is False,
            "app_ready after abandonment is ignored (no double admit)")
    _assert(all(r.get("result") != "admitted" or r.get("container") != "edge_aa:05"
                for r in _rows(adm_log)),
            "no admitted row for an abandoned backend")

    # ── 8. event before enqueue is replayed on enqueue (startup race) ──
    gate8 = make_gate("direct")
    _assert(gate8.admit_on_event("aa:08") is False,
            "event before enqueue is buffered (returns False)")
    p8 = pb("aa:08", "10.0.0.58", now - 10.0)
    gate8.enqueue(p8)
    _assert(p8.admitted_wall_s is not None and p8.admit_source == "event",
            "buffered event replayed on enqueue -> event-driven admission")

    # ── 9. discovery mode ignores app_ready events (cadence is the treatment) ──
    gate9 = make_gate("discovery")
    p9 = pb("aa:09", "10.0.0.59", now - 10.0)
    gate9.enqueue(p9)
    _assert(gate9.admit_on_event("aa:09") is False,
            "discovery mode ignores app_ready events")

    print("\nALL RQ3 V2 APP_READY ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
