#!/usr/bin/env python3
"""Standalone selftest for the RQ3 timing analyzer (timing-screen/timing-campaign).

Self-contained on purpose: the rq3tim family must not depend on the shared
qoe selftest file. Covers the preflight gates (P1 candidate / descend / V1
pressure amendment, P2 confirm / none-cell stop_null, lock + marginal) and the
E-stage timing-campaign (H-T1/H-T2/H-T3 pass + controls-tail negative), plus
the timing_tail_slow artifact math.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source/scripts/testing/analysis"))

from rq3 import readiness_robustness as robustness  # noqa: E402


def pooled(timeout: float, bad: float, slow: float | None = None,
           cancel: float = 0.0) -> dict:
    slow = timeout + 0.005 if slow is None else slow
    return {"offered": 10000, "canceled_dropped": int(cancel * 10000),
            "cancel_rate": cancel, "http000": 0, "completed": 10000,
            "timeouts": int(timeout * 10000),
            "failures": int(bad * 10000) - int(timeout * 10000) if bad > timeout else 0,
            "timeout_rate": timeout, "failure_rate": max(bad - timeout, 0.0),
            "bad_rate": bad, "service_requests": 10000,
            "slow_n": int(slow * 10000), "slow_rate": slow,
            "slow5_n": int(slow * 8000), "slow5_rate": round(slow * 0.8, 4),
            "lat_completed_n": 10000, "lat_completed_p50": 0.05,
            "lat_completed_p95": 0.3, "lat_completed_p99": 0.5}


def fake_metrics(cell: str, arm: str, timeout: float = 0.0, bad: float = 0.0,
                 slow: float | None = None, admit: int = 0, abandon: int = 0,
                 sources: list[str] | None = None) -> dict:
    sources = sources or []
    admitted_total = len(sources)
    event_admitted = sum(1 for src in sources if src == "event")
    probe_admitted = sum(1 for src in sources if src == "probe")
    probe_fallback = sum(1 for src in sources if src == "probe_fallback")
    per_lan = {1: admitted_total, 2: admitted_total}
    p = pooled(timeout, bad, slow)
    return {
        "run": f"rq3tim_{cell}_{arm}_1",
        "parts": (cell, arm, "1"),
        "plateau": {"pooled": p, "per_lan": {1: p, 2: p}},
        "baseline": {"pooled": pooled(0.0, 0.0, 0.005),
                     "per_lan": {1: pooled(0.0, 0.0, 0.005),
                                 2: pooled(0.0, 0.0, 0.005)}},
        "floors": {"plateau_pooled_ge_5000": True, "plateau_per_lan_ge_1000": True,
                   "baseline_pooled_ge_100": True, "baseline_per_lan_ge_50": True},
        "driver_clean": True, "baseline_http000_zero": True,
        "mechanism": {
            "admitted_total": admitted_total, "abandoned_total": abandon,
            "per_lan_admitted": per_lan,
            "per_lan_abandoned": {1: abandon, 2: abandon},
            "per_lan_sources": {1: sources, 2: sources},
            "probe_fallback_admitted": probe_fallback,
            "event_admitted": event_admitted, "probe_admitted": probe_admitted,
            "event_fraction": event_admitted / admitted_total if admitted_total else None,
        },
        "drop_modes": ["all"] if cell == "loss_all" else [],
        "scale_down_in_plateau": False,
        "quota": {"requested_edge_cpus": "0.12", "all_compute_containers_match": True,
                  "present": True},
        "pressure": {"pre_cpu": {1: {"median": 70.0, "p95": 80.0},
                                 2: {"median": 72.0, "p95": 82.0}},
                     "method": "pre_first_ready", "error": None},
        "pressure_ok": True,
    }


def loss_arms() -> dict:
    return {
        "event_only": fake_metrics("loss_all", "event_only", timeout=0.01,
                                   bad=0.012, slow=0.09, abandon=3),
        "hybrid": fake_metrics("loss_all", "hybrid", timeout=0.004, bad=0.004,
                               slow=0.01, sources=["probe_fallback"] * 2),
        "reconcile": fake_metrics("loss_all", "reconcile", timeout=0.004,
                                  bad=0.004, slow=0.01, sources=["probe"] * 2),
    }


def none_metrics() -> dict:
    return fake_metrics("none", "event_only", timeout=0.001, bad=0.001,
                        slow=0.006, sources=["event"] * 2)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        arms = loss_arms()
        none = none_metrics()
        labels_3 = ("rq3tim_loss_all_event_only_50", "rq3tim_loss_all_hybrid_51",
                    "rq3tim_loss_all_reconcile_52")
        labels_4 = (*labels_3, "rq3tim_none_event_only_56")

        def lookup(p: Path):
            label = robustness.timing_label_parts(p.name)
            return none if label[0] == "none" else arms[label[1]]

        robustness.qoe_run_metrics = lookup
        robustness.timing_tail_slow = lambda p: 0.01
        # Contract-v2 onset co-primary: default onset slow is high for
        # event_only (0.40) and healthy for the controls (0.02), giving an
        # onset Delta well above the >=5 pp bar.
        def onset_default(p: Path):
            arm = robustness.timing_label_parts(p.name)[1]
            return 0.40 if arm == "event_only" else 0.02

        robustness.timing_onset_slow = onset_default
        robustness.timing_relief_metrics = lambda p: {
            "ttr_s": 25.0, "new_backend_successes": 50,
            "first_success_sent_s": None}

        def run_screen(stage, labels, rate="2.0", seed="5301", out="s.json",
                       screen_json=None, lock_out=None):
            return robustness.timing_screen_command(argparse.Namespace(
                stage=stage, rate=rate, seed=seed,
                run_dir=[str(base / n) for n in labels],
                screen_json=screen_json or [], out=str(base / out),
                lock_out=lock_out))

        # P1 candidate: delta_slow 8 pp, onset delta ~38 pp, relief ok, V1 ok.
        code = run_screen("p1", labels_3, out="p1.json")
        p1 = json.loads((base / "p1.json").read_text(encoding="utf-8"))
        assert p1["verdict"] == "candidate" and code == 0, (p1, code)
        assert p1["delta_slow_pp"] == 8.0, p1
        assert p1["onset_delta_slow_pp"] == 38.0, p1
        assert p1["relief_ok"] is True and p1["pressure_ok"] is True, p1
        assert p1["bounded_ok"] is True, p1

        # P1 onset co-primary negative: full-plateau contrast >=5 pp but the
        # onset-window contrast <5 pp -> the verdict must descend (contract
        # v2 requires BOTH co-primaries).
        def onset_flat(p: Path):
            return 0.02  # no onset separation

        robustness.timing_onset_slow = onset_flat
        code_o = run_screen("p1", labels_3, out="p1noonset.json")
        p1no = json.loads((base / "p1noonset.json").read_text(encoding="utf-8"))
        assert p1no["onset_delta_slow_pp"] == 0.0, p1no
        assert p1no["verdict"] == "descend" and code_o == 2, (p1no, code_o)
        robustness.timing_onset_slow = onset_default

        # loss_all control full-plateau slow may be elevated (early-window
        # cost); health is the plateau TAIL, so P1 stays a candidate.
        def control_elevated(p: Path):
            out = lookup(p)
            label = robustness.timing_label_parts(p.name)
            if label[0] == "loss_all":
                if label[1] == "event_only":
                    slow, timeout, bad = 0.12, 0.01, 0.012
                elif label[1] == "hybrid":
                    slow, timeout, bad = 0.05, 0.004, 0.004
                else:
                    slow, timeout, bad = 0.04, 0.004, 0.004
                out = dict(out)
                out["plateau"] = {"pooled": pooled(timeout, bad, slow),
                                  "per_lan": out["plateau"]["per_lan"]}
            return out

        robustness.qoe_run_metrics = control_elevated
        code_e = run_screen("p1", labels_3, out="p1ctrl.json")
        p1e = json.loads((base / "p1ctrl.json").read_text(encoding="utf-8"))
        assert p1e["delta_slow_pp"] == 7.0, p1e
        assert p1e["verdict"] == "candidate" and code_e == 0, (p1e, code_e)
        robustness.qoe_run_metrics = lookup

        # V1 amendment: pre-ready p95 >95 (cap dropped) passes when median >=60.
        def hot_p95(p: Path):
            out = lookup(p)
            label = robustness.timing_label_parts(p.name)
            if label[1] == "hybrid":
                out = dict(out)
                out["pressure"] = {"pre_cpu": {1: {"median": 70.0, "p95": 99.0},
                                               2: {"median": 72.0, "p95": 99.0}},
                                   "method": "pre_first_ready", "error": None}
            return out

        robustness.qoe_run_metrics = hot_p95
        code_h = run_screen("p1", labels_3, out="p1hot.json")
        p1hot = json.loads((base / "p1hot.json").read_text(encoding="utf-8"))
        assert p1hot["pressure_ok"] is True and p1hot["verdict"] == "candidate", p1hot
        assert code_h == 0, code_h

        # V1 negative: median <60 -> pressure fails -> descend.
        def cold_median(p: Path):
            out = lookup(p)
            label = robustness.timing_label_parts(p.name)
            if label[1] == "hybrid":
                out = dict(out)
                out["pressure"] = {"pre_cpu": {1: {"median": 40.0, "p95": 60.0},
                                               2: {"median": 45.0, "p95": 62.0}},
                                   "method": "pre_first_ready", "error": None}
            return out

        robustness.qoe_run_metrics = cold_median
        code_c = run_screen("p1", labels_3, out="p1cold.json")
        p1cold = json.loads((base / "p1cold.json").read_text(encoding="utf-8"))
        assert p1cold["pressure_ok"] is False and p1cold["verdict"] == "descend", p1cold
        assert code_c == 2, code_c

        # P1 descend: weak contrast.
        robustness.qoe_run_metrics = lookup
        weak = dict(arms)
        weak["event_only"] = fake_metrics("loss_all", "event_only", timeout=0.004,
                                          bad=0.01, slow=0.03, abandon=3)
        robustness.qoe_run_metrics = lambda p: (
            none if robustness.timing_label_parts(p.name)[0] == "none"
            else weak[robustness.timing_label_parts(p.name)[1]])
        code_w = run_screen("p1", labels_3, out="p1weak.json")
        p1w = json.loads((base / "p1weak.json").read_text(encoding="utf-8"))
        assert p1w["verdict"] == "descend" and code_w == 2, (p1w, code_w)

        # P2 confirm (3 loss_all + none healthy).
        robustness.qoe_run_metrics = lookup
        code_p2 = run_screen("p2", labels_4, seed="5302", out="p2a.json")
        p2a = json.loads((base / "p2a.json").read_text(encoding="utf-8"))
        assert p2a["verdict"] == "confirm" and code_p2 == 0, (p2a, code_p2)

        # P2 negative: none-cell plateau-TAIL slow >2 pp -> stop_null
        # (contract v2 none-cell amendment 2026-09-08: the no-fault cell's
        # slow health floor is the plateau TAIL, so a sick none TAIL trips
        # stop_null; a full-plateau read alone no longer does).
        def none_tail_sick(p: Path):
            label = robustness.timing_label_parts(p.name)
            return 0.03 if label[0] == "none" else 0.01

        robustness.qoe_run_metrics = lookup
        robustness.timing_tail_slow = none_tail_sick
        code_n = run_screen("p2", labels_4, seed="5302", out="p2neg.json")
        p2neg = json.loads((base / "p2neg.json").read_text(encoding="utf-8"))
        assert p2neg["verdict"] == "stop_null" and code_n == 3, (p2neg, code_n)

        # P2 confirm again with a healthy none tail (restore default tail).
        robustness.timing_tail_slow = lambda p: 0.01
        code_p2b = run_screen("p2", labels_4, seed="5302", out="p2b.json")
        p2b = json.loads((base / "p2b.json").read_text(encoding="utf-8"))
        assert p2b["verdict"] == "confirm" and code_p2b == 0, (p2b, code_p2b)

        # Lock: 3 screens, all >=5 pp (full AND onset) and both medians >=7 pp
        # -> lock file (contract v2 onset co-primary carried through).
        robustness.qoe_run_metrics = lookup
        screens = []
        for i, delta in enumerate((7.5, 8.0, 7.0)):
            s = dict(p1) if i == 0 else dict(p2a)
            s["stage"] = "p1" if i == 0 else "p2"
            s["seed"] = "5301" if i == 0 else f"530{i + 1}"
            s["delta_slow_pp"] = delta
            s["onset_delta_slow_pp"] = 38.0
            s["verdict"] = "candidate" if i == 0 else "confirm"
            screens.append(s)
            (base / f"lock_s{i}.json").write_text(json.dumps(s), encoding="utf-8")
        code_l = run_screen("lock", (), screen_json=[str(base / f"lock_s{i}.json")
                                                     for i in range(3)],
                            out="lock.json",
                            lock_out=str(base / "rq3tim_preflight_lock.json"))
        lock = json.loads((base / "lock.json").read_text(encoding="utf-8"))
        assert lock["verdict"] == "lock" and code_l == 0, (lock, code_l)
        assert lock["median_delta_pp"] == 7.5, lock
        assert lock["onset_median_delta_pp"] == 38.0, lock
        assert (base / "rq3tim_preflight_lock.json").exists()

        # Lock marginal: median <7 pp.
        for i, delta in enumerate((5.5, 5.0, 5.2)):
            s = {**screens[i], "delta_slow_pp": delta}
            (base / f"lockm_s{i}.json").write_text(json.dumps(s), encoding="utf-8")
        code_m = run_screen("lock", (), screen_json=[str(base / f"lockm_s{i}.json")
                                                     for i in range(3)],
                            out="lockm.json")
        lockm = json.loads((base / "lockm.json").read_text(encoding="utf-8"))
        assert lockm["verdict"] == "marginal" and code_m == 2, (lockm, code_m)

        # timing-campaign: 6 blocks x 3 arms x 2 cells.
        def campaign_metrics(p: Path) -> dict:
            cell, arm, block = robustness.timing_label_parts(p.name)
            if cell == "none":
                bad, timeout, slow = 0.003, 0.002, 0.006
            else:
                bad, timeout, slow = ((0.012, 0.01, 0.09) if arm == "event_only"
                                      else (0.004, 0.004, 0.01))
            pool = pooled(timeout, bad, slow)
            return {"run": p.name, "parts": (cell, arm, block),
                    "plateau": {"pooled": pool,
                                "per_lan": {1: dict(pool), 2: dict(pool)}},
                    "baseline": {"pooled": pooled(0.0, 0.0, 0.005),
                                 "per_lan": {1: pooled(0.0, 0.0, 0.005),
                                             2: pooled(0.0, 0.0, 0.005)}},
                    "floors": {"plateau_pooled_ge_5000": True,
                               "plateau_per_lan_ge_1000": True,
                               "baseline_pooled_ge_100": True,
                               "baseline_per_lan_ge_50": True},
                    "driver_clean": True, "baseline_http000_zero": True}

        robustness.qoe_rate_metrics = campaign_metrics
        robustness.timing_tail_slow = lambda p: 0.01

        def relief(p: Path):
            cell, arm, _ = robustness.timing_label_parts(p.name)
            if cell == "none" or arm == "event_only":
                return {"ttr_s": None, "new_backend_successes": 0,
                        "first_success_sent_s": None}
            return {"ttr_s": 25.0 if arm == "hybrid" else 15.0,
                    "new_backend_successes": 50, "first_success_sent_s": None}

        robustness.timing_relief_metrics = relief
        robustness.timing_onset_slow = onset_default
        fake_dirs = [str(base / f"rq3tim_{cell}_{arm}_{block}")
                     for block in range(1, 7)
                     for cell in ("none", "loss_all")
                     for arm in ("event_only", "hybrid", "reconcile")]
        code_t = robustness.timing_campaign_command(argparse.Namespace(
            run_dir=fake_dirs, rate="2.0", out=str(base / "campaign.json")))
        report = json.loads((base / "campaign.json").read_text(encoding="utf-8"))
        assert report["pass"] is True and code_t == 0, (report, code_t)
        assert report["H-T1"]["pass"] and report["H-T1_boundedness"]["pass"], report
        assert report["H-T1"]["onset_pass"] is True, report
        assert report["H-T2"]["pass"] and report["H-T3"]["pass"], report

        # timing-campaign onset negative: full-plateau contrast reproduces but
        # the onset-window co-primary does not (event_only onset == controls)
        # -> H-T1 onset_pass fails -> campaign does not pass.
        def onset_flat2(p: Path):
            return 0.02

        robustness.timing_onset_slow = onset_flat2
        code_to = robustness.timing_campaign_command(argparse.Namespace(
            run_dir=fake_dirs, rate="2.0", out=str(base / "campaign_noonset.json")))
        report_no = json.loads((base / "campaign_noonset.json").read_text(
            encoding="utf-8"))
        assert report_no["pass"] is False and code_to == 2, (report_no, code_to)
        assert report_no["H-T1"]["full_pass"] is True, report_no
        assert report_no["H-T1"]["onset_pass"] is False, report_no
        robustness.timing_onset_slow = onset_default

        # timing-campaign negative: controls tail slow >2 pp in 3 blocks.
        def tail_sick(p: Path):
            cell, arm, block = robustness.timing_label_parts(p.name)
            if cell == "loss_all" and arm == "hybrid" and block in ("1", "2", "3"):
                return 0.03
            return 0.01

        robustness.timing_tail_slow = tail_sick
        code_tn = robustness.timing_campaign_command(argparse.Namespace(
            run_dir=fake_dirs, rate="2.0", out=str(base / "campaign_neg.json")))
        report_n = json.loads((base / "campaign_neg.json").read_text(encoding="utf-8"))
        assert report_n["pass"] is False and code_tn == 2, (report_n, code_tn)
        assert "controls_tail_slow_gt_2pp" in report_n["block_exclusions"]["1"], report_n

        print(json.dumps({
            "timing_p1_candidate": "PASS", "timing_p1_control_elevated": "PASS",
            "timing_p1_hot_p95": "PASS",
            "timing_p1_cold_median": "PASS", "timing_p1_descend": "PASS",
            "timing_p1_onset_co_primary": "PASS",
            "timing_p2_confirm": "PASS", "timing_p2_none_stop_null": "PASS",
            "timing_lock": "PASS", "timing_lock_marginal": "PASS",
            "timing_campaign_pass": "PASS", "timing_campaign_negative": "PASS",
            "timing_campaign_onset_negative": "PASS",
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
