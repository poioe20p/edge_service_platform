#!/usr/bin/env python3
"""Synthetic selftest for the RQ3 QoE analyzer (Family 3).

Covers: the QoE measurement contract math (rates/bad_rate/floors), label
parsing, the C1/C3 screen decision logic (gates + contrast/lock), and the
E-stage qoe-campaign verdicts (H-Q1 contrast + boundedness, H-Q2, H-Q3,
H-Q4, direction checks). Artifact-heavy gates are exercised on synthetic
client_requests/run-status fixtures; screen/campaign gate logic is exercised
by patching the artifact readers with controlled metric dicts.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source/scripts/testing/analysis"))

from rq3 import readiness_robustness as robustness  # noqa: E402

PHASES = {
    "phases": [
        {"name": "baseline", "duration_s": 60.0},
        {"name": "compute_plateau", "duration_s": 600.0},
        {"name": "demand_drop", "duration_s": 60.0},
    ]
}
CSV_HEADER = ["sent_at", "phase", "client_lan", "http_status", "latency_s", "status"]


def _write_run(base: Path, name: str, event_timeouts: float = 0.0) -> Path:
    """Minimal run with real baseline/plateau CSVs for the artifact layer."""
    run = base / name
    run.mkdir(parents=True)
    start = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc).timestamp()
    (run / "run_status.json").write_text(
        json.dumps({"started_at": datetime.fromtimestamp(start, tz=timezone.utc).isoformat()}),
        encoding="utf-8")
    (run / "phases_snapshot.json").write_text(json.dumps(PHASES), encoding="utf-8")
    rows = [CSV_HEADER]
    sent = start
    # baseline: 1200 completed rows per LAN (60 s @ ~20 rps/lan)
    for i in range(2400):
        lan = 1 if i % 2 == 0 else 2
        rows.append([f"{sent + (i % 60):.3f}", "baseline", f"lan{lan}", "200", "0.01",
                     "completed"])
    # plateau: 3000 rows per LAN; event_only cell mixes in timeouts (bad).
    timeout_ix = int(event_timeouts * 3000) if event_timeouts > 0 else 0
    for i in range(6000):
        lan = 1 if i % 2 == 0 else 2
        plateau_sent = start + 60.0 + (i % 600)
        per_lan_index = i // 2
        if event_timeouts > 0 and per_lan_index < timeout_ix:
            rows.append([f"{plateau_sent:.3f}", "compute_plateau", f"lan{lan}", "000", "120.0",
                         "timeout"])
        else:
            rows.append([f"{plateau_sent:.3f}", "compute_plateau", f"lan{lan}", "200", "0.01",
                         "completed"])
    (run / "client_requests.csv").write_text("\n".join(",".join(row) for row in rows) + "\n",
                                             encoding="utf-8")
    return run


def _pooled(timeout: float, bad: float, cancel: float = 0.0) -> dict:
    return {"offered": 10000, "canceled_dropped": int(cancel * 10000),
            "cancel_rate": cancel, "http000": 0, "completed": 10000, "timeouts": int(timeout * 10000),
            "failures": int(bad * 10000) - int(timeout * 10000) if bad > timeout else 0,
            "timeout_rate": timeout, "failure_rate": max(bad - timeout, 0.0),
            "bad_rate": bad, "service_requests": 10000}


def fake_metrics(run: str, cell: str, arm: str, timeout: float = 0.0, bad: float = 0.0,
                 admitted_per_lan: dict | None = None, abandoned_per_lan: dict | None = None,
                 sources: dict | None = None, drop_modes: list | None = None,
                 quota: str = "0.11") -> dict:
    admitted_per_lan = admitted_per_lan or {1: 0, 2: 0}
    abandoned_per_lan = abandoned_per_lan or {1: 0, 2: 0}
    sources = sources or {1: [], 2: []}
    admitted_total = sum(admitted_per_lan.values())
    event_admitted = sum(1 for lan in (1, 2) for src in sources.get(lan, [])
                         if src == "event")
    probe_admitted = sum(1 for lan in (1, 2) for src in sources.get(lan, [])
                         if src == "probe")
    probe_fallback = sum(1 for lan in (1, 2) for src in sources.get(lan, [])
                         if src == "probe_fallback")
    return {
        "run": run,
        "parts": (cell, arm, "1") if arm else None,
        "plateau": {"pooled": _pooled(timeout, bad),
                    "per_lan": {1: _pooled(timeout, bad), 2: _pooled(timeout, bad)}},
        "baseline": {"pooled": _pooled(0.0, 0.001), "per_lan": {1: _pooled(0, 0), 2: _pooled(0, 0)}},
        "floors": {"plateau_pooled_ge_5000": True, "plateau_per_lan_ge_1000": True,
                   "baseline_pooled_ge_100": True, "baseline_per_lan_ge_50": True},
        "driver_clean": True,
        "baseline_http000_zero": True,
        "mechanism": {
            "admitted_total": admitted_total,
            "abandoned_total": sum(abandoned_per_lan.values()),
            "per_lan_admitted": admitted_per_lan,
            "per_lan_abandoned": abandoned_per_lan,
            "per_lan_sources": sources,
            "probe_fallback_admitted": probe_fallback,
            "event_admitted": event_admitted,
            "probe_admitted": probe_admitted,
            "event_fraction": event_admitted / admitted_total if admitted_total else None,
        },
        "drop_modes": drop_modes or [],
        "scale_down_in_plateau": False,
        "quota": {"requested_edge_cpus": quota, "all_compute_containers_match": True,
                  "present": True},
        "pressure": {"pre_cpu": {1: {"median": 70.0, "p95": 80.0},
                                 2: {"median": 72.0, "p95": 82.0}},
                     "method": "pre_first_ready", "error": None},
        "pressure_ok": True,
    }


def c1_set() -> dict:
    base = {
        "event_only": fake_metrics("rq3qoe_loss_all_event_only_50", "loss_all", "event_only",
                                   timeout=0.08, bad=0.09,
                                   abandoned_per_lan={1: 3, 2: 3}, drop_modes=["all"]),
        "hybrid": fake_metrics("rq3qoe_loss_all_hybrid_51", "loss_all", "hybrid",
                               timeout=0.005, bad=0.004,
                               admitted_per_lan={1: 2, 2: 2},
                               sources={1: ["probe_fallback", "probe_fallback"],
                                        2: ["probe_fallback", "probe_fallback"]}),
        "reconcile": fake_metrics("rq3qoe_loss_all_reconcile_52", "loss_all", "reconcile",
                                  timeout=0.005, bad=0.004,
                                  admitted_per_lan={1: 2, 2: 2},
                                  sources={1: ["probe", "probe"], 2: ["probe", "probe"]}),
    }
    return base


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        run = _write_run(base, "rq3qoe_loss_all_event_only_50", event_timeouts=0.06)
        m = robustness.qoe_rate_metrics(run)
        parts = robustness.qoe_label_parts(run.name)
        assert parts == ("loss_all", "event_only", "50"), parts
        assert all(m["floors"].values()), m["floors"]
        assert m["driver_clean"] and m["baseline_http000_zero"], m
        assert m["plateau"]["pooled"]["timeout_rate"] is not None, m
        assert abs(m["plateau"]["pooled"]["bad_rate"] - 0.06) < 0.005, m["plateau"]["pooled"]

        # Screen C1: eligibility + lock candidate on a qualifying rung.
        robustness.qoe_run_metrics = lambda p: c1_set()[
            robustness.qoe_label_parts(Path(p).name)[1]]
        run_dirs = [base / n for n in
                    ("rq3qoe_loss_all_event_only_50", "rq3qoe_loss_all_hybrid_51",
                     "rq3qoe_loss_all_reconcile_52")]
        args = argparse.Namespace(stage="c1", rung="0.11", seed="5301",
                                  run_dir=[str(p) for p in run_dirs],
                                  c1_json=None, out=str(base / "c1.json"))
        code = robustness.qoe_screen_command(args)
        c1 = json.loads((base / "c1.json").read_text(encoding="utf-8"))
        assert c1["eligible"] is True, c1
        assert c1["lock_candidate"] is True, c1
        assert c1["delta_pp"] is not None and c1["delta_pp"] >= 5.0, c1
        assert code == 0, code

        # C2 reads the C1 output.
        args2 = argparse.Namespace(stage="c2", rung="0.11", seed="5301", run_dir=[],
                                   c1_json=str(base / "c1.json"),
                                   out=str(base / "c2.json"))
        robustness.qoe_screen_command(args2)
        c2 = json.loads((base / "c2.json").read_text(encoding="utf-8"))
        assert c2["lock_candidate"] is True, c2

        # C3 seed confirmation (4 runs incl. healthy none cell).
        c3_metrics = dict(c1_set())
        c3_metrics["none_eo"] = fake_metrics("rq3qoe_none_event_only_56", "none", "event_only",
                                             timeout=0.001, bad=0.001,
                                             admitted_per_lan={1: 2, 2: 2},
                                             sources={1: ["event", "event"],
                                                      2: ["event", "event"]})
        c3_dirs = [base / n for n in
                   ("rq3qoe_loss_all_event_only_53", "rq3qoe_loss_all_hybrid_54",
                    "rq3qoe_loss_all_reconcile_55", "rq3qoe_none_event_only_56")]

        def c3_lookup(p: Path):
            parts = robustness.qoe_label_parts(p.name)
            if parts[0] == "none":
                return c3_metrics["none_eo"]
            return c3_metrics[parts[1]]

        robustness.qoe_run_metrics = c3_lookup
        args3 = argparse.Namespace(stage="c3", rung="0.11", seed="5302",
                                   run_dir=[str(p) for p in c3_dirs],
                                   c1_json=None, out=str(base / "c3.json"))
        code3 = robustness.qoe_screen_command(args3)
        c3 = json.loads((base / "c3.json").read_text(encoding="utf-8"))
        assert c3["seed_confirm"] is True, c3
        assert code3 == 0, code3

        # qoe-campaign verdicts over 6 fabricated blocks (patched readers).
        def campaign_metrics(p: Path) -> dict:
            cell, arm, block = robustness.qoe_label_parts(p.name)
            floors = {"plateau_pooled_ge_5000": True, "plateau_per_lan_ge_1000": True,
                      "baseline_pooled_ge_100": True, "baseline_per_lan_ge_50": True}

            def pooled(bad: float, timeout: float) -> dict:
                return {"offered": 10000, "canceled_dropped": 0, "cancel_rate": 0.0,
                        "http000": 0, "completed": int((1 - timeout) * 10000),
                        "timeouts": int(timeout * 10000),
                        "failures": int((bad - timeout) * 10000) if bad > timeout else 0,
                        "timeout_rate": timeout, "failure_rate": max(bad - timeout, 0.0),
                        "bad_rate": bad, "service_requests": 10000}

            if cell == "none":
                bad, timeout = 0.003, 0.002
            elif cell == "loss_all":
                bad, timeout = (0.08, 0.075) if arm == "event_only" else (0.004, 0.004)
            else:  # loss_alt
                bad, timeout = (0.04, 0.035) if arm == "event_only" else (0.004, 0.004)
            pool = pooled(bad, timeout)
            return {"run": p.name, "parts": (cell, arm, block),
                    "plateau": {"pooled": pool,
                                "per_lan": {1: dict(pool), 2: dict(pool)}},
                    "baseline": {"pooled": pooled(0.0, 0.0),
                                  "per_lan": {1: pooled(0.0, 0.0), 2: pooled(0.0, 0.0)}},
                    "floors": dict(floors), "driver_clean": True,
                    "baseline_http000_zero": True}

        def campaign_classify(p: Path) -> dict:
            cell, arm, block = robustness.qoe_label_parts(p.name)
            parts = (cell, arm, block)
            if cell == "loss_all":
                return {"run": p.name, "parts": parts,
                        "preservation": 0.0 if arm == "event_only" else 1.0,
                        "per_lan_preservation": {1: 0.0 if arm == "event_only" else 1.0,
                                                 2: 0.0 if arm == "event_only" else 1.0},
                        "admitted": 0 if arm == "event_only" else 4,
                        "abandoned": 6 if arm == "event_only" else 0, "spawned": 6}
            if cell == "loss_alt":
                return {"run": p.name, "parts": parts,
                        "preservation": 0.6 if arm == "event_only" else 1.0,
                        "per_lan_preservation": {1: 1.0, 2: 1.0},
                        "admitted": 4, "abandoned": 0, "spawned": 6}
            return {"run": p.name, "parts": parts, "preservation": 1.0,
                    "per_lan_preservation": {1: 1.0, 2: 1.0},
                    "admitted": 4, "abandoned": 0, "spawned": 4}

        robustness.qoe_rate_metrics = campaign_metrics
        robustness.classify = campaign_classify
        fake_dirs = [str(base / f"rq3qoe_{cell}_{arm}_{block}")
                     for block in range(1, 7)
                     for cell in ("none", "loss_all", "loss_alt")
                     for arm in ("event_only", "hybrid", "reconcile")]
        args4 = argparse.Namespace(run_dir=fake_dirs, rung="0.11",
                                   out=str(base / "campaign.json"))
        code4 = robustness.qoe_campaign_command(args4)
        report = json.loads((base / "campaign.json").read_text(encoding="utf-8"))
        assert report["pass"] is True, report
        assert report["H-Q1"]["pass"] is True, report["H-Q1"]
        assert report["H-Q1_boundedness"]["pass"] is True, report["H-Q1_boundedness"]
        assert report["H-Q2"]["pass"] is True, report["H-Q2"]
        assert report["H-Q3"]["pass"] is True, report["H-Q3"]
        assert report["H-Q4"]["pass"] is True, report["H-Q4"]
        assert code4 == 0, code4

        # Boundedness component must fail when event_only loss_all is severe.
        def severe_metrics(p: Path) -> dict:
            out = campaign_metrics(p)
            cell, arm, _ = robustness.qoe_label_parts(p.name)
            if cell == "loss_all" and arm == "event_only":
                out = dict(out)
                out["plateau"] = {
                    "pooled": dict(out["plateau"]["pooled"]),
                    "per_lan": out["plateau"]["per_lan"]}
                out["plateau"]["pooled"]["bad_rate"] = 0.35
            return out

        robustness.qoe_rate_metrics = severe_metrics
        code5 = robustness.qoe_campaign_command(argparse.Namespace(
            run_dir=fake_dirs, rung="0.11", out=str(base / "campaign2.json")))
        report2 = json.loads((base / "campaign2.json").read_text(encoding="utf-8"))
        assert report2["pass"] is False, report2
        assert report2["H-Q1_boundedness"]["pass"] is False, report2["H-Q1_boundedness"]
        assert code5 == 2, code5

        # Real-artifact readers: mechanism + zero-admission pressure fallback.
        arun = base / "rq3qoe_loss_all_event_only_60"
        arun.mkdir()
        start2 = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc).timestamp()
        (arun / "run_status.json").write_text(json.dumps(
            {"started_at": datetime.fromtimestamp(start2, tz=timezone.utc).isoformat()}),
            encoding="utf-8")
        (arun / "phases_snapshot.json").write_text(json.dumps(PHASES), encoding="utf-8")
        for lan in (1, 2):
            (arun / f"admission_log_lan{lan}.csv").write_text(
                "ts,network_id,lan,container,mac,ip,mode,result,spawn_started_ts,"
                "spawn_complete_ts,probe_first_ts,app_ready_ts,admitted_ts,admit_source\n"
                + "".join(
                    f"{i},lan{lan},{lan},edge_server_lan{lan}_dyn{i},mac,ip,direct,"
                    f"abandoned,{start2 + 10:.3f},{start2 + 11:.3f},,,{start2 + 130:.3f},\n"
                    for i in range(2)),
                encoding="utf-8")
            (arun / f"window_log_lan{lan}.jsonl").write_text(
                "\n".join(json.dumps({"window_end": start2 + 60.0 + i,
                                      "servers": {"edge_server_n1":
                                                  {"avg_cpu_percent": 72.0}}})
                          for i in range(6)) + "\n", encoding="utf-8")
        # Authoritative phase_bounds derives windows from generator-labeled
        # request rows, so the zero-admission fixture still needs a labeled
        # compute_plateau client_requests.csv for the pressure fallback path.
        plateau_rows = []
        for i in range(20):
            lan = 1 if i % 2 == 0 else 2
            plateau_rows.append(",".join(
                [f"{start2 + 60.0 + i:.3f}", "compute_plateau", f"lan{lan}", "200", "0.01",
                 "completed"]))
        (arun / "client_requests.csv").write_text(
            ",".join(CSV_HEADER) + "\n" + "\n".join(plateau_rows) + "\n",
            encoding="utf-8")
        mech = robustness.qoe_mechanism(arun)
        assert mech["admitted_total"] == 0, mech
        assert all(mech["per_lan_abandoned"][lan] >= 2 for lan in (1, 2)), mech
        pressure = robustness.qoe_pressure(arun)
        assert pressure["method"] == "plateau_first_120s", pressure
        assert robustness.qoe_pressure_ok(pressure), pressure

        # C1 negative: a control (hybrid) over 1% bad_rate -> ineligible (exit 2).
        def neg_lookup(p: Path):
            arm = robustness.qoe_label_parts(p.name)[1]
            base = dict(c1_set()[arm])
            if arm == "hybrid":
                base = dict(base)
                base["plateau"] = {"pooled": _pooled(0.005, 0.02),
                                   "per_lan": base["plateau"]["per_lan"]}
            return base

        robustness.qoe_run_metrics = neg_lookup
        code_neg = robustness.qoe_screen_command(argparse.Namespace(
            stage="c1", rung="0.11", seed="5301",
            run_dir=[str(p) for p in run_dirs], c1_json=None,
            out=str(base / "c1neg.json")))
        c1neg = json.loads((base / "c1neg.json").read_text(encoding="utf-8"))
        assert c1neg["eligible"] is False and code_neg == 2, (c1neg, code_neg)

        # C2 negative: delta below 5 pp -> no lock candidate.
        weak = dict(c1)
        weak["delta_pp"] = 2.0
        (base / "c1weak.json").write_text(json.dumps(weak), encoding="utf-8")
        code_neg2 = robustness.qoe_screen_command(argparse.Namespace(
            stage="c2", rung="0.11", seed="5301", run_dir=[],
            c1_json=str(base / "c1weak.json"), out=str(base / "c2weak.json")))
        c2weak = json.loads((base / "c2weak.json").read_text(encoding="utf-8"))
        assert c2weak["lock_candidate"] is False and code_neg2 == 2, (c2weak, code_neg2)

        print(json.dumps({
            "artifact_rates": "PASS", "c1": "PASS", "c2": "PASS", "c3": "PASS",
            "campaign_pass": "PASS", "campaign_boundedness_fail": "PASS",
            "artifact_readers": "PASS", "c1_negative": "PASS", "c2_negative": "PASS",
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
