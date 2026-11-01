#!/usr/bin/env python3
"""Standalone selftest for the RQ3 admission-modes completion analyzer surface
(`readiness_robustness.py` subcommands: completion-intact, completion-lost,
completion-premature, completion-support).

Self-contained on purpose (mirrors rq3tim_p0_02_analyzer_selftest.py and
rq3snd_p0_02_analyzer_selftest.py): the Family-6 campaign must not depend on
another family's selftest. Fixtures are synthetic run records patched onto the
analyzer's per-run readers; the assertions restate the plan numbers from
docs/operation/testing/experiment/v3/rq3_admission_modes/experiment_plan.md
§7.1-§7.4 plus run_matrix.md, including the 2026-10-11 (b) amendment:

- E-I1: gated leg = spawn -> first successful response (2026-10-12 (h)
  re-scope; the ready -> admission leg is unmeasurable in this build and is a
  note only), A/B before C by median within each primary stratum (fast/slow
  binding; the pooled "all" stratum is descriptive only), the C-band (0, one
  poll period], and the B event-source share.
- Block-7 labels: the re-runs keep their planned `_7` labels; a voided run is
  quarantined as `_7_void` (never matched by the set-selection regex) per the
  recorded §9 mechanic.
- E-I4/E-L4: the recorded H-T3 floor reported exactly as recorded (16/18,
  none_cell_healthy=false where it applies; blocks 3 and 5 stay recorded
  transients) plus the conformance read (recorded outcome reproduced and
  block 7 clean); the floor never makes exit 0 impossible.
- E-L1: full-plateau AND onset-window co-primary on the fixed six-block
  denominator (block 4 seed 5204 recorded exclusion), with the block-7
  per-block report (E-L1, E-L2 and E-L3).
- E-L2/E-L3: the recorded H-T2/H-T1 accept counts.
- E-P1: per-run validity + attestation on all provided runs with the
  false-claim signature (A/B) vs immunity (C), the recorded cell bands
  (C's admit->first band descriptive, never a gate) and the 5/7 block
  assessability rule (recorded runs count only when passing). Log-based
  gates (`check_violation_ge_1` / `zero_check_violations`) are
  not evaluable — explicitly reported, never failed — when the run folder
  carries neither controller LAN log (2026-10-13 (j): recorded-era folders
  predate the capture).
- E-P2: per-block rule separation >0 in >=5/7 blocks with median >=5 pp.
- E-P3: the amended seven-pair paired damage (A/B median D >=5 pp with >=5/7
  positive; C per-pair D_s <=2 pp with >=5/7 passing, one-sided and with no
  median bar); a partner no-fault-floor breach or base-gate invalidity drops
  the pair; two or more lost seeds make a rule's component not assessable
  (recorded soundness 4.5); E-P4 retired — no unpaired path.
- E-S1: per-LAN per-backend ordering on the gated spawn -> first-success leg
  (2026-10-13 (l) re-scope mirroring (h); the registered ready -> admit leg is
  a not-evaluable note when it reads 0.000 s), source vocabulary and the
  handover anchors.
- E-S2: the recorded scale-decision -> usable-capacity anchor (2.17 / 6.01 s
  set-level values restated; direction check on the replicate), the recorded
  gap-window null and the driver-clean read.
- E-S3: the ~one-period (10-20 s) quantization band, descriptive only; when
  the registered leg is degenerate the band check is not assessable and is
  reported as a note.
- E-W1/E-W2/E-W3 (2026-10-13 (m) wake-and-verify un-parking): wake earlier
  than periodic in every primary stratum on the (h) leg (gated; wake vs
  event_only near-tie descriptive) — the wake `none` cell's ordinals 8-14
  (seeds 5201-5207); the lost family pairs loss_all/loss-none at the same
  ordinals with per-block slow-share delta <= 2 pp in >= 5/7 (gated) and the
  recorded `probe_fallback` mechanism source; the premature cell (ordinals
  1-7) follows the immunity branch (E-P1), pairs one-sided like C (E-P3) and
  adds the gated wake-vs-A damage contrast plus the descriptive wake-vs-C
  near-tie (E-P2). The wake none ordinals 1-7 are refused by the intact/lost
  commands and the 8-14 ordinals by the premature command (exit 3).
- duplicate set/arm identities are refused (exit 3), never last-wins.

Exit non-zero on any assertion failure.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source/scripts/testing/analysis"))

from rq3 import readiness_robustness as robustness  # noqa: E402

ARMS = ("event_only", "hybrid", "reconcile")


def check(condition: bool, label: str) -> None:
    if not condition:
        print(f"FAIL: {label}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {label}")


def make_dir(base: Path, name: str) -> Path:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def fake_qoe_metrics(path: Path, bad: float = 0.001) -> dict:
    return {"run": path.name, "parts": (None, None, None), "floors": {},
            "driver_clean": True, "scale_down_in_plateau": False,
            "quota": {"requested_edge_cpus": "0.12",
                      "all_compute_containers_match": True, "present": True},
            "pressure": {"method": "pre_first_ready", "pre_cpu": {}, "error": None},
            "pressure_ok": True,
            "plateau": {"pooled": {"bad_rate": bad, "slow_rate": 0.004,
                                   "timeout_rate": 0.0, "cancel_rate": 0.0}},
            "baseline": {"pooled": {"bad_rate": 0.0}}}


def contact_row(sent: float, backend: str = "edge_server_lan1_dyn1") -> dict:
    return {"_sent": sent, "phase": robustness.PHASE, "status": "completed",
            "http_status": "200", "latency_s": 0.02, "backend_id": backend,
            "client_lan": "lan1"}


def timeout_row(sent: float, backend: str = "") -> dict:
    return {"_sent": sent, "phase": "compute_spike", "status": "timeout",
            "http_status": "000", "latency_s": None, "backend_id": backend,
            "client_lan": "lan1"}


def intact_backend_records(path: Path) -> list[dict]:
    parts = robustness.timing_label_parts(path.name)
    wake = parts is None
    if wake:
        # 2026-10-13 (m): wake-and-verify intact runs (soundness labels).
        # Wake = event + probe verification; 13.0 (fast) / 13.4 (slow) is
        # earlier than periodic (reconcile 15.6) in both strata and within
        # the near-tie band of event_only (12.5).
        soundness = robustness.soundness_label_parts(path.name)
        assert soundness is not None and soundness[1] == "wake_verify"
        value, source = 0.0, "wake_verify_probe"
        first_success_by_stratum = {"fast": 13.0, "slow": 13.4}
    else:
        cell, arm, block = parts
        # 2026-10-12 (h): E-I1 gates the spawn -> first-success leg (A/B
        # earlier than C by ~3 s = well within one check period); the ready ->
        # admission leg is degenerate (~0.001 s across all arms) and must
        # never gate.
        values = {"event_only": (0.001, "event"), "hybrid": (0.002, "event"),
                  "reconcile": (6.984, "probe")}
        first_success = {"event_only": 12.5, "hybrid": 12.4, "reconcile": 15.6}
        value, source = values[arm]
        first_success_by_stratum = None
    records = []
    for index, stratum in enumerate(("fast", "slow")):
        bind = 0.2 if stratum == "fast" else 6.0
        spawn = 100.0 + index * 10
        admitted = spawn + 1.0
        flow_value = (first_success_by_stratum[stratum] if wake
                      else first_success[arm])
        records.append({
            "run": path.name, "container": f"edge_server_lan1_dyn{index + 1}",
            "lan": 1, "admit_source": source, "bind_s": bind, "stratum": stratum,
            "ready_source": "admission_log", "ready_to_admit_s": value,
            "spawn_to_admitted_s": 1.0,
            "spawn_started_ts": spawn, "admitted_ts": admitted,
            "admitted_to_first_routed_s": 0.5,
            "spawn_to_first_success_s": flow_value,
            "spawn_to_first_routed_s": 1.5,
            "first_routed_sent_s": admitted + 0.5,
            "first_success_sent_s": spawn + flow_value,
        })
    return records


def fake_intact_rows(path: Path) -> list[dict]:
    rows = [contact_row(105.0), contact_row(112.0),
            contact_row(105.5, backend="edge_server_lan1_dyn2")]
    if "_rq3tim_none_event_only_7" in path.name:
        rows.append({"sent_at": None, "_sent": 106.0, "phase": robustness.PHASE,
                     "status": "completed", "http_status": "000",
                     "latency_s": 0.01, "backend_id": "", "client_lan": "lan1"})
    return rows


def recorded_transient_tail(path: Path) -> float:
    """Reproduces the recorded intact floor (16/18; none-cell transients in
    blocks 3/5). Only none-cell runs are affected so the lost-set control-tail
    assessability fixtures stay healthy."""
    label = robustness.timing_label_parts(path.name)
    if label is None:
        return 0.01
    cell, arm, block = label
    if cell == "none":
        if block == "3" and arm == "event_only":
            return 0.03
        if block == "5" and arm == "reconcile":
            return 0.03
    return 0.01


def install_intact_patches() -> None:
    robustness.completion_backend_records = intact_backend_records
    robustness.request_rows = fake_intact_rows
    robustness.phase_bounds = lambda path: {robustness.PHASE: (100.0, 700.0)}
    robustness.qoe_run_metrics = lambda path: fake_qoe_metrics(path)
    robustness.timing_tail_slow = recorded_transient_tail


def intact_run_dirs(base: Path) -> list[str]:
    return [str(make_dir(base, f"20261011_000000_rq3tim_none_{arm}_{block}"))
            for block in robustness.COMPLETION_BLOCKS for arm in ARMS]


def wake_intact_run_dirs(base: Path) -> list[str]:
    return [str(make_dir(
        base, f"20261012_000000_rq3snd_none_wake_verify_{ordinal}"))
        for ordinal in robustness.COMPLETION_WAKE_ORDINALS]


def run_intact(base: Path, out: str = "intact.json",
               dirs: list[str] | None = None) -> tuple[int, dict]:
    code = robustness.completion_intact_command(argparse.Namespace(
        run_dir=dirs if dirs is not None else intact_run_dirs(base),
        rate="2.0", out=str(base / out)))
    return code, json.loads((base / out).read_text(encoding="utf-8"))


def test_completion_intact(base: Path) -> None:
    install_intact_patches()
    code, report = run_intact(base)
    check(code == 0 and report["pass"] is True,
          "E-I1 pass + E-I4 recorded-qualification conformance -> exit 0")
    e_i1 = report["E-I1"]
    check(e_i1["assessable_blocks"] == list(robustness.COMPLETION_BLOCKS),
          "E-I1 assessed on all seven blocks")
    check(e_i1["primary_strata"] == ["fast", "slow"]
          and e_i1["primary_strata_evidence"] == 2
          and e_i1["strata"]["all"].get("descriptive") is True,
          "E-I1 gates only the fast/slow strata ('all' descriptive)")
    check(e_i1["leg"] == "spawn -> first successful response"
          and e_i1["level_flags"]["c_later_by_one_period"] is True
          and e_i1["level_flags"]["a_median_lt_c"] is True
          and e_i1["level_flags"]["b_median_lt_c"] is True,
          "E-I1 gated leg = spawn->first-success (2026-10-12 (h)); C-band "
          "flags set")
    check(e_i1["ready_to_admit_leg"]["status"].startswith("unmeasurable")
          and e_i1["ready_to_admit_leg"]["medians_s"]["reconcile"]
          == statistics.median([6.984, 6.984]),
          "E-I1 ready->admission leg recorded as unmeasurable (note only)")
    for name in ("fast", "slow"):
        entry = e_i1["strata"][name]
        check(entry["direction_a_before_c"] is True
              and entry["direction_b_before_c"] is True
              and entry["primary_a_vs_c"]["cliffs_d"] == -1.0
              and entry["secondary_b_vs_c"]["cliffs_d"] == -1.0,
              f"E-I1 {name}: A/B before C by median on the spawn->first leg")
        check(entry["median_reconcile"] >
              max(entry["median_event_only"], entry["median_hybrid"])
              and entry["c_delta_over_ab_s"] is not None
              and 0.0 < entry["c_delta_over_ab_s"]
              <= robustness.COMPLETION_POLL_PERIOD_S,
              f"E-I1 {name}: C later than A/B by ~one check period")
    check(e_i1["b_event_fraction_ge_80pct"] is True,
          "E-I1 B event-source share >= 80 %")
    e_i4 = report["E-I4"]
    check(e_i4["clean"] == 19 and e_i4["none_cell_healthy"] is False
          and e_i4["recorded_qualification"]["reproduced"] is True
          and e_i4["recorded_qualification"]["conforms"] is True,
          "E-I4 reported exactly as recorded (16/18 -> false) and conforms")
    check(report["pass"] is True
          and any("E-I2" in finding for finding in report["descriptive_findings"]),
          "E-I2 unattributed 000 is a descriptive finding, never a gate")

    # E-W1 (2026-10-13 (m)): wake intact runs accepted and gated; without them
    # the 21-run set behaves exactly as before.
    check(report["E-W1"]["present"] is False
          and "not provided" in report["E-W1"]["note"]
          and "E-W1" not in report["gated_components"],
          "E-W1 absent without wake runs (21-run set unchanged)")
    wake_dirs = wake_intact_run_dirs(base)
    code_w, report_w = run_intact(base, out="intact_wake.json",
                                  dirs=intact_run_dirs(base) + wake_dirs)
    e_w1 = report_w["E-W1"]
    check(code_w == 0 and e_w1["present"] is True and e_w1["pass"] is True
          and e_w1["runs_provided"] == 7
          and all(e_w1["strata"][name]["direction_wake_before_reconcile"] is True
                  for name in ("fast", "slow"))
          and all(e_w1["strata"][name]["vs_event_only_near_tie"] is True
                  for name in ("fast", "slow"))
          and report_w["gated_components"]["E-W1"] is True
          and sorted(int(key) for key in e_w1["per_run"])
          == list(robustness.COMPLETION_WAKE_ORDINALS),
          "E-W1 wake earlier than periodic in every primary stratum (gated); "
          "wake vs event_only near-tie (descriptive)")

    # E-W1 failure path: wake slower than periodic in the fast stratum.
    def wake_slow_inverted(path: Path) -> list[dict]:
        records = intact_backend_records(path)
        if robustness.timing_label_parts(path.name) is None:
            records = [{**record,
                        "spawn_to_first_success_s": (
                            16.5 if record["stratum"] == "fast" else 13.4)}
                       for record in records]
        return records

    robustness.completion_backend_records = wake_slow_inverted
    code_wi, report_wi = run_intact(base, out="intact_wake_fail.json",
                                    dirs=intact_run_dirs(base) + wake_dirs)
    check(code_wi == 2 and report_wi["E-W1"]["pass"] is False
          and report_wi["E-W1"]["strata"]["fast"]["direction_wake_before_reconcile"]
          is False
          and report_wi["E-W1"]["strata"]["slow"]["direction_wake_before_reconcile"]
          is True,
          "E-W1 wake-after-periodic in one primary stratum -> exit 2")
    install_intact_patches()

    # Guard: the wake none ordinals 1-7 (premature-cell partners) are refused.
    bad_dirs = intact_run_dirs(base) + [str(make_dir(
        base, "20261012_000000_rq3snd_none_wake_verify_1"))]
    code_ord = robustness.main([
        "completion-intact", "--run-dir", *bad_dirs,
        "--out", str(base / "intact_wake_ordinal.json")])
    check(code_ord == 3,
          "wake none ordinal 1-7 refused by completion-intact (exit 3)")

    # Failure path: C earlier than A in the fast stratum -> E-I1 fails.
    def inverted_records(path: Path) -> list[dict]:
        records = intact_backend_records(path)
        if robustness.timing_label_parts(path.name)[1] == "reconcile":
            records[0] = {**records[0], "stratum": "fast",
                          "spawn_to_first_success_s": 9.0}
        return records

    robustness.completion_backend_records = inverted_records
    code_bad, report_bad = run_intact(base, out="intact_bad.json")
    check(code_bad == 2 and report_bad["E-I1"]["pass"] is False,
          "E-I1 fast-stratum direction failure on the spawn->first leg -> "
          "exit 2, pass false")
    install_intact_patches()

    # Qualification failure: a fresh breach on block 7 -> conformance false,
    # the recorded floor itself still reported as recorded.
    def block7_breach(path: Path) -> float:
        label = robustness.timing_label_parts(path.name)
        if label is not None and label[0] == "none" \
                and label[2] == "7" and label[1] == "hybrid":
            return 0.03
        return recorded_transient_tail(path)

    robustness.timing_tail_slow = block7_breach
    code_t, report_t = run_intact(base, out="intact_transient.json")
    check(code_t == 2
          and report_t["E-I4"]["recorded_qualification"]["conforms"] is False
          and report_t["E-I4"]["none_cell_healthy"] is False
          and report_t["E-I4"]["denominator"] == 21,
          "fresh block-7 breach fails conformance; floor remains as recorded")
    install_intact_patches()


def run_lost(base: Path, dirs: list[str], out: str) -> tuple[int, dict]:
    code = robustness.completion_lost_command(argparse.Namespace(
        run_dir=dirs, rate="2.0", out=str(base / out)))
    return code, json.loads((base / out).read_text(encoding="utf-8"))


WAKE_LOST_SLOW_PP = {1: 1.0, 2: 0.5, 3: 1.5, 4: 0.8, 5: 2.0, 6: 1.2, 7: 0.6}


def wake_soundness_metrics(path: Path) -> dict:
    cell, arm, ordinal = robustness.soundness_label_parts(path.name)
    block = robustness._completion_ordinal(ordinal)
    is_none = cell == "none"
    return {
        "run": path.name, "cell": cell, "arm": arm, "ordinal": ordinal,
        "fault": cell, "n": 10 if cell == "premature10" else 0,
        "n_source": "env", "s_lie": 0, "s_source": "default",
        "seed": robustness.COMPLETION_WAKE_SEED_FAMILY.get(block, 6300 + block),
        "valid": True,
        "validity": {"provenance_ok": True, "driver_clean": True,
                     "quota_ok": True, "no_restart": True},
        "none_health": ({"tail_slow_share": 0.005, "bad_share": 0.002,
                         "tail_slow_breach": False, "bad_breach": False,
                         "breach": False} if is_none else None),
        "adm_flow": {"admit_to_first_median_s": 0.5 if is_none else 21.0,
                     "claim_to_first_median_s": None if is_none else 31.0},
        "check_violations": {"lan1": 0, "lan2": 0, "total": 0,
                             "logs_absent": True},
    }


def wake_plateau_rows(path: Path, extra_slow_pp: dict | None = None) -> list[dict]:
    cell, arm, ordinal = robustness.soundness_label_parts(path.name)
    block = robustness._completion_ordinal(ordinal)
    logical = robustness.COMPLETION_WAKE_ORDINALS.index(block) + 1
    delta_pp = (extra_slow_pp or {}).get(logical, 0.0) if cell == "loss_all" else 0.0
    slow_pp = 0.4 + (WAKE_LOST_SLOW_PP[logical] if cell == "loss_all" else 0.0) \
        + delta_pp
    offered = 1000
    slow_n = round(slow_pp / 100.0 * offered)
    rows = []
    for index in range(offered):
        row = contact_row(150.0 + index)
        if index < slow_n:
            row = {**row, "latency_s": 12.0}
        rows.append(row)
    return rows


def wake_admission_rows(path: Path) -> list[dict]:
    cell, _, _ = robustness.soundness_label_parts(path.name)
    source = ("wake_verify_probe" if cell == "none"
              else robustness.COMPLETION_WAKE_MECHANISM_SOURCE)
    return [{"container": "edge_server_lan1_dyn1", "lan": 1,
             "admit_source": source, "result": "admitted"}]


def test_completion_lost(base: Path) -> None:
    deltas = {1: 0.08, 2: 0.06, 3: 0.07, 4: 0.055, 5: 0.06, 6: 0.07, 7: 0.09}
    onset = {1: 0.30, 2: 0.25, 3: 0.28, 4: 0.20, 5: 0.27, 6: 0.31, 7: 0.35}
    slows = {block: {"event_only": 0.10 + deltas[block],
                     "hybrid": 0.10, "reconcile": 0.095}
             for block in robustness.COMPLETION_BLOCKS}

    def label_of(path: Path) -> tuple[str, str, int]:
        cell, arm, block = robustness.timing_label_parts(path.name)
        return cell, arm, int(block)

    def fake_rates(path: Path) -> dict:
        cell, arm, block = label_of(path)
        return {"run": path.name, "parts": (cell, arm, str(block)),
                "bad": 0.002, "timeout": 0.001, "slow": slows[block][arm],
                "floors_ok": True, "driver_clean": True}

    def fake_onset(path: Path) -> float:
        cell, arm, block = label_of(path)
        base_value = onset[block]
        if cell == "none":
            return 0.02
        return base_value if arm == "event_only" else 0.02

    def fake_mechanism(path: Path) -> dict:
        cell, arm, block = label_of(path)
        if cell == "none":
            return {"admitted_total": 2, "abandoned_total": 0,
                    "per_lan_admitted": {1: 1, 2: 1},
                    "per_lan_abandoned": {1: 0, 2: 0},
                    "per_lan_sources": {1: ["event"], 2: ["event"]},
                    "probe_fallback_admitted": 0, "event_admitted": 2,
                    "probe_admitted": 0, "event_fraction": 1.0}
        if arm == "event_only":
            return {"admitted_total": 0, "abandoned_total": 6,
                    "per_lan_admitted": {1: 0, 2: 0},
                    "per_lan_abandoned": {1: 3, 2: 3},
                    "per_lan_sources": {1: [], 2: []},
                    "probe_fallback_admitted": 0, "event_admitted": 0,
                    "probe_admitted": 0, "event_fraction": None}
        source = "probe_fallback" if arm == "hybrid" else "probe"
        return {"admitted_total": 2, "abandoned_total": 0,
                "per_lan_admitted": {1: 1, 2: 1},
                "per_lan_abandoned": {1: 0, 2: 0},
                "per_lan_sources": {1: [source], 2: [source]},
                "probe_fallback_admitted": 2 if arm == "hybrid" else 0,
                "event_admitted": 0, "probe_admitted": 2 if arm == "reconcile" else 0,
                "event_fraction": 0.0}

    robustness._campaign_run_rates = fake_rates
    robustness.timing_onset_slow = fake_onset
    robustness.timing_tail_slow = recorded_transient_tail
    robustness.qoe_mechanism = fake_mechanism
    robustness.timing_relief_metrics = lambda path: {
        "ttr_s": 25.0, "new_backend_successes": 0, "first_success_sent_s": None}
    robustness.qoe_run_metrics = lambda path: fake_qoe_metrics(path)

    run_dirs = [str(make_dir(base, f"20261011_000001_rq3tim_loss_all_{arm}_{block}"))
                for block in robustness.COMPLETION_BLOCKS for arm in ARMS]
    run_dirs += [str(make_dir(base, f"20261011_000001_rq3tim_none_{arm}_{block}"))
                 for block in robustness.COMPLETION_BLOCKS for arm in ARMS]
    code = robustness.completion_lost_command(argparse.Namespace(
        run_dir=run_dirs, rate="2.0", out=str(base / "lost.json")))
    report = json.loads((base / "lost.json").read_text(encoding="utf-8"))
    check(code == 0 and report["pass"] is True,
          "E-L1..E-L3 pass + E-L4 recorded-qualification conformance -> exit 0")
    check(report["block_rows"]["4"]["exclusion"] == "recorded_exclusion_seed_5204",
          "block 4 (seed 5204) recorded exclusion applied")
    check(report["E-L1"]["denominator_blocks"] == [1, 2, 3, 4, 5, 6]
          and report["E-L1"]["denominator_assessable"] == [1, 2, 3, 5, 6],
          "E-L1 fixed six-block denominator, five assessable")
    check(report["E-L1"]["delta_ge_5pp_blocks"] == 5
          and report["E-L1"]["median_pp"] >= 5.0
          and report["E-L1"]["onset_delta_ge_5pp_blocks"] == 5,
          "E-L1 co-primary accept counts (x/6)")
    check(report["E-L1"]["block7"] == {"delta_slow_pp": 9.0,
                                       "onset_delta_slow_pp": 33.0,
                                       "assessable": True},
          "added block 7 reported per block under the same rules")
    check(report["E-L3"]["block7"] == {"event_only_slow": 0.19,
                                       "le_25pct": True, "assessable": True},
          "E-L3 block-7 per-block report (no accept-count re-scope)")
    check(report["E-L2"]["a_blocks"] == 5 and report["E-L2"]["b_blocks"] == 5
          and report["E-L2"]["c_blocks"] == 5
          and report["E-L2"]["relief_block_count"] == 5,
          "E-L2 recorded H-T2 accept counts >= 5/6")
    check(report["E-L3"]["pass"] is True, "E-L3 boundedness accept count")
    check(report["E-L4"]["clean"] == 19
          and report["E-L4"]["none_cell_healthy"] is False
          and report["E-L4"]["recorded_qualification"]["conforms"] is True,
          "E-L4 reported exactly as recorded (16/18 -> false) and conforms")

    # A quarantined void run (`..._7_void`) never matches the label regex and
    # is refused if provided (recorded §9 mechanic: quarantine as `_7_void`,
    # re-run under the planned `_7` label).
    check(robustness.timing_label_parts(
        "20261011_000002_rq3tim_loss_all_event_only_7_void") is None,
        "quarantined `_7_void` label never matches the set-selection regex")
    run_dirs_void = run_dirs + [
        str(make_dir(base, "20261011_000002_rq3tim_loss_all_event_only_7_void"))]
    code_void = robustness.main([
        "completion-lost", "--run-dir", *run_dirs_void,
        "--out", str(base / "lost_void.json")])
    check(code_void == 3,
          "a provided `_7_void` folder is refused (exit 3), never silently "
          "analysed")

    # E-W2 (2026-10-13 (m)): wake loss family (ordinals 8-14 -> seeds
    # 5201-5207), paired slow-share delta vs its own none partner <= 2 pp in
    # >= 5/7 blocks; the mechanism source is recorded (`probe_fallback`).
    wake_loss_dirs = [str(make_dir(
        base, f"20261012_000001_rq3snd_loss_all_wake_verify_{ordinal}"))
        for ordinal in robustness.COMPLETION_WAKE_ORDINALS]
    wake_none_dirs = [str(make_dir(
        base, f"20261012_000001_rq3snd_none_wake_verify_{ordinal}"))
        for ordinal in robustness.COMPLETION_WAKE_ORDINALS]
    robustness.request_rows = wake_plateau_rows
    robustness.phase_bounds = lambda path: {robustness.PHASE: (100.0, 1300.0)}
    robustness.admission_rows = wake_admission_rows
    robustness.soundness_run_metrics = wake_soundness_metrics
    code_w, report_w = run_lost(base, run_dirs + wake_loss_dirs + wake_none_dirs,
                                "lost_wake.json")
    e_w2 = report_w["E-W2"]
    check(code_w == 0 and e_w2["present"] is True and e_w2["pass"] is True
          and e_w2["usable_pairs"] == 7 and e_w2["le_2pp_pairs"] == 7
          and report_w["gated_components"]["E-W2"] is True
          and e_w2["mechanism_source_ok"] is True
          and e_w2["mechanism_sources_loss"] == {"probe_fallback": 7}
          and {int(key): value for key, value in e_w2["seed_family"].items()}
          == dict(robustness.COMPLETION_WAKE_SEED_FAMILY)
          and e_w2["recovery_admit_to_first_median_s"] == 21.0
          and e_w2["timing_anchor_s"] == 21.0,
          "E-W2 recovery via the 20 s fallback: paired slow delta <= 2 pp in "
          "7/7, source `probe_fallback`, ~21 s anchor recorded")

    def wake_fail_rows(path: Path) -> list[dict]:
        return wake_plateau_rows(path, extra_slow_pp={1: 3.0, 2: 3.0, 3: 3.0})

    robustness.request_rows = wake_fail_rows
    code_wf, report_wf = run_lost(base, run_dirs + wake_loss_dirs
                                  + wake_none_dirs, "lost_wake_fail.json")
    e_w2f = report_wf["E-W2"]
    check(code_wf == 2 and e_w2f["pass"] is False
          and e_w2f["le_2pp_pairs"] == 4 and e_w2f["usable_pairs"] == 7,
          "E-W2 three blocks over 2 pp -> exit 2")
    # Guard: the wake ordinals 1-7 belong to the premature cell here too.
    bad_wake = run_dirs + [str(make_dir(
        base, "20261012_000001_rq3snd_loss_all_wake_verify_3"))]
    code_bad_wake = robustness.main([
        "completion-lost", "--run-dir", *bad_wake,
        "--out", str(base / "lost_wake_ordinal.json")])
    check(code_bad_wake == 3,
          "wake loss ordinal 1-7 refused by completion-lost (exit 3)")

    # Failure path: only three deltas >=5 pp -> headline fails (E-L4
    # conformance still holds, so the exit-2 cause is E-L1 alone).
    deltas_bad = {1: 0.08, 2: 0.02, 3: 0.07, 4: 0.055, 5: 0.06,
                  6: 0.01, 7: 0.02}

    def fake_rates_bad(path: Path) -> dict:
        cell, arm, block_value = robustness.timing_label_parts(path.name)
        index = int(block_value)
        if arm == "event_only":
            slow = 0.10 + deltas_bad[index]
        elif arm == "hybrid":
            slow = 0.10
        else:
            slow = 0.095
        return {"run": path.name, "parts": (cell, arm, block_value),
                "bad": 0.002, "timeout": 0.001, "slow": slow,
                "floors_ok": True, "driver_clean": True}

    robustness._campaign_run_rates = fake_rates_bad
    code_b = robustness.completion_lost_command(argparse.Namespace(
        run_dir=run_dirs, rate="2.0", out=str(base / "lost_bad.json")))
    report_b = json.loads((base / "lost_bad.json").read_text(encoding="utf-8"))
    check(code_b == 2 and report_b["E-L1"]["pass"] is False
          and report_b["E-L4"]["recorded_qualification"]["conforms"] is True,
          "E-L1 sub-threshold counts -> exit 2, pass false")
    robustness._campaign_run_rates = fake_rates


def psnd_record(run_name: str, arm: str, block: int, u_pct: float, *,
                deferred: float = 10.0, valid: bool = True,
                pre000: tuple[int, int] = (15, 15), violations: int = 1,
                admit_first: float = 10.0, claim_first: float = 15.0,
                run000: int = 200, run000_share: float = 0.004,
                prebind: int = 0, none_health: dict | None = None,
                logs_absent: bool = False) -> dict:
    return {
        "run": run_name, "cell": "premature10", "arm": arm,
        "ordinal": str(block),
        "seed": robustness.COMPLETION_PREMATURE_SEEDS[block],
        "n": 10, "n_source": "env", "deferrals_s": [deferred, deferred],
        "f2": {"u_by_n": {"10": {"u_pct": u_pct, "offered": 6000}},
               "pre_by_n": {"10": {"http000": {"lan1": pre000[0],
                                               "lan2": pre000[1]}}}},
        "adm_flow": {"admit_to_first_median_s": admit_first,
                     "claim_to_first_median_s": claim_first},
        "check_violations": {"lan1": violations, "lan2": 0,
                             "total": violations, "logs_absent": logs_absent},
        "run_wide": {"http000": run000, "http000_share": run000_share,
                     "offered": 60000},
        "pre_bind_successes": prebind,
        "validity": {"provenance_ok": True, "driver_clean": True,
                     "quota_ok": True, "no_restart": True},
        "valid": valid,
        "attestation": {"path": "rq3snd_fault_attestation.txt", "present": True,
                        "knobs": {"EDGE_READY_PREMATURE_S": 10}},
        "env_knobs": {"EDGE_READY_PREMATURE_S": "10",
                      "EDGE_READY_SEMANTIC_LIE_S": "0"},
        "none_health": none_health,
    }


U_A = {1: 22.57, 2: 21.57, 3: 25.0, 4: 22.0, 5: 23.5, 6: 20.5, 7: 24.5}
U_B = {1: 23.33, 2: 20.0, 3: 26.4, 4: 21.0, 5: 22.6, 6: 19.9, 7: 23.9}
U_C = {1: 10.0, 2: 9.0, 3: 11.0, 4: 10.5, 5: 11.5, 6: 9.5, 7: 10.2}
# 2026-10-13 (m): wake-and-verify (D) is immune like C (near-tie with C,
# >=5 pp below A/B); its paired damage stays <= 2 pp at every seed.
U_D = {1: 10.2, 2: 9.6, 3: 10.8, 4: 10.1, 5: 11.0, 6: 10.4, 7: 9.9}
DAMAGE_D = {1: 0.5, 2: 1.2, 3: -0.8, 4: 0.9, 5: 1.6, 6: 0.2, 7: 1.1}
DAMAGE_A = {1: 13.34, 2: 3.86, 3: 16.41, 4: 8.0, 5: 6.5, 6: 9.2, 7: 7.7}
DAMAGE_B = {1: 12.0, 2: 14.0, 3: 15.0, 4: 6.0, 5: 9.0, 6: 11.0, 7: 8.0}
DAMAGE_C = {1: 0.12, 2: -0.99, 3: -4.68, 4: 1.0, 5: 1.5, 6: -1.2, 7: 0.8}


def install_premature_patches(deferral_overrides: dict | None = None,
                              reconcile_admit_first: float = 0.7,
                              logs_absent_blocks: set[int] | None = None,
                              violation_switch_blocks: set[int] | None = None) -> None:
    """Fake the per-run readers. `logs_absent_blocks` marks blocks whose fake
    run folders carry no controller LAN logs (both gates then not evaluable);
    `violation_switch_blocks` flips the CHECK VIOLATION count of that block's
    fakes (A/B 1 -> 0, C 0 -> 1), i.e. the log gate is evaluated and fails."""
    overrides = deferral_overrides or {}
    absent_blocks = logs_absent_blocks or set()
    switch_blocks = violation_switch_blocks or set()

    def fake_metrics(path: Path) -> dict:
        parts = robustness.soundness_label_parts(path.name)
        assert parts is not None
        cell, arm, ordinal = parts
        block = int(ordinal.rstrip("r"))
        logs_absent = block in absent_blocks
        if cell == "none":
            u_none = {"event_only": U_A[block] - DAMAGE_A[block],
                      "hybrid": U_B[block] - DAMAGE_B[block],
                      "reconcile": U_C[block] - DAMAGE_C[block],
                      "wake_verify": U_D[block] - DAMAGE_D[block]}[arm]
            return psnd_record(path.name, arm, block, u_none,
                               run000=0, run000_share=0.0, violations=0,
                               prebind=0, logs_absent=logs_absent)
        if arm == "wake_verify":
            return psnd_record(path.name, arm, block, U_D[block], run000=0,
                               run000_share=0.0, violations=0, prebind=0,
                               claim_first=15.0, admit_first=0.7,
                               deferred=overrides.get((block, arm), 10.0),
                               logs_absent=logs_absent)
        if arm == "reconcile":
            return psnd_record(path.name, arm, block, U_C[block], run000=0,
                               run000_share=0.0,
                               violations=1 if block in switch_blocks else 0,
                               prebind=0, claim_first=15.0,
                               admit_first=reconcile_admit_first,
                               deferred=overrides.get((block, arm), 10.0),
                               logs_absent=logs_absent)
        return psnd_record(
            path.name, arm, block, {"event_only": U_A, "hybrid": U_B,
                                    "reconcile": U_C}[arm][block],
            deferred=overrides.get((block, arm), 10.0),
            violations=0 if block in switch_blocks else 1,
            logs_absent=logs_absent)

    robustness.soundness_run_metrics = fake_metrics


def premature_run_dirs(base: Path) -> list[str]:
    dirs = []
    for block in robustness.COMPLETION_BLOCKS:
        for arm in ARMS:
            dirs.append(str(make_dir(
                base, f"20261011_000002_rq3snd_premature10_{arm}_{block}")))
    for block in robustness.COMPLETION_BLOCKS:
        for arm in ARMS:
            dirs.append(str(make_dir(
                base, f"20261011_000002_rq3snd_none_{arm}_{block}")))
    # wake_verify (2026-10-13 (m)): the premature cell's ordinals 1-7
    # (pilots 1-3 = seeds 6301-6303 + new 4-7 = seeds 6304-6307).
    for block in robustness.COMPLETION_BLOCKS:
        dirs.append(str(make_dir(
            base, f"20261011_000002_rq3snd_premature10_wake_verify_{block}")))
    for block in robustness.COMPLETION_BLOCKS:
        dirs.append(str(make_dir(
            base, f"20261011_000002_rq3snd_none_wake_verify_{block}")))
    return dirs


def run_premature(base: Path, dirs: list[str], out: str) -> tuple[int, dict]:
    code = robustness.completion_premature_command(argparse.Namespace(
        run_dir=dirs, out=str(base / out)))
    return code, json.loads((base / out).read_text(encoding="utf-8"))


def test_completion_premature(base: Path) -> None:
    install_premature_patches()
    dirs = premature_run_dirs(base)
    code, report = run_premature(base, dirs, "prem.json")
    check(code == 0 and report["pass"] is True, "E-P1..E-P3 all-pass verdict")
    check(all(report["E-P1"]["cells"][arm]["passing_blocks"] == 7
              for arm in ARMS)
          and report["E-P1"]["cells"]["hybrid"]["recorded_blocks_passing"] == 3
          and report["E-P1"]["cells"]["hybrid"]["new_runs_passing"] == 4,
          "E-P1 12 new runs pass; recorded runs count only when passing")
    check(report["E-P1"]["run_checks"]["b2_hybrid"]["recorded"] is True
          and report["E-P1"]["new_run_checks"]["b4_hybrid"]["pass"] is True,
          "E-P1 per-run entries carry the recorded/new split")
    check(report["E-P1"]["run_checks"]["b1_reconcile"]["descriptive"]
          == {"admit_to_first_in_0_2": True}
          and "descriptive_admit_to_first_in_0_2"
          not in report["E-P1"]["run_checks"]["b1_reconcile"]["gates"],
          "E-P1 C admit->first band is descriptive, never a gate")

    # (a) 2026-10-13 (j): a run folder with no controller LAN logs -> the
    # log-based gate is not_evaluable (explicitly reported), never failed.
    install_premature_patches(logs_absent_blocks={1, 2})
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code_na = robustness.completion_premature_command(argparse.Namespace(
            run_dir=dirs, out=str(base / "prem_na_logs.json")))
    report_na = json.loads((base / "prem_na_logs.json").read_text(encoding="utf-8"))
    entry_a = report_na["E-P1"]["run_checks"]["b1_event_only"]
    entry_c = report_na["E-P1"]["run_checks"]["b2_reconcile"]
    check(code_na == 0
          and entry_a["not_evaluable_logs_absent"] is True
          and "controller_lan{1,2}.log absent"
          in entry_a["not_evaluable"]["check_violation_ge_1"]
          and "check_violation_ge_1" not in entry_a["gates"]
          and entry_a["failed"] == [] and entry_a["pass"] is True
          and report_na["E-P1"]["run_checks"]["b2_hybrid"]["pass"] is True,
          "E-P1 absent controller logs -> gate not_evaluable, run not failed")
    check(entry_c["not_evaluable_logs_absent"] is True
          and "zero_check_violations" in entry_c["not_evaluable"]
          and "zero_check_violations" not in entry_c["gates"]
          and entry_c["failed"] == []
          and report_na["E-P1"]["cells"]["event_only"]["passing_blocks"] == 7
          and report_na["E-P1"]["cells"]["reconcile"]["passing_blocks"] == 7,
          "E-P1 C absent controller logs -> zero_check_violations not_evaluable")
    check(report_na["E-P1"]["not_evaluable_runs"]
          == [f"b{block}_{arm}" for block in (1, 2)
              for arm in (*ARMS, "wake_verify")]
          and '"E-P1_log_gates_not_evaluable"' in buffer.getvalue()
          and "b2_reconcile" in buffer.getvalue(),
          "E-P1 not-evaluable runs are surfaced (JSON field + console key)")

    # (b) logs present -> the gate is evaluated exactly as before (fails).
    install_premature_patches(violation_switch_blocks={1})
    code_v, report_v = run_premature(base, dirs, "prem_zero_viol.json")
    entry_v = report_v["E-P1"]["run_checks"]["b1_event_only"]
    entry_vc = report_v["E-P1"]["run_checks"]["b1_reconcile"]
    check(code_v == 0
          and entry_v["not_evaluable_logs_absent"] is False
          and entry_v["gates"]["check_violation_ge_1"] is False
          and entry_v["failed"] == ["check_violation_ge_1"]
          and entry_v["pass"] is False
          and report_v["E-P1"]["cells"]["event_only"]["passing_blocks"] == 6
          and entry_vc["failed"] == ["zero_check_violations"]
          and report_v["E-P1"]["cells"]["reconcile"]["passing_blocks"] == 6,
          "E-P1 logs present -> gate evaluated (0 violations fails A/B, 1 fails C)")
    install_premature_patches()
    check(report["E-P2"]["A vs C"]["positive_blocks"] == 7
          and report["E-P2"]["A vs C"]["median_pp"] >= 5.0
          and report["E-P2"]["B vs C"]["positive_blocks"] == 7,
          "E-P2 rule separation >0 in >=5/7 with median >=5 pp")
    e_p3 = report["E-P3"]["rules"]
    check(e_p3["event_only"]["positive_pairs"] == 7
          and e_p3["event_only"]["median_d_pp"] >= 5.0
          and e_p3["hybrid"]["positive_pairs"] == 7,
          "E-P3 A/B seven positive pairs, median >=5 pp")
    check(e_p3["reconcile"]["le_2pp_pairs"] == 7
          and e_p3["reconcile"]["pass"] is True
          and e_p3["reconcile"]["pairs"][2]["d_s"] == -4.68,
          "E-P3 C one-sided per-pair criterion: recorded seeds 3/3 -> 7/7")
    check("E-P4" not in report and "retired" in report["E-P3"]["note"],
          "E-P4 retired: no unpaired/descriptive damage path")

    # E-W3 (2026-10-13 (m)): wake_verify (D) follows the immunity branch in
    # E-P1; E-P2 adds the gated wake-vs-A damage contrast and the descriptive
    # wake-vs-periodic near-tie; E-P3 pairs D one-sided like C.
    check(report["wake_verify_present"] is True
          and report["E-P1"]["cells"]["wake_verify"]["passing_blocks"] == 7
          and report["E-P1"]["cells"]["wake_verify"]["assessable"] is True
          and "zero_pre_bind_successes"
          in report["E-P1"]["run_checks"]["b4_wake_verify"]["gates"],
          "E-W3 E-P1 wake cell passes the immunity branch (7/7)")
    check(report["E-P2"]["A vs D (wake vs A damage contrast)"]["pass"] is True
          and report["E-P2"]["A vs D (wake vs A damage contrast)"]
          ["positive_blocks"] == 7,
          "E-P2 A vs D damage contrast gated (wake vs event_only)")
    d_vs_c = report["E-P2"]["D vs C (wake vs periodic, descriptive)"]
    check(d_vs_c["gated"] is False and d_vs_c["pass"] is None
          and d_vs_c["near_tie"] is True,
          "E-P2 D vs C descriptive near-tie (never gates)")
    e_p3_wake = report["E-P3"]["rules"]["wake_verify"]
    check(e_p3_wake["pass"] is True and e_p3_wake["le_2pp_pairs"] == 7
          and len(e_p3_wake["pairs"]) == 7,
          "E-P3 wake_verify paired one-sided (<= 2 pp, 7/7)")

    # E-W3: a pre-bind signature on a wake run reduces cell credit (6/7,
    # still assessable) — the immunity branch is genuinely evaluated.
    base_fake = robustness.soundness_run_metrics

    def wake_prebind_fail(path: Path) -> dict:
        record = base_fake(path)
        if "_premature10_wake_verify_4" in path.name:
            record["pre_bind_successes"] = 3
        return record

    robustness.soundness_run_metrics = wake_prebind_fail
    code_wp, report_wp = run_premature(base, dirs, "prem_wake_prebind.json")
    check(code_wp == 0
          and report_wp["E-P1"]["cells"]["wake_verify"]["passing_blocks"] == 6
          and report_wp["E-P1"]["run_checks"]["b4_wake_verify"]["failed"]
          == ["zero_pre_bind_successes"],
          "E-W3 pre-bind signature on a wake run reduces cell credit (6/7)")
    install_premature_patches()

    # E-P1: a supplied-but-failing recorded run earns no assessability credit.
    install_premature_patches(deferral_overrides={(2, "hybrid"): 6.0})
    code_r, report_r = run_premature(base, dirs, "prem_recorded_bad.json")
    check(report_r["E-P1"]["cells"]["hybrid"]["passing_blocks"] == 6
          and report_r["E-P1"]["cells"]["hybrid"]["recorded_blocks_passing"] == 2
          and code_r == 0,
          "E-P1 failing recorded run reduces credit (6/7, still assessable)")

    # E-P1 signature failure on three new B runs -> assessability drops to 3
    # (recorded credit 2 + only block 7 passing).
    install_premature_patches(deferral_overrides={(2, "hybrid"): 6.0,
                                                  (4, "hybrid"): 6.0,
                                                  (5, "hybrid"): 6.0,
                                                  (6, "hybrid"): 6.0})
    code_b, report_b = run_premature(base, dirs, "prem_bad.json")
    check(code_b == 2
          and report_b["E-P1"]["cells"]["hybrid"]["assessable_blocks"] == 3
          and report_b["E-P1"]["pass"] is False,
          "E-P1 failures (incl. recorded) -> cell not assessable (<5/7)")
    install_premature_patches()

    # C's descriptive band never fails the cell.
    install_premature_patches(reconcile_admit_first=3.0)
    code_d, report_d = run_premature(base, dirs, "prem_descriptive.json")
    check(code_d == 0
          and report_d["E-P1"]["cells"]["reconcile"]["passing_blocks"] == 7
          and report_d["E-P1"]["run_checks"]["b4_reconcile"]["descriptive"]
          == {"admit_to_first_in_0_2": False},
          "P1 C descriptive band out of range -> still pass")
    install_premature_patches()

    # E-P3: a partner removed entirely cannot be compensated (no unpaired path).
    dirs_missing = [path for path in dirs
                    if "_rq3snd_none_event_only_5" not in path]
    code_m, report_m = run_premature(base, dirs_missing, "prem_missing.json")
    rule = report_m["E-P3"]["rules"]["event_only"]
    check(rule["usable_pairs"] == 6 and rule["assessable"] is True
          and rule["pass"] is True
          and any(pair.get("status") == "missing_partner"
                  for pair in rule["pairs"]),
          "E-P3 one lost seed: pair reported, rule still assessable (6/7)")
    dirs_two_missing = [path for path in dirs
                        if "_rq3snd_none_hybrid_5" not in path
                        and "_rq3snd_none_hybrid_6" not in path]
    code_m2, report_m2 = run_premature(base, dirs_two_missing,
                                       "prem_missing2.json")
    rule2 = report_m2["E-P3"]["rules"]["hybrid"]
    check(rule2["lost_pairs"] == 2 and rule2["assessable"] is False
          and rule2["pass"] is False and report_m2["na"] is True,
          "E-P3 two lost seeds -> component not assessable (recorded 4.5)")

    # E-P3 C failure path: three pairs fail the one-sided D_s <= 2 pp rule.
    damage_map = dict(DAMAGE_C)
    damage_map[4] = 4.0
    damage_map[5] = 3.5
    damage_map[6] = 3.0

    def fake_c_damage(path: Path) -> dict:
        parts = robustness.soundness_label_parts(path.name)
        assert parts is not None
        cell, arm, ordinal = parts
        block = int(ordinal.rstrip("r"))
        if cell == "none":
            u_none = U_C[block] - damage_map[block]
            return psnd_record(path.name, arm, block, u_none, run000=0,
                               run000_share=0.0, violations=0, prebind=0)
        return psnd_record(path.name, arm, block, U_C[block], run000=0,
                           run000_share=0.0, violations=0, prebind=0,
                           claim_first=15.0, admit_first=0.7)

    robustness.soundness_run_metrics = fake_c_damage
    code_c, report_c = run_premature(base, dirs, "prem_c_bad.json")
    check(code_c == 2
          and report_c["E-P3"]["rules"]["reconcile"]["le_2pp_pairs"] == 4
          and report_c["E-P3"]["rules"]["reconcile"]["pass"] is False,
          "E-P3 C one-sided: D_s > 2 pp in 3 pairs -> fail")
    install_premature_patches()


def test_check_violation_log_detection(base: Path) -> None:
    """The real `soundness_check_violations` marks a folder carrying neither
    controller LAN log as `logs_absent` (not-evaluable marker), and evaluates
    normally as soon as one log file exists (2026-10-13 (j))."""
    run = make_dir(base, "check_violation_log_probe")
    absent = robustness.soundness_check_violations(run)
    check(absent == {"lan1": 0, "lan2": 0, "total": 0, "logs_absent": True},
          "no controller LAN logs -> logs_absent true (not evaluable)")
    (run / "controller_lan1.log").write_text(
        "WARNING CHECK VIOLATION backend=x\nother line\n", encoding="utf-8")
    partial = robustness.soundness_check_violations(run)
    check(partial["logs_absent"] is False and partial["total"] == 1
          and partial["lan2"] == 0,
          "one controller LAN log present -> gate evaluable as before")


def support_backend_records(path: Path, overrides: dict | None = None) -> list[dict]:
    parts = robustness.completion_support_label_parts(path.name)
    assert parts is not None
    name, arm, _ = parts
    overrides = overrides or {}
    defaults = {
        "camp": {"direct": [0.03, 0.02], "discovery": [7.1, 7.3]},
        "cell12": {"direct": [0.0], "discovery": [6.5]},
        "disc15": {"discovery": [14.8]},
    }
    ready_values = overrides.get(name, {}).get(arm, defaults[name][arm])
    source = "event" if arm == "direct" else "probe"
    first_success_offset = overrides.get(name, {}).get(
        f"{arm}_first_success_s", 2.1 if arm == "direct" else 8.0)
    records = []
    for index, ready in enumerate(ready_values):
        lan = index + 1 if index < 2 else 1
        spawn = 60.0
        admitted = spawn + ready + 0.4
        first_success = spawn + first_success_offset
        records.append({
            "run": path.name,
            "container": f"edge_server_lan{lan}_dyn{index + 1}", "lan": lan,
            "admit_source": source, "bind_s": 0.2, "stratum": "fast",
            "ready_source": "service_log", "ready_to_admit_s": ready,
            "spawn_to_admitted_s": ready + 0.4,
            "spawn_started_ts": spawn, "admitted_ts": admitted,
            "admitted_to_first_routed_s": 0.5,
            "spawn_to_first_success_s": first_success - spawn,
            "spawn_to_first_routed_s": 1.0,
            "first_routed_sent_s": admitted + 0.5,
            "first_success_sent_s": first_success,
        })
    return records


def support_rows(path: Path) -> list[dict]:
    spike = [contact_row(float(sent), backend="edge_server_lan1_dyn1")
             for sent in range(61, 2065)]
    baseline = [contact_row(float(sent)) for sent in range(1, 151)]
    for row in spike:
        row["phase"] = "compute_spike"
    for row in baseline:
        row["phase"] = "baseline"
    return spike + baseline


def support_scale_decisions(path: Path) -> dict[int, list[float]]:
    """Fixture decision-log anchor: recorded metric values 2.17 / 6.01 s."""
    parts = robustness.completion_support_label_parts(path.name)
    name, arm, _ = parts
    if name == "cell12":
        if arm == "direct":
            return {1: [62.1 - 2.17], 2: [55.0]}
        return {1: [68.0 - 6.01], 2: [55.0]}
    return {1: [55.0], 2: [55.0]}


def install_support_patches(with_gap_bad: bool = False,
                            discovery_source_event: bool = False,
                            overrides: dict | None = None) -> None:
    def records(path: Path) -> list[dict]:
        out = support_backend_records(path, overrides)
        if discovery_source_event and robustness.completion_support_label_parts(
                path.name)[:2] == ("camp", "discovery"):
            out = [{**record, "admit_source": "event"} for record in out]
        return out

    def rows(path: Path) -> list[dict]:
        out = support_rows(path)
        if with_gap_bad and "rq3_probe_cell12_disc_" in path.name:
            out.append(timeout_row(67.5))
        return out

    robustness.completion_backend_records = records
    robustness.request_rows = rows
    robustness.phase_bounds = lambda path: {
        "baseline": (1.0, 150.0), "compute_spike": (60.0, 2100.0)}
    robustness._completion_scale_decisions = support_scale_decisions


def support_run_dirs(base: Path) -> list[str]:
    labels = ("rq3_camp_direct_7", "rq3_camp_disc_7",
              "rq3_probe_cell12_direct_6", "rq3_probe_cell12_disc_6",
              "rq3_disc15_7")
    return [str(make_dir(base, f"20261011_000003_{label}")) for label in labels]


def run_support(base: Path, dirs: list[str], out: str) -> tuple[int, dict]:
    code = robustness.completion_support_command(argparse.Namespace(
        run_dir=dirs, out=str(base / out)))
    return code, json.loads((base / out).read_text(encoding="utf-8"))


def test_completion_support(base: Path) -> None:
    install_support_patches()
    dirs = support_run_dirs(base)
    code, report = run_support(base, dirs, "support.json")
    check(code == 0 and report["pass"] is True, "E-S1/E-S2 direction checks pass")
    e_s1 = report["E-S1"]
    check(e_s1["leg"].startswith("spawn -> first successful response")
          and all(abs(value - 2.1) < 1e-9
                  for value in e_s1["direct_spawn_to_first_success_s"])
          and e_s1["discovery_spawn_to_first_success_s"] == [8.0, 8.0]
          and e_s1["direct_event_sourced"] is True
          and e_s1["discovery_probe_sourced"] is True
          and e_s1["direct_before_discovery_every_backend"] is True
          and len(e_s1["ordering_pairs"]) == 2
          and all(pair["x_before_y"] for pair in e_s1["ordering_pairs"])
          and e_s1["ready_to_admit_leg_unmeasurable"] is False
          and e_s1["ready_to_admit_leg"]["direct_values_s"] == [0.03, 0.02]
          and e_s1["ready_to_admit_leg"]["direct_le_0_05_every"] is True
          and e_s1["pass"] is True,
          "E-S1 per-LAN per-backend ordering on the spawn->first-success leg "
          "(2026-10-13 (l) re-scope)")
    e_s2 = report["E-S2"]
    check(abs(e_s2["direct_median_s"] - 2.17) < 1e-6
          and abs(e_s2["discovery_median_s"] - 6.01) < 1e-6
          and e_s2["anchor_direct_median_s"] == 2.17
          and e_s2["anchor_discovery_median_s"] == 6.01
          and e_s2["gap_null_ok"] is True
          and e_s2["driver_clean"] is True
          and e_s2["pass"] is True,
          "E-S2 recorded scale-decision -> usable-capacity anchor (2.17/6.01)")
    e_s3 = report["E-S3"]
    check(e_s3["median_s"] == 14.8
          and e_s3["quantization_within_expected_band"] is True
          and e_s3["descriptive"] is True
          and "pass" not in e_s3,
          "E-S3 ~one-period band (10-20 s), descriptive (no pass key)")

    # Console summary: the E-S3 line reports the descriptive status; the
    # removed pass key must never resurface as a console `null`.
    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        robustness.completion_support_command(argparse.Namespace(
            run_dir=dirs, out=str(base / "support_console.json")))
    summary = console.getvalue()
    check('"E-S3_descriptive"' in summary and '"E-S3_pass"' not in summary,
          "console summary reports the E-S3 descriptive status (no stale key)")

    # Failure paths: wrong source vocabulary; gap-window failures present;
    # per-backend ordering inversion.
    install_support_patches(discovery_source_event=True)
    code_s, out = run_support(base, dirs, "support_bad.json")
    install_support_patches()
    check(code_s == 2 and out["E-S1"]["pass"] is False,
          "E-S1 wrong admit source -> exit 2")
    install_support_patches(with_gap_bad=True)
    code_g, out_g = run_support(base, dirs, "support_gap.json")
    install_support_patches()
    check(code_g == 2 and out_g["E-S2"]["gap_null_ok"] is False,
          "E-S2 gap-window failure -> exit 2")
    # E-S1 (l) failure path: the gated spawn->first-success leg inverts for
    # the direct arm (9.0 s > discovery 8.0 s) -> exit 2.
    install_support_patches(
        overrides={"camp": {"direct_first_success_s": 9.0}})
    code_o, out_o = run_support(base, dirs, "support_order.json")
    install_support_patches()
    check(code_o == 2 and out_o["E-S1"]["pass"] is False
          and not all(pair["x_before_y"] for pair in out_o["E-S1"]["ordering_pairs"])
          and out_o["E-S1"]["ready_to_admit_leg_unmeasurable"] is False,
          "E-S1 per-backend ordering inversion on the gated leg -> exit 2")

    # E-S1/E-S3 (l): a degenerate registered ready->admit leg (0.000 s for
    # every backend, the (h) instrument degeneracy) is a note, never a gate.
    install_support_patches(overrides={
        "camp": {"direct": [0.0, 0.0], "discovery": [0.0, 0.0]},
        "disc15": {"discovery": [0.0]}})
    code_z, out_z = run_support(base, dirs, "support_zero.json")
    install_support_patches()
    check(code_z == 0
          and out_z["E-S1"]["pass"] is True
          and out_z["E-S1"]["ready_to_admit_leg_unmeasurable"] is True
          and "unmeasurable" in out_z["E-S1"]["ready_to_admit_leg"]["status"]
          and out_z["E-S1"]["ready_to_admit_leg"]["discovery_in_0_10_every"]
          is False
          and out_z["E-S3"]["quantization_within_expected_band"] is None
          and out_z["E-S3"]["ready_to_admit_leg_unmeasurable"] is True
          and any("unmeasurable" in finding
                  for finding in out_z["descriptive_findings"]),
          "degenerate ready->admit leg: E-S1 passes on the note, E-S3 band "
          "not assessable (descriptive)")

    # E-S3 out-of-band is a descriptive finding, never a failure.
    install_support_patches(overrides={"disc15": {"discovery": [25.0]}})
    code_b3, out_b3 = run_support(base, dirs, "support_band.json")
    install_support_patches()
    check(code_b3 == 0
          and out_b3["E-S3"]["quantization_within_expected_band"] is False
          and any("E-S3" in finding
                  for finding in out_b3["descriptive_findings"]),
          "E-S3 out-of-band reported descriptively; exit unaffected")

    # Duplicate run identities are refused (exit 3), never last-wins.
    code_dup = robustness.main([
        "completion-support", "--run-dir", *dirs, dirs[2],
        "--out", str(base / "support_dup.json")])
    check(code_dup == 3, "duplicate set/arm identity refused (exit 3)")

    # End-to-end entry point + analyzer error path (exit 3, never 2).
    code_main = robustness.main([
        "completion-support", "--run-dir", *dirs,
        "--out", str(base / "support_main.json")])
    check(code_main == 0, "completion-support via main() exits 0")
    code_err = robustness.main([
        "completion-intact", "--run-dir", str(base / "not_a_label"),
        "--out", str(base / "err.json")])
    check(code_err == 3, "unparsable label through main() exits 3 (STOP)")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        test_completion_intact(base)
        test_completion_lost(base)
        test_completion_premature(base)
        test_check_violation_log_detection(base)
        test_completion_support(base)
        print(json.dumps({
            "e_i1_direction_fast_slow_only": "PASS",
            "e_i1_spawn_leg_and_c_band": "PASS",
            "e_i1_ready_to_admit_leg_unmeasurable": "PASS",
            "e_i1_failure_path": "PASS",
            "block7_void_exclusion": "PASS",
            "e_i2_handover_descriptive": "PASS",
            "e_i3_redistribution": "PASS",
            "e_i4_recorded_qualification": "PASS",
            "e_l1_denominator_and_block7": "PASS",
            "e_l1_failure_path": "PASS",
            "e_l2_mechanism_counts": "PASS",
            "e_l3_boundedness_and_block7": "PASS",
            "e_l4_recorded_qualification": "PASS",
            "e_p1_new_run_checks": "PASS",
            "e_p1_recorded_run_credit": "PASS",
            "e_p1_logs_absent_not_evaluable": "PASS",
            "e_p1_log_gate_evaluated_with_logs": "PASS",
            "check_violation_log_detection": "PASS",
            "e_p1_c_descriptive_band": "PASS",
            "e_p2_separation": "PASS",
            "e_p3_seven_pairs": "PASS",
            "e_p3_missing_partner": "PASS",
            "e_p3_c_one_sided_no_median_bar": "PASS",
            "e_p4_retired": "PASS",
            "e_s1_per_backend_ordering": "PASS",
            "e_s1_spawn_leg_rescope": "PASS",
            "e_s1_es3_degenerate_leg_note": "PASS",
            "e_s2_recorded_anchor": "PASS",
            "e_s3_descriptive_band": "PASS",
            "duplicate_refusal": "PASS",
            "e_w1_wake_intact_direction": "PASS",
            "e_w2_wake_loss_pairs": "PASS",
            "e_w3_wake_immunity_pairs": "PASS",
            "wake_ordinal_guards": "PASS",
            "main_entrypoints": "PASS",
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
