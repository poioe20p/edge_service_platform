#!/usr/bin/env python3
"""
openloop_p1_01_driver_selftest.py — hard gate for the open-loop traffic driver.

Verifies, against a synthetic slow aiohttp endpoint (no netns required):

  Scenario A (production config):
      with L < CURL_MAX_TIME and INFLIGHT_WINDOW/rate > cap, the issued rate
      equals the configured rate (even when latency > interval) and ZERO
      requests are dropped — offered load is preserved under degradation.

  Scenario B (drop accounting, bounded override):
      with a small INFLIGHT_WINDOW and short CURL_MAX_TIME, the window
      provably fills; the first `dropped` appears and is counted separately.

  Scenario C (timeout separation):
      with a short CURL_MAX_TIME, requests are recorded as `timeout`
      (http_status="000", status="timeout") and are NOT counted as failures.

  Scenario D (drain):
      in-flight requests at a phase boundary are drained up to DRAIN_S and
      the remainder are recorded as `canceled`.

Exit code 0 = all scenarios pass; non-zero = gate failed.
Runs the REAL worker path (`traffic_generator._worker_main`) in-process.

Usage:
    python3 openloop_p1_01_driver_selftest.py
"""
import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import traffic_generator as tg  # noqa: E402


def _mk_snapshot(snap_dir: str) -> None:
    """Minimal real snapshot so the worker exercises the file-loading path."""
    content = [
        {"_id": "lan1::content::001", "region_origin": "lan1"},
        {"_id": "lan1::content::002", "region_origin": "lan1"},
        {"_id": "lan2::content::001", "region_origin": "lan2"},
        {"_id": "lan2::content::002", "region_origin": "lan2"},
    ]
    users = [
        {"_id": "lan1::user::001", "home_region": "lan1"},
        {"_id": "lan2::user::001", "home_region": "lan2"},
    ]
    with open(os.path.join(snap_dir, "content_items.json"), "w") as f:
        json.dump(content, f)
    with open(os.path.join(snap_dir, "user_profiles.json"), "w") as f:
        json.dump(users, f)


def _mk_schedule(sched_path: str, duration_s: int, rate: float) -> None:
    sched = {
        "base_seed": 42,
        "phases": [
            {
                "name": "episode",
                "duration_s": duration_s,
                "rate_per_client": rate,
                "cross_region_ratio": 0.0,
                "hotspot_direction": "bidirectional",
                "mix": {"content_lookup": 1.0},
                "client_fraction": 1.0,
            }
        ],
        "active_masks": [["test_client_1"]],
    }
    with open(sched_path, "w") as f:
        json.dump(sched, f)


def _worker_args(**overrides) -> argparse.Namespace:
    base = dict(
        schedule_file="", client_ns="test_client_1", client_lan="lan1",
        ns_index=0, vip="127.0.0.1:1", snapshot_dir="", output="",
        driver_mode="open_loop", in_flight_window=1024, drain_s=30.0,
        phase_state_file="", dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


async def _run_worker(args) -> int:
    return await tg._worker_main(args)


def _read_rows(path: str) -> list[list]:
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        assert header and header[-1] == "status", f"status column missing: {header}"
        return [r for r in reader if r]


async def _slow_server(port: int, latency_s: float, status: int = 200):
    """Run an aiohttp app with a /content/{cid} handler that sleeps latency_s."""
    import aiohttp
    from aiohttp import web

    async def handler(request):
        await asyncio.sleep(latency_s)
        return web.Response(text="ok", status=status,
                            headers={"X-Backend-ID": "n1"})

    app = web.Application()
    app.router.add_get("/content/{cid}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


async def scenario_a(tmp: str) -> None:
    """Issued rate preserved under latency > interval; zero dropped."""
    port = 9111
    runner = await _slow_server(port, latency_s=1.0)  # L=1.0s > interval
    try:
        snap_dir = os.path.join(tmp, "snap_a")
        os.makedirs(snap_dir)
        _mk_snapshot(snap_dir)
        sched_path = os.path.join(tmp, "sched_a.json")
        out = os.path.join(tmp, "out_a.csv")
        rate = 4.0
        dur = 10
        _mk_schedule(sched_path, dur, rate)
        args = _worker_args(
            schedule_file=sched_path, snapshot_dir=snap_dir, output=out,
            vip=f"127.0.0.1:{port}", in_flight_window=1024, drain_s=0.5,
            phase_state_file=os.path.join(tmp, "phase_a.txt"),
        )
        os.environ["CURL_MAX_TIME"] = "300"
        # latency 1.0s < CURL_MAX_TIME and the window never fills, so no drops here.
        rc = await _run_worker(args)
        assert rc == 0, f"worker rc={rc}"
        rows = _read_rows(out)
        issued = len(rows)
        expected = dur * rate
        assert abs(issued - expected) <= max(2, expected * 0.15), (
            f"Scenario A: issued {issued} != configured {expected}")
        dropped = [r for r in rows if r[-1] == "dropped"]
        timeout = [r for r in rows if r[-1] == "timeout"]
        completed = [r for r in rows if r[-1] == "completed"]
        canceled = [r for r in rows if r[-1] == "canceled"]
        assert not dropped, f"Scenario A: unexpected dropped={len(dropped)}"
        assert not timeout, f"Scenario A: unexpected timeout={len(timeout)}"
        # In-flight requests pending at phase end are drained and recorded as
        # canceled by design: not lost, and not counted as failures.
        assert len(completed) + len(canceled) == issued, (
            f"Scenario A: completed {len(completed)} + canceled {len(canceled)} "
            f"!= issued {issued}")
        print(f"  [PASS] A: issued={issued} (expected ~{expected}), "
              f"completed={len(completed)}, canceled={len(canceled)}, "
              f"dropped=0, timeout=0")
    finally:
        await runner.cleanup()


async def scenario_b(tmp: str) -> None:
    """Window binds -> dropped appears and is counted separately."""
    port = 9112
    runner = await _slow_server(port, latency_s=1.0)
    try:
        snap_dir = os.path.join(tmp, "snap_b")
        os.makedirs(snap_dir)
        _mk_snapshot(snap_dir)
        sched_path = os.path.join(tmp, "sched_b.json")
        out = os.path.join(tmp, "out_b.csv")
        rate = 20.0
        dur = 6
        _mk_schedule(sched_path, dur, rate)
        args = _worker_args(
            schedule_file=sched_path, snapshot_dir=snap_dir, output=out,
            vip=f"127.0.0.1:{port}", in_flight_window=2, drain_s=0.5,
            phase_state_file=os.path.join(tmp, "phase_b.txt"),
        )
        os.environ["CURL_MAX_TIME"] = "300"
        rc = await _run_worker(args)
        assert rc == 0, f"worker rc={rc}"
        rows = _read_rows(out)
        issued = len(rows)
        expected = dur * rate
        assert abs(issued - expected) <= max(2, expected * 0.15), (
            f"Scenario B: issued {issued} != configured {expected}")
        dropped = [r for r in rows if r[-1] == "dropped"]
        assert dropped, "Scenario B: expected dropped rows"
        assert len(dropped) + len([r for r in rows if r[-1] != "dropped"]) == issued
        print(f"  [PASS] B: issued={issued}, dropped={len(dropped)} "
              f"(window=2, L=1s => max completed ~12)")
    finally:
        await runner.cleanup()


async def scenario_c(tmp: str) -> None:
    """Timeout separated from failure (short cap override)."""
    port = 9113
    runner = await _slow_server(port, latency_s=0.5)
    try:
        snap_dir = os.path.join(tmp, "snap_c")
        os.makedirs(snap_dir)
        _mk_snapshot(snap_dir)
        sched_path = os.path.join(tmp, "sched_c.json")
        out = os.path.join(tmp, "out_c.csv")
        rate = 5.0
        dur = 4
        _mk_schedule(sched_path, dur, rate)
        args = _worker_args(
            schedule_file=sched_path, snapshot_dir=snap_dir, output=out,
            vip=f"127.0.0.1:{port}", in_flight_window=64, drain_s=0.5,
            phase_state_file=os.path.join(tmp, "phase_c.txt"),
        )
        os.environ["CURL_MAX_TIME"] = "0.2"  # every request times out
        rc = await _run_worker(args)
        assert rc == 0, f"worker rc={rc}"
        rows = _read_rows(out)
        timeout = [r for r in rows if r[-1] == "timeout"]
        completed = [r for r in rows if r[-1] == "completed"]
        assert timeout, "Scenario C: expected timeout rows"
        assert not completed, f"Scenario C: unexpected completed={len(completed)}"
        for r in timeout:
            assert r[8] == "000", f"Scenario C: timeout http_status={r[8]!r}"
            assert r[9] != "", "Scenario C: timeout latency_s should be elapsed"
        print(f"  [PASS] C: timeout={len(timeout)} (http_status=000, "
              f"status=timeout, not failures)")
    finally:
        await runner.cleanup()


async def scenario_d(tmp: str) -> None:
    """Drain: in-flight at phase end are awaited up to DRAIN_S then canceled."""
    port = 9114
    runner = await _slow_server(port, latency_s=2.0)  # > DRAIN_S
    try:
        snap_dir = os.path.join(tmp, "snap_d")
        os.makedirs(snap_dir)
        _mk_snapshot(snap_dir)
        sched_path = os.path.join(tmp, "sched_d.json")
        out = os.path.join(tmp, "out_d.csv")
        rate = 5.0
        dur = 3
        _mk_schedule(sched_path, dur, rate)
        args = _worker_args(
            schedule_file=sched_path, snapshot_dir=snap_dir, output=out,
            vip=f"127.0.0.1:{port}", in_flight_window=64, drain_s=0.3,
            phase_state_file=os.path.join(tmp, "phase_d.txt"),
        )
        os.environ["CURL_MAX_TIME"] = "300"
        rc = await _run_worker(args)
        assert rc == 0, f"worker rc={rc}"
        rows = _read_rows(out)
        canceled = [r for r in rows if r[-1] == "canceled"]
        completed = [r for r in rows if r[-1] == "completed"]
        assert canceled, "Scenario D: expected canceled rows after drain"
        assert completed, "Scenario D: expected some completed before drain"
        print(f"  [PASS] D: completed={len(completed)}, canceled={len(canceled)} "
              f"(drain={args.drain_s}s, L=2s)")
    finally:
        await runner.cleanup()


async def scenario_e(tmp: str) -> None:
    """Dry-run: no real requests are dispatched (header-only output)."""
    port = 9115
    runner = await _slow_server(port, latency_s=0.1)
    try:
        snap_dir = os.path.join(tmp, "snap_e")
        os.makedirs(snap_dir)
        _mk_snapshot(snap_dir)
        sched_path = os.path.join(tmp, "sched_e.json")
        out = os.path.join(tmp, "out_e.csv")
        rate = 5.0
        dur = 2
        _mk_schedule(sched_path, dur, rate)
        args = _worker_args(
            schedule_file=sched_path, snapshot_dir=snap_dir, output=out,
            vip=f"127.0.0.1:{port}", in_flight_window=64, drain_s=0.2,
            phase_state_file=os.path.join(tmp, "phase_e.txt"), dry_run=True,
        )
        os.environ["CURL_MAX_TIME"] = "300"
        rc = await _run_worker(args)
        assert rc == 0, f"worker rc={rc}"
        rows = _read_rows(out)
        assert not rows, f"Scenario E: dry-run must dispatch 0 requests, got {len(rows)}"
        print("  [PASS] E: dry-run dispatches zero requests (header-only)")
    finally:
        await runner.cleanup()


async def scenario_f(tmp: str) -> None:
    """CLI worker path via the real argparse entry (NO --config) — regression
    gate for the required-arg bug that broke every open_loop campaign: the
    supervisor spawns workers without --config, and a required=True --config
    killed them with rc=2 (argparse). Spawning through the CLI catches it;
    the in-process _worker_main path cannot."""
    port = 9116
    runner = await _slow_server(port, latency_s=0.05)
    try:
        snap_dir = os.path.join(tmp, "snap_f")
        os.makedirs(snap_dir)
        _mk_snapshot(snap_dir)
        sched_path = os.path.join(tmp, "sched_f.json")
        out = os.path.join(tmp, "out_f.csv")
        rate = 3.0
        dur = 2
        _mk_schedule(sched_path, dur, rate)
        env = dict(os.environ)
        env["CURL_MAX_TIME"] = "300"
        cmd = [sys.executable, os.path.abspath(tg.__file__),
               "--worker", "--client-ns", "test_client_1", "--client-lan", "lan1",
               "--ns-index", "0", "--vip", f"127.0.0.1:{port}",
               "--schedule-file", sched_path, "--snapshot-dir", snap_dir,
               "--output", out, "--driver-mode", "open_loop",
               "--in-flight-window", "64", "--drain-s", "0.2",
               "--phase-state-file", os.path.join(tmp, "phase_f.txt")]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert r.returncode == 0, (
            f"Scenario F: worker CLI rc={r.returncode} — "
            f"stderr={r.stderr[-400:]!r}")
        rows = _read_rows(out)
        assert rows, "Scenario F: worker CLI produced no rows"
        print(f"  [PASS] F: CLI worker path (no --config) rc=0, rows={len(rows)}")
    finally:
        await runner.cleanup()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="keep temp dir on failure for inspection")
    args = parser.parse_args()
    tmp = tempfile.mkdtemp(prefix="openloop_selftest_")
    failures = 0
    try:
        for name, fn in [
            ("scenario_a", scenario_a), ("scenario_b", scenario_b),
            ("scenario_c", scenario_c), ("scenario_d", scenario_d),
            ("scenario_e", scenario_e), ("scenario_f", scenario_f),
        ]:
            try:
                print(f"[RUN] {name}")
                await fn(tmp)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  [FAIL] {name}: {exc!r}")
    finally:
        if args.keep and failures:
            print(f"Temp dir kept: {tmp}")
        elif not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\nOPEN-LOOP SELF-TEST FAILED ({failures} scenario(s))")
        return 1
    print("\nOPEN-LOOP SELF-TEST PASSED (A: offered-load preserved, "
          "B: drop accounting, C: timeout separation, D: drain, "
          "E: dry-run dispatch-free, F: CLI worker path)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
