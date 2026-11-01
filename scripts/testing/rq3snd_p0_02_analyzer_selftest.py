#!/usr/bin/env python3
"""Standalone selftest for the RQ3 soundness analyzer surface
(`readiness_robustness.py` subcommands: soundness-damage, soundness-preflight,
soundness-crossover).

Self-contained on purpose (mirrors rq3tim_p0_02_analyzer_selftest.py): the
rq3snd family must not depend on any other family's selftest. Builds real
fixture run folders (client_requests.csv, phases_snapshot.json, env snapshot,
fault attestation, service logs with READINESS_CLAIM/READINESS_BIND markers,
admission logs, controller logs) and asserts the exact plan numbers from
docs/operation/testing/experiment/v3/rq3_soundness/{experiment_plan.md,preflight_campaign.md}:

- class priority boundaries: timeout beats its http_status=000 encoding; the
  000 / 5xx / slow / good boundaries; canceled/dropped excluded from
  offered and U.
- F2/F3/F1 window extraction: t_bind from the `edge-server listening` app log;
  F2 = [t_bind - N - 10, t_bind + 30]; F3 = [t_bind, min(t_bind + S,
  plateau_end)]; F1 = full plateau.
- D_s / D-bar math incl. the none-floor breach exclusion (lost seeds).
- crossover: finite N*, censored (push-advantage-through-N10),
  non-identifiable fit, stop_null / sub-threshold / growth-failure branches,
  NA handling.
- R1-R3/R3b preflight gate thresholds, lock JSON (S = 1200), and the
  DIAGNOSE (2) vs STOP (3) exit codes.
- class-based `000` counting (a timeout carrying http_status=000 never counts
  as the 000 class nor trips R1(a)/(b), R2(a), R3(c) or H-S1 F2 counts);
  `--rerun-attempted` persisting-DIAGNOSE -> STOP; R1 §4.5 floor gating;
  H-S2 same-seed joint pattern; exact crossover band mapping; report-only
  spawn cross-check; exit-3 analyzer errors; `r`-suffix rerun labels.

Exit non-zero on any assertion failure.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source/scripts/testing/analysis"))

from rq3 import readiness_robustness as robustness  # noqa: E402

# Synthetic timeline: one 600 s compute_plateau starting at T0 + 100.
T0 = 1_790_000_000.0
PLATEAU_START = T0 + 100.0
PLATEAU_END = PLATEAU_START + 600.0

CLIENT_FIELDS = [
    "sent_at", "phase", "client_ns", "client_lan", "endpoint", "content_id",
    "user_id", "target_region", "http_status", "latency_s", "completed_at",
    "backend_id", "source_port", "status",
]
ADMISSION_FIELDS = [
    "ts", "network_id", "lan", "container", "mac", "ip", "mode", "result",
    "spawn_started_ts", "spawn_complete_ts", "probe_first_ts", "app_ready_ts",
    "admitted_ts", "admit_source",
]


def check(condition: bool, label: str) -> None:
    if not condition:
        print(f"FAIL: {label}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {label}")


def near(value, target, tolerance=1e-6) -> bool:
    return value is not None and abs(value - target) <= tolerance


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _log_line(ts: float, message: str) -> str:
    docker = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f") + "000Z"
    pytime = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S,") + f"{int((ts % 1) * 1000):03d}"
    return f"{docker} {pytime} [MainThread] INFO {message}"


def client_row(ts: float, phase: str, lan: str, http: str, latency: float | None,
               status: str, backend: str = "") -> dict:
    sent = _iso(ts)
    return {
        "sent_at": sent, "phase": phase, "client_ns": f"{lan}_client_1",
        "client_lan": lan, "endpoint": "service_pressure", "content_id": "",
        "user_id": "", "target_region": lan, "http_status": http,
        "latency_s": "" if latency is None else f"{latency:.4f}",
        "completed_at": sent, "backend_id": backend, "source_port": 0,
        "status": status,
    }


def filler_rows(count: int, start: float, end: float, lan: str) -> list[dict]:
    span = (end - start) / max(count, 1)
    return [client_row(start + span * (i + 0.5), "compute_plateau", lan,
                       "200", 0.05, "completed")
            for i in range(count)]


def admission(lan: int, container: str, admitted_ts: float, *,
              source: str = "probe", result: str = "admitted") -> dict:
    return {
        "ts": f"{admitted_ts:.3f}", "network_id": f"lan{lan}", "lan": str(lan),
        "container": container, "mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.8",
        "mode": "discovery" if source == "probe" else "direct",
        "result": result, "spawn_started_ts": f"{admitted_ts - 1.5:.3f}",
        "spawn_complete_ts": f"{admitted_ts - 1.0:.3f}",
        "probe_first_ts": f"{admitted_ts - 0.5:.3f}",
        "app_ready_ts": f"{admitted_ts - 0.2:.3f}",
        "admitted_ts": f"{admitted_ts:.3f}", "admit_source": source,
    }


def spawn_containers(spawns: list[tuple[int, float, float | None]]) -> list[str]:
    return [f"edge_server_lan{lan}_dyn{index + 2}"
            for index, (lan, _, _) in enumerate(spawns)]


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_run(base: Path, name: str, *, arm: str, seed: int, n: int, s_lie: int,
              spawns: list[tuple[int, float, float | None]], rows: list[dict],
              admissions: list[dict], violations: tuple[int, int] = (0, 0),
              quota: str = "0.12") -> Path:
    run = base / name
    (run / "service_logs").mkdir(parents=True, exist_ok=True)
    (run / "controller_env_snapshot.env").write_text(
        "# fixture env snapshot\n"
        f"EDGE_READY_PREMATURE_S={n}\n"
        f"EDGE_READY_SEMANTIC_LIE_S={s_lie}\n", encoding="utf-8")
    (run / "rq3snd_fault_attestation.txt").write_text(
        f"label={name}\n"
        f"EDGE_READY_PREMATURE_S={n}\n"
        f"EDGE_READY_SEMANTIC_LIE_S={s_lie}\n", encoding="utf-8")
    (run / "phases_snapshot.json").write_text(json.dumps({"phases": [
        {"name": "baseline", "duration_s": 60, "rate_per_client": 1.0},
        {"name": "compute_plateau", "duration_s": 600, "rate_per_client": 2.0,
         "service_pressure": 1.0},
        {"name": "recovery_gap", "duration_s": 120, "rate_per_client": 0.5},
    ]}), encoding="utf-8")
    (run / "open_loop_schedule.json").write_text(
        json.dumps({"base_seed": seed}), encoding="utf-8")
    (run / "quota_snapshot.json").write_text(json.dumps({
        "requested_edge_cpus": quota, "all_compute_containers_match": True,
    }), encoding="utf-8")
    for index, (lan, t_listen, t_claim) in enumerate(spawns):
        container = f"edge_server_lan{lan}_dyn{index + 2}"
        lines = []
        if t_claim is not None:
            lines.append(_log_line(t_claim,
                                   f"READINESS_CLAIM lan=lan{lan} premature_s={n} "
                                   f"claimed=1 ts={t_claim:.3f}"))
            lines.append(_log_line(t_listen,
                                   f"READINESS_BIND lan=lan{lan} premature_s={n} "
                                   f"bind_ts={t_listen:.3f} "
                                   f"deferral_s={t_listen - t_claim:.3f}"))
        lines.append(_log_line(t_listen,
                               "edge-server listening: http://0.0.0.0:5000"))
        (run / "service_logs" / f"{container}.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    for lan in (1, 2):
        lan_rows = [row for row in admissions if row["lan"] == str(lan)]
        _write_csv(run / f"admission_log_lan{lan}.csv", ADMISSION_FIELDS, lan_rows)
    # Anchor rows pin the driver-labeled plateau bounds to [PS, PE] exactly
    # (phase_bounds derives bounds from sent_at min/max).
    rows = list(rows) + [
        client_row(PLATEAU_START, "compute_plateau", "lan1", "200", 0.05,
                   "completed"),
        client_row(PLATEAU_END, "compute_plateau", "lan1", "200", 0.05,
                   "completed"),
    ]
    _write_csv(run / "client_requests.csv", CLIENT_FIELDS, rows)
    violation_line = ("[warn] [readiness] post-admission identity "
                      "CHECK VIOLATION name=x mac=y\n")
    (run / "controller_lan1.log").write_text(violation_line * violations[0],
                                             encoding="utf-8")
    (run / "controller_lan2.log").write_text(violation_line * violations[1],
                                             encoding="utf-8")
    return run


def build_premature_fault(base: Path, seed: int, bad: int, ordinal: str) -> Path:
    t_bind = PLATEAU_START + 60.0
    t_claim = t_bind - 10.0
    spawns = [(1, t_bind, t_claim), (2, t_bind, t_claim)]
    rows = []
    for i in range(99):
        ts = PLATEAU_START + 41.0 + i * 0.48
        http = "000" if i < bad else "200"
        latency = 0.01 if i < bad else 0.05
        rows.append(client_row(ts, "compute_plateau", "lan1", http, latency,
                               "completed"))
    rows.append(client_row(t_bind - 20.0, "compute_plateau", "lan1", "000",
                           0.01, "completed"))  # window start (inclusive)
    rows.append(client_row(t_bind + 30.0, "compute_plateau", "lan1", "000",
                           0.01, "completed"))  # window end (exclusive)
    rows += filler_rows(10, PLATEAU_START + 150, PLATEAU_START + 160, "lan1")
    rows.append(client_row(PLATEAU_END + 50.0, "recovery_gap", "lan1", "000",
                           300.0, "timeout"))
    return build_run(base, f"rq3snd_premature10_event_only_{ordinal}",
                     arm="event_only", seed=seed, n=10, s_lie=0, spawns=spawns,
                     rows=rows, admissions=[])


def build_none_damage(base: Path, seed: int, ordinal: str,
                      breach: bool = False) -> Path:
    t_bind = PLATEAU_START + 60.0
    rows = [client_row(PLATEAU_START + 41.0 + i * 0.48, "compute_plateau",
                       "lan1", "200", 0.05, "completed") for i in range(99)]
    rows.append(client_row(t_bind - 20.0, "compute_plateau", "lan1", "200",
                           0.05, "completed"))
    rows.append(client_row(t_bind + 30.0, "compute_plateau", "lan1", "200",
                           0.05, "completed"))
    rows += filler_rows(10, PLATEAU_START + 150, PLATEAU_START + 160, "lan1")
    rows += filler_rows(30, PLATEAU_END - 200, PLATEAU_END - 170, "lan1")
    if breach:
        for i in range(4):
            rows.append(client_row(PLATEAU_END - 200.0 + i, "compute_plateau",
                                   "lan1", "200", 1.5, "completed"))
    return build_run(base, f"rq3snd_none_event_only_{ordinal}", arm="event_only",
                     seed=seed, n=0, s_lie=0, spawns=[(1, t_bind, None)],
                     rows=rows, admissions=[])


def build_semantic_damage(base: Path, seed: int, ordinal: str) -> Path:
    t_bind = PLATEAU_START + 10.0
    container = "edge_server_lan1_dyn2"
    rows = [client_row(PLATEAU_START + 5.0, "compute_plateau", "lan1", "200",
                       0.05, "completed")]
    for i in range(9):
        rows.append(client_row(PLATEAU_START + 20.0 + i * 2.0, "compute_plateau",
                               "lan1", "503", 0.05, "completed",
                               backend=container))
    rows.append(client_row(PLATEAU_START + 45.0, "compute_plateau", "lan1",
                           "000", 300.0, "timeout", backend=container))
    for i in range(10):
        rows.append(client_row(PLATEAU_START + 60.0 + i * 2.0, "compute_plateau",
                               "lan1", "200", 0.05, "completed",
                               backend=container))
    rows.append(client_row(PLATEAU_END + 50.0, "recovery_gap", "lan1", "200",
                           0.05, "completed"))
    return build_run(base, f"rq3snd_semantic_event_only_{ordinal}",
                     arm="event_only", seed=seed, n=0, s_lie=1200,
                     spawns=[(1, t_bind, None)], rows=rows, admissions=[])


def build_r1(base: Path, *, deferral: float = 10.0, window_bad: int = 12,
             window_timeouts: int = 0, other_000: int = 20,
             success_after_admit: tuple = (10.4, 10.6),
             violations: tuple = (2, 1), ordinal: str = "90",
             seed: int = 6351, filler_per_lan: int = 2600,
             quota: str = "0.12") -> Path:
    t_bind = PLATEAU_START + 60.0
    t_claim = t_bind - deferral
    spawns = [(1, t_bind, t_claim), (2, t_bind, t_claim)]
    containers = spawn_containers(spawns)
    rows = []
    for lan_no, offset in zip((1, 2), success_after_admit):
        lan = f"lan{lan_no}"
        for i in range(window_bad):
            frac = (i + 0.5) / window_bad
            rows.append(client_row(t_claim + frac * deferral, "compute_plateau",
                                   lan, "000", 0.01, "completed"))
        for i in range(window_timeouts):
            # Driver timeouts also carry http_status="000": §4.1 timeout
            # class, and must never inflate the `000` counters.
            frac = (i + 0.5) / max(window_timeouts, 1)
            rows.append(client_row(t_claim + frac * deferral, "compute_plateau",
                                   lan, "000", 300.0, "timeout"))
        for i in range(other_000):
            rows.append(client_row(PLATEAU_START + 200.0 + i * 2.0,
                                   "compute_plateau", lan, "000", 0.01,
                                   "completed"))
        rows.append(client_row(t_claim + 0.001 + offset, "compute_plateau", lan,
                               "200", 0.05, "completed",
                               backend=containers[lan_no - 1]))
    # Default filler meets the §4.5 floors (>=5,000 pooled / >=1,000 per LAN /
    # F2 window >=15 per LAN); tests override it to breach a floor.
    rows += filler_rows(filler_per_lan, PLATEAU_START + 1, PLATEAU_END - 1,
                        "lan1")
    rows += filler_rows(filler_per_lan, PLATEAU_START + 1, PLATEAU_END - 1,
                        "lan2")
    admissions = [admission(1, containers[0], t_claim + 0.001, source="event"),
                  admission(2, containers[1], t_claim + 0.001, source="event")]
    return build_run(base, f"rq3snd_premature10_event_only_{ordinal}",
                     arm="event_only", seed=seed, n=10, s_lie=0, spawns=spawns,
                     rows=rows, admissions=admissions, violations=violations,
                     quota=quota)


def build_r2(base: Path, *, zero_rows: int = 1, timeout_rows: int = 0,
             success_after_claim: tuple = (11.3, 11.5), ordinal: str = "91",
             seed: int = 6352) -> Path:
    t_bind = PLATEAU_START + 60.0
    t_claim = t_bind - 10.0
    spawns = [(1, t_bind, t_claim), (2, t_bind, t_claim)]
    containers = spawn_containers(spawns)
    rows = []
    for lan_no, offset in zip((1, 2), success_after_claim):
        rows.append(client_row(t_claim + offset, "compute_plateau",
                               f"lan{lan_no}", "200", 0.05, "completed",
                               backend=containers[lan_no - 1]))
    for i in range(zero_rows):
        rows.append(client_row(PLATEAU_START + 150.0 + i, "compute_plateau",
                               "lan1", "000", 0.01, "completed"))
    for i in range(timeout_rows):
        # http_status="000" + status="timeout": timeout class, not `000`.
        rows.append(client_row(PLATEAU_START + 130.0 + i, "compute_plateau",
                               "lan1", "000", 300.0, "timeout"))
    rows += filler_rows(400, PLATEAU_START + 1, PLATEAU_END - 1, "lan1")
    rows += filler_rows(400, PLATEAU_START + 1, PLATEAU_END - 1, "lan2")
    admissions = [admission(1, containers[0], t_bind + 0.001, source="probe"),
                  admission(2, containers[1], t_bind + 0.001, source="probe")]
    return build_run(base, f"rq3snd_premature10_reconcile_{ordinal}",
                     arm="reconcile", seed=seed, n=10, s_lie=0, spawns=spawns,
                     rows=rows, admissions=admissions)


def build_r3(base: Path, *, t_bind_offset: float = 60.0, fivexx: int = 18,
             per_lan: int = 20, transport_000: int = 3, window_timeouts: int = 0,
             ordinal: str = "92",
             seed: int = 6353, arm: str = "event_only") -> Path:
    t_bind = PLATEAU_START + t_bind_offset
    spawns = [(1, t_bind, None), (2, t_bind, None)]
    containers = spawn_containers(spawns)
    rows = []
    for lan_no in (1, 2):
        lan = f"lan{lan_no}"
        container = containers[lan_no - 1]
        for i in range(per_lan):
            http = "503" if i < fivexx else "200"
            rows.append(client_row(t_bind + 10.0 + i * 2.0, "compute_plateau",
                                   lan, http, 0.05, "completed",
                                   backend=container))
    for i in range(transport_000):
        rows.append(client_row(t_bind + 100.0 + i, "compute_plateau", "lan1",
                               "000", 0.01, "completed"))
    for i in range(window_timeouts):
        # In-window driver timeouts encode http_status="000" but stay in the
        # timeout class: they must not inflate R3(c)'s transport-000 share.
        rows.append(client_row(t_bind + 300.0 + i, "compute_plateau", "lan1",
                               "000", 300.0, "timeout"))
    rows += filler_rows(400, PLATEAU_START + 1, PLATEAU_END - 1, "lan1")
    rows += filler_rows(400, PLATEAU_START + 1, PLATEAU_END - 1, "lan2")
    return build_run(base, f"rq3snd_semantic_{arm}_{ordinal}", arm=arm,
                     seed=seed, n=0, s_lie=1200, spawns=spawns, rows=rows,
                     admissions=[])


def build_r3b(base: Path, **kwargs) -> Path:
    return build_r3(base, ordinal="93", seed=6354, arm="reconcile", **kwargs)


def run_preflight(base: Path, tag: str, *, r1: dict | None = None,
                  r2: dict | None = None, r3: dict | None = None,
                  r3b: dict | None = None,
                  rerun_attempted: bool = False) -> tuple[int, dict, Path]:
    directory = base / tag
    directory.mkdir(parents=True, exist_ok=True)
    runs = [build_r1(directory, **(r1 or {})),
            build_r2(directory, **(r2 or {})),
            build_r3(directory, **(r3 or {}))]
    if r3b is not False:
        runs.append(build_r3b(directory, **(r3b or {})))
    out = base / f"preflight_{tag}.json"
    lock = base / f"preflight_{tag}_lock.json"
    code = robustness.soundness_preflight_command(argparse.Namespace(
        run_dir=[str(path) for path in runs], out=str(out), lock_out=str(lock),
        rerun_attempted=rerun_attempted))
    return code, json.loads(out.read_text(encoding="utf-8")), lock


def write_crossover_damage(path: Path, *, push: dict | None = None,
                           pull: dict | None = None,
                           extra_contrasts: list | None = None,
                           runs: list | None = None,
                           seeds: tuple = (6301, 6302, 6303)) -> None:
    contrasts = []
    for seed in seeds:
        for cell, n in (("premature2", 2), ("premature5", 5),
                        ("premature10", 10)):
            if push:
                contrasts.append({"arm": "event_only", "cell": cell,
                                  "seed": seed, "n": n, "d_s": push[n],
                                  "status": "ok"})
            if pull:
                contrasts.append({"arm": "reconcile", "cell": cell,
                                  "seed": seed, "n": n, "d_s": pull[n],
                                  "status": "ok"})
    contrasts.extend(extra_contrasts or [])
    path.write_text(json.dumps({
        "family": "rq3_soundness", "contrasts": contrasts,
        "runs": runs or [],
    }), encoding="utf-8")


def run_crossover(base: Path, tag: str, **kwargs) -> tuple[int, dict]:
    damage = base / f"damage_{tag}.json"
    write_crossover_damage(damage, **kwargs)
    out = base / f"crossover_{tag}.json"
    code = robustness.soundness_crossover_command(argparse.Namespace(
        damage_json=str(damage), out=str(out)))
    return code, json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        rows = [
            {"status": "timeout", "http_status": "000", "latency_s": "300.0"},
            {"status": "completed", "http_status": "000", "latency_s": "0.01"},
            {"status": "completed", "http_status": "503", "latency_s": "0.02"},
            {"status": "completed", "http_status": "200", "latency_s": "1.0001"},
            {"status": "completed", "http_status": "200", "latency_s": "1.0"},
            {"status": "completed", "http_status": "200", "latency_s": "0.5"},
            {"status": "canceled", "http_status": "", "latency_s": ""},
            {"status": "dropped", "http_status": "", "latency_s": ""},
        ]
        unit = robustness.soundness_u(rows)
        check(robustness.soundness_class_of(rows[0]) == "timeout",
              "class priority: timeout beats its http_status=000 encoding")
        check(unit["classes"] == {"timeout": 1, "000": 1, "fail5xx": 1,
                                  "slow": 1, "good": 2},
              "class boundaries: 000 / 5xx / slow(>1.0) / good(<=1.0)")
        check(unit["offered"] == 6 and near(unit["u_pct"], 66.6666667, 1e-6),
              "U = 100 * bad / offered (canceled/dropped excluded)")
        check(unit["cancelled"] == 1 and unit["dropped"] == 1,
              "canceled/dropped reported separately")
        check(robustness.soundness_label_parts("rq3snd_premature10_event_only_90r")
              == ("premature10", "event_only", "90r"),
              "SOUNDNESS_LABEL_RE accepts the optional rerun r-suffix (…_90r)")

        damage_dir = base / "damage"
        damage_dir.mkdir()
        records = []
        for seed, bad, ordinal in ((6301, 9, "1"), (6302, 19, "2"), (6303, 29, "3")):
            records.append(build_premature_fault(damage_dir, seed, bad, ordinal))
            records.append(build_none_damage(damage_dir, seed, ordinal,
                                             breach=(seed == 6303)))
        records.append(build_semantic_damage(damage_dir, 6301, "1"))
        # Pre-registered rerun label (trailing `r`) must parse end-to-end.
        records.append(build_premature_fault(damage_dir, 6399, 9, "9r"))

        fault = robustness.soundness_run_metrics(
            damage_dir / "rq3snd_premature10_event_only_1")
        check(near(fault["f2"]["u_by_n"]["10"]["u_pct"], 10.0, 1e-9),
              "F2 window U=10.0 (boundary start included, end excluded)")
        check(fault["f2"]["u_by_n"]["10"]["offered"] == 100
              and fault["f2"]["u_by_n"]["10"]["classes"]["000"] == 10
              and fault["f2"]["u_by_n"]["10"]["classes"]["good"] == 90,
              "F2 window extraction: 100 offered, 10 bad / 90 good")
        check(near(fault["spawns"][0]["deferral_s"], 10.0, 1e-9),
              "t_bind/t_claim extraction: measured deferral = N = 10 s")

        none_rec = robustness.soundness_run_metrics(
            damage_dir / "rq3snd_none_event_only_1")
        check(none_rec["u"]["f1_pct"] == 0.0,
              "F1 = full plateau only (recovery-phase row excluded)")
        check(none_rec["none_health"]["breach"] is False,
              "none-cell healthy (tail slow 0 pp, bad 0 pp)")
        breach_rec = robustness.soundness_run_metrics(
            damage_dir / "rq3snd_none_event_only_3")
        check(breach_rec["none_health"]["breach"] is True,
              "none-tail slow >2 pp -> none-floor breach")

        semantic = robustness.soundness_run_metrics(
            damage_dir / "rq3snd_semantic_event_only_1")
        check(near(semantic["f3"]["coverage"], 59.0 / 60.0, 1e-9),
              "F3 window clipped to plateau: coverage = 590/600 s")
        check(near(semantic["f3"]["u"]["u_pct"], 50.0, 1e-9),
              "F3 U=50.0 with timeout-first split (1 timeout + 9 5xx + 10 good)")
        check(semantic["f3"]["u"]["classes"]["timeout"] == 1
              and semantic["f3"]["u"]["classes"]["fail5xx"] == 9,
              "class split in F3 window: timeout not swallowed by 000")

        # Class-based 000 counting: timeouts encode http_status="000" but must
        # stay out of R1(a)/R1(b)/H-S1 F2 counters.
        r1_timeout = build_r1(base / "r1_timeout", window_timeouts=3)
        r1t = robustness.soundness_run_metrics(r1_timeout)
        pre = r1t["f2"]["pre_by_n"]["10"]["http000"]
        check(pre["lan1"] == 12 and pre["lan2"] == 12
              and r1t["run_wide"]["http000"] == 64
              and r1t["run_wide"]["classes"]["timeout"] == 6,
              "class-based 000: 6 in-window timeouts (http 000) excluded from "
              "R1(a)/R1(b)/H-S1 F2 counts")
        check(fault["spawns_found"] == 2 and fault["spawns_admission_rows"] == 0
              and fault["spawns_mismatch"] is True,
              "spawn cross-check reports mismatch (2 logs vs 0 admission rows)")

        damage_out = base / "soundness_damage.json"
        code = robustness.soundness_damage_command(argparse.Namespace(
            run_dir=[str(path) for path in records], out=str(damage_out)))
        damage = json.loads(damage_out.read_text(encoding="utf-8"))
        check(code == 0 and not [r for r in damage["runs"] if "error" in r],
              "soundness-damage runs over all fixtures")
        check(any(run.get("ordinal") == "9r" for run in damage["runs"]),
              "soundness-damage accepts an r-suffixed rerun label")
        by_seed = {(entry["arm"], entry["cell"], entry["seed"]): entry
                   for entry in damage["contrasts"]}
        check(near(by_seed[("event_only", "premature10", 6301)]["d_s"], 10.0, 1e-9),
              "D_s seed 6301 = U(cell) - U(none) = 10 - 0")
        check(near(by_seed[("event_only", "premature10", 6302)]["d_s"], 20.0, 1e-9),
              "D_s seed 6302 = 20.0")
        check(by_seed[("event_only", "premature10", 6303)]["status"]
              == "none_breach",
              "breached none seed excluded from D_s (NA policy §4.5)")
        bucket = damage["medians"]["event_only|premature10"]
        check(bucket["n_usable"] == 2 and near(bucket["d_bar_pp"], 15.0, 1e-9),
              "D-bar = median(10, 20) = 15.0 over usable seeds")
        check(near(by_seed[("event_only", "semantic", 6301)]["d_s"], 50.0, 1e-9),
              "semantic D_s = 50.0 (matched fixed-S window)")

        code, rep = run_crossover(base, "finite", push={2: 10, 5: 20, 10: 30},
                                  pull={2: 14, 5: 17, 10: 22})
        check(code == 0 and rep["crossover"]["n_star"] == 5.0
              and rep["crossover"]["band"] == "crossover"
              and rep["crossover"]["censored"] == 0,
              "shortfall at N=5 -> finite N* = 5, band crossover")
        check(near(rep["fit"]["event_only"]["alpha"], 120.0 / 49.0, 1e-9),
              "OLS alpha_push = 120/49 pp/s")
        check(near(rep["fit"]["n_star_fit"], 4.056338, 1e-4)
              and rep["fit"]["extrapolated"] is False
              and rep["fit"]["non_identifiable"] is False,
              "N*_fit = (beta_pull - beta_push)/(alpha_push - alpha_pull)")
        check(rep["growth"]["gate_pass"] is True
              and near(rep["growth"]["alpha_push_pp_per_s"], 120.0 / 49.0, 1e-9),
              "growth gate alpha >= 0.5 and CI lower > 0.2")
        check(rep["branch"] == "reported" and rep["H-S3"]["pass"] is True,
              "H-S3 met: growth passes and a band is reported")

        code, rep = run_crossover(base, "censored", push={2: 5, 5: 10, 10: 15},
                                  pull={2: 20, 5: 21, 10: 22})
        check(rep["crossover"]["n_star"] == 11.0
              and rep["crossover"]["censored"] == 3
              and rep["crossover"]["band"] == "push-advantage-through-N10",
              "all seeds censored (inf=11) -> push-advantage-through-N10")
        check(rep["fit"]["extrapolated"] is True
              and rep["branch"] == "reported" and rep["H-S3"]["pass"] is True,
              "censored-band report counts as an N* report (H-S3 met)")

        code, rep = run_crossover(base, "band10", push={2: 10, 5: 20, 10: 30},
                                  pull={2: 14, 5: 22, 10: 28},
                                  seeds=(6301, 6302))
        check(rep["crossover"]["n_star"] == 10.0
              and rep["crossover"]["censored"] == 0
              and rep["crossover"]["band"] == "crossover",
              "band mapping exact: median N* = 10 -> crossover")

        extra = []
        for cell, n in (("premature2", 2), ("premature5", 5),
                        ("premature10", 10)):
            extra += [
                {"arm": "event_only", "cell": cell, "seed": 6302, "n": n,
                 "d_s": {2: 5, 5: 10, 10: 15}[n], "status": "ok"},
                {"arm": "reconcile", "cell": cell, "seed": 6302, "n": n,
                 "d_s": {2: 20, 5: 21, 10: 22}[n], "status": "ok"},
            ]
        code, rep = run_crossover(base, "band_inf", push={2: 10, 5: 20, 10: 30},
                                  pull={2: 14, 5: 22, 10: 28},
                                  seeds=(6301, 6302), extra_contrasts=extra)
        check(rep["crossover"]["censored"] == 1
              and rep["crossover"]["n_star"] == 10.5
              and rep["crossover"]["band"] == "push-advantage-through-N10",
              "band mapping exact: one censored of two (median 10.5 > 10) -> "
              "push-advantage-through-N10")

        code, rep = run_crossover(base, "nonident", push={2: 10, 5: 10.5, 10: 12},
                                  pull={2: 10, 5: 16, 10: 26})
        check(rep["fit"]["non_identifiable"] is True
              and rep["fit"]["n_star_fit"] is None,
              "alpha_push - alpha_pull <= 0 -> N*_fit non-identifiable")
        check(rep["crossover"]["band"] == "pull-dominates-immediately"
              and rep["crossover"]["n_star"] == 2.0,
              "crossing at N=2 -> pull-dominates-immediately")
        check(rep["branch"] == "growth_gate_failure",
              "growth fails with max D-bar >= 5 pp -> growth-gate failure")

        code, rep = run_crossover(base, "stopnull", push={2: 0.6, 5: 1.0, 10: 1.6},
                                  pull={2: 5, 5: 6, 10: 7})
        check(rep["branch"] == "stop_null" and rep["H-S3"]["pass"] is False,
              "max D-bar < 2 pp -> stop_null (precedence step 1)")

        code, rep = run_crossover(base, "subthreshold", push={2: 2.5, 5: 2.8, 10: 3.2},
                                  pull={2: 4, 5: 5, 10: 6})
        check(rep["branch"] == "sub_threshold",
              "growth fails with max D-bar < 5 pp -> sub-threshold report")

        code, rep = run_crossover(base, "na", push={2: 10, 5: 20, 10: 30},
                                  pull={2: 14, 5: 17, 10: 22}, seeds=(6301,))
        check(rep["crossover"]["na"] is True and rep["H-S3"]["status"] == "na",
              "<2 usable seeds -> crossover NA (counts as unmet)")

        extra = []
        runs = []
        for seed in (6301, 6302, 6303):
            runs.append({"arm": "event_only", "cell": "premature10", "seed": seed,
                         "f2": {"pre_by_n": {"10": {"http000": {"lan1": 12, "lan2": 11}}}}})
            runs.append({"arm": "hybrid", "cell": "premature10", "seed": seed,
                         "f2": {"pre_by_n": {"10": {"http000": {"lan1": 12, "lan2": 11}}}}})
            runs.append({"arm": "reconcile", "cell": "semantic", "seed": seed,
                         "check_violations": {"total": 0}})
            extra += [
                {"arm": "event_only", "cell": "semantic", "seed": seed,
                 "d_s": {6301: 31, 6302: 33, 6303: 35}[seed], "status": "ok"},
                {"arm": "reconcile", "cell": "semantic", "seed": seed,
                 "d_s": {6301: 32, 6302: 34, 6303: 39}[seed], "status": "ok"},
                {"arm": "reconcile", "cell": "premature10", "seed": seed, "n": 10,
                 "d_s": {6301: 0.5, 6302: 1.0, 6303: 1.5}[seed], "status": "ok"},
            ]
        code, rep = run_crossover(base, "hypotheses", extra_contrasts=extra,
                                  runs=runs)
        check(rep["H-S1"]["pass"] is True,
              "H-S1: all five components >= 2/3 seeds")
        check(rep["H-S2"]["pass"] is True
              and near(rep["H-S2"]["gap_median_pp"], 33.0, 1e-9),
              "H-S2: reconcile F2 <=2 pp, semantic >=30 pp, zero violations "
              "(gap median 33 pp >= 28 pp)")

        # Joint-pattern boundary: marginals each >=2/3 seeds but no seed meets
        # all three at once -> H-S2 not met; the gap stays descriptive.
        extra = []
        runs = []
        for seed in (6301, 6302, 6303):
            runs.append({"arm": "reconcile", "cell": "semantic", "seed": seed,
                         "check_violations": {"total": 0}})
            extra += [
                {"arm": "reconcile", "cell": "premature10", "seed": seed,
                 "n": 10, "d_s": {6301: 1.0, 6302: 5.0, 6303: 1.5}[seed],
                 "status": "ok"},
                {"arm": "reconcile", "cell": "semantic", "seed": seed,
                 "d_s": {6301: 20.0, 6302: 40.0, 6303: 35.0}[seed],
                 "status": "ok"},
            ]
        code, rep = run_crossover(base, "h2_split", extra_contrasts=extra,
                                  runs=runs)
        joint = rep["H-S2"]["components"][
            "reconcile_same_seed_joint_f2_le_2pp_semantic_ge_30pp_zero_violations"]
        check(rep["H-S2"]["pass"] is False and joint["hits"] == 1
              and rep["H-S2"]["components"]["reconcile_f2_le_2pp"]["pass"] is True
              and rep["H-S2"]["components"]["reconcile_semantic_ge_30pp"]
              ["pass"] is True,
              "H-S2 joint boundary: marginals pass, no same-seed pair -> not met")
        check(rep["H-S2"]["gap_ge_28pp"] is True
              and near(rep["H-S2"]["gap_median_pp"], 33.5, 1e-9),
              "H-S2 gap >=28 pp stays reported descriptively alongside")

        extra = [{"arm": "event_only", "cell": "semantic", "seed": 6301,
                  "d_s": 31.0, "status": "ok"}]
        code, rep = run_crossover(base, "na_component",
                                  push={2: 10, 5: 20, 10: 30},
                                  pull={2: 14, 5: 17, 10: 22},
                                  extra_contrasts=extra)
        component = rep["H-S1"]["components"]["event_only_semantic_ge_30pp"]
        check(component["verdict"] == "na" and component["lost"] == 2,
              "component with >=2 lost seeds -> NA, counts as unmet")

        extra = []
        runs = []
        for seed in (6301, 6302, 6303):
            extra += [
                {"arm": "wake_verify", "cell": "premature10", "seed": seed,
                 "d_s": 1.0, "status": "ok"},
                {"arm": "wake_verify", "cell": "semantic", "seed": seed,
                 "d_s": {6301: 32, 6302: 34, 6303: 36}[seed], "status": "ok"},
            ]
            runs += [
                {"arm": "wake_verify", "cell": "none", "seed": seed,
                 "u": {"f1_pct": 3.0}},
                {"arm": "event_only", "cell": "none", "seed": seed,
                 "u": {"f1_pct": 1.5}},
            ]
        code, rep = run_crossover(base, "wake", extra_contrasts=extra, runs=runs)
        check(rep["H-S4"]["status"] == "pass" and rep["H-S4"]["pass"] is True,
              "H-S4: wake_verify F2 <=2, F3 >=30, healthy cost 1.5 <=2 pp")

        code, report, lock_path = run_preflight(base, "ok")
        check(code == 0 and report["verdict"] == "lock",
              "R1/R2/R3/R3b all pass -> lock written (exit 0)")
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        check(lock["locked"] is True and lock["semantic_lie_s"] == 1200
              and lock["premature_lead_s"] == 10,
              "lock JSON records S=1200 and the gate results")
        r1g = report["runs"]["R1"]["gates"]
        r2g = report["runs"]["R2"]["gates"]
        r3g = report["runs"]["R3"]["gates"]
        check(all(r1g.values()) and all(r2g.values()) and all(r3g.values()),
              "all R1 (a-f) / R2 (a-e) / R3 (a-d) gates true at the thresholds")
        check(near(report["runs"]["R1"]["measured_deferrals_s"][0], 10.0, 1e-9),
              "R1(f) measured deferral = 10 s in [9, 11]")
        check(near(report["runs"]["R2"]["descriptive"]
                   ["admit_to_first_median_s"], 1.399, 1e-3),
              "R2(d) descriptive t_admit->first in [0, 2] s")
        check(near(report["runs"]["R3"]["f3_coverage"], 0.9, 1e-12),
              "R3(a) coverage boundary: exactly 90 % passes")
        check(report["r3b_informative"] is True,
              "R3b same gates evaluated (reconcile x semantic)")
        r1floors = report["runs"]["R1"]["floors"]
        check(r1floors["pass"] is True and r1floors["pooled_offered"] >= 5000
              and all(value >= 1000
                      for value in r1floors["per_lan_offered"].values())
              and r1floors["f2_window_min_per_lan"]["lan1"] >= 15
              and r1floors["cancel_rate"] < 0.05,
              "R1 §4.5 floors met + reported (>=5,000 pooled / >=1,000 LAN / "
              "F2 window >=15 / cancel <5 %)")
        check(report["runs"]["R1"]["spawns_found"] == 2
              and report["runs"]["R1"]["spawns_admission_rows"] == 2
              and report["runs"]["R1"]["spawns_mismatch"] is False,
              "spawn cross-check exposed and clean for R1 (2 logs = 2 rows)")

        code, report, lock_path = run_preflight(base, "c12",
                                                r1={"success_after_admit":
                                                    (12.0, 12.0)})
        check(code == 0 and report["runs"]["R1"]["gates"]
              ["c_median_admit_first_in_8_12"] is True,
              "R1(c) boundary: median 12.0 s passes")

        code, report, lock_path = run_preflight(
            base, "diag", r3={"t_bind_offset": 100.0})
        check(code == 2 and report["verdict"] == "diagnose"
              and not lock_path.exists(),
              "R3 late bind (coverage 83 %) -> DIAGNOSE (exit 2), no lock")
        check(report["runs"]["R3"]["gates"]
              ["a_lie_window_coverage_ge_90pct"] is False,
              "R3(a) shortfall flagged; rerun allowance")

        code, report, _ = run_preflight(base, "r1a", r1={"window_bad": 9})
        check(code == 3 and report["runs"]["R1"]["gates"]
              ["a_in_window_000_ge_10_per_lan"] is False
              and report["runs"]["R1"]["gates"]
              ["f_measured_deferral_in_9_11"] is True,
              "R1(a): 9/LAN < 10 -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r1b", r1={"other_000": 0})
        check(code == 3 and report["runs"]["R1"]["gates"]
              ["b_run_wide_000_ge_0.3pct_and_50"] is False
              and report["runs"]["R1"]["gates"]
              ["a_in_window_000_ge_10_per_lan"] is True,
              "R1(b): 24 run-wide 000 rows < 50 -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r1c", r1={"success_after_admit":
                                                         (12.2, 12.2)})
        check(code == 3 and report["runs"]["R1"]["gates"]
              ["c_median_admit_first_in_8_12"] is False,
              "R1(c): median 12.2 s > 12 -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r1f", r1={"deferral": 5.0})
        check(code == 3 and report["runs"]["R1"]["gates"]
              ["f_measured_deferral_in_9_11"] is False,
              "R1(f): measured deferral 5 s outside [9, 11] -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r2a", r2={"zero_rows": 3})
        check(code == 3 and report["runs"]["R2"]["gates"]
              ["a_000_le_1_and_le_0.2pct"] is False,
              "R2(a): 3 run-wide 000 rows > 1 -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r2d", r2={"success_after_claim":
                                                         (22.5, 22.5)})
        check(code == 3 and report["runs"]["R2"]["gates"]
              ["d_median_claim_first_in_10_22"] is False,
              "R2(d): median claim->first 22.5 s > 22 -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r3sig", r3={"fivexx": 17})
        check(code == 3 and report["runs"]["R3"]["gates"]
              ["b_attributed_5xx_ge_90pct_and_15_per_lan"] is False,
              "R3(b): 85 % attributed 5xx < 90 % -> STOP (exit 3)")

        code, report, lock_path = run_preflight(
            base, "r3bbad", r3b={"t_bind_offset": 100.0})
        check(code == 0 and report["verdict"] == "lock"
              and lock_path.exists() and report["r3b_informative"] is False,
              "R3b failure is informative only: R1/R2/R3 pass -> lock (exit 0)")

        # Gate-level class-based 000: timeouts with http_status=000 must not
        # trip R2(a) (raw counting would give 1 + 4 = 5 > 1).
        code, report, _ = run_preflight(base, "r2timeout",
                                        r2={"timeout_rows": 4})
        check(code == 0 and report["verdict"] == "lock"
              and report["runs"]["R2"]["gates"]
              ["a_000_le_1_and_le_0.2pct"] is True,
              "class-based 000: 4 timeouts (http 000) do NOT trip R2(a)")

        # Raw counting with 6 timeouts in the F3 window would be 9/771 > 1 %.
        code, report, _ = run_preflight(base, "r3timeout",
                                        r3={"window_timeouts": 6})
        check(code == 0 and report["verdict"] == "lock"
              and report["runs"]["R3"]["gates"]
              ["c_transport_000_le_1pct_window"] is True,
              "class-based 000: 6 in-window timeouts (http 000) do NOT trip "
              "R3(c)")

        code, report, lock_path = run_preflight(
            base, "diag_rerun", r3={"t_bind_offset": 100.0},
            rerun_attempted=True)
        check(code == 3 and report["verdict"] == "stop"
              and not lock_path.exists()
              and any("after permitted rerun" in reason
                      for reason in report["stop_reasons"]),
              "--rerun-attempted: persisting R3 shortfall -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "invalid", r1={"quota": "0.13"})
        check(code == 2 and report["verdict"] == "diagnose",
              "base-gate-invalid run without the flag stays DIAGNOSE (exit 2)")

        code, report, _ = run_preflight(base, "invalid_rerun",
                                        r1={"quota": "0.13"},
                                        rerun_attempted=True)
        check(code == 3 and report["verdict"] == "stop"
              and report["runs"]["R1"]["evaluated"] is False,
              "--rerun-attempted: unrepaired invalid run -> STOP (exit 3)")

        code, report, _ = run_preflight(base, "r1floors",
                                        r1={"filler_per_lan": 10})
        check(code == 3 and report["verdict"] == "stop"
              and report["runs"]["R1"]["pass"] is True
              and report["runs"]["R1"]["floors"]["pass"] is False
              and any("R1 measurement floors unmet" in reason
                      for reason in report["stop_reasons"]),
              "R1 §4.5 floors unmet -> STOP (exit 3, no N escalation) even "
              "with every R1 signature gate true")

        err_code = robustness.main([
            "soundness-preflight", "--run-dir", str(base / "no_such_run"),
            "--out", str(base / "err.json"),
            "--lock-out", str(base / "err_lock.json")])
        check(err_code == 3,
              "unexpected/parse failures exit 3 (STOP): never 2")

        print(json.dumps({
            "class_priority_boundaries": "PASS",
            "class_000_timeout_exclusion": "PASS",
            "label_r_suffix": "PASS",
            "f1_f2_f3_windows": "PASS",
            "d_s_d_bar_none_breach": "PASS",
            "crossover_finite": "PASS",
            "crossover_censored": "PASS",
            "crossover_band_exact": "PASS",
            "crossover_non_identifiable": "PASS",
            "crossover_stop_null": "PASS",
            "crossover_sub_threshold": "PASS",
            "crossover_na": "PASS",
            "h_s1_verdicts": "PASS",
            "h_s2_verdicts": "PASS",
            "h_s2_joint_pattern": "PASS",
            "h_s4_verdicts": "PASS",
            "spawn_cross_check": "PASS",
            "preflight_lock": "PASS",
            "preflight_r1_boundaries": "PASS",
            "preflight_r1_floors": "PASS",
            "preflight_r2_thresholds": "PASS",
            "preflight_r3_diagnose": "PASS",
            "preflight_r3_stop": "PASS",
            "preflight_rerun_attempted": "PASS",
            "preflight_r3b_informative": "PASS",
            "analyzer_error_exit": "PASS",
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
