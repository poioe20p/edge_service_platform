"""cli_release_compare — research_q3 release-mechanism cross-run comparison.

Compares release arms (``off`` / ``immediate`` / ``drained`` / ``stabilized`` /
``stabilized-calibration``) across research_q3 run folders produced by
``research_q3_launch_run.sh``. Each run folder carries:

  - release_log_lan{1,2}.csv   — release events (begin / end /
                                  quarantine_begin / recall)
  - client_requests.csv        — request-level timeouts / errors
  - container_events.csv       — docker add/remove timeline
  - decision_log_lan{1,2}.csv  — scale-up decisions
  - resource_stats.csv / per_node_stats.csv — phase-keyed resource samples
  - rs_evict_logs_lan{1,2}/*.log — per-container eviction outcomes (JSON lines)
  - rs_status_*.json           — rs.status() snapshots (ghost-member count)
  - phases_snapshot.json       — workload phases (names + durations)

Metrics:

  C1 release speed  — matched begin→end pairs (join key = (network_id,
                      container)); latency = end.ts − begin.ts, reported as
                      p50/p90 per tier; ``orphan_begin`` counts begins with no
                      matching end. For ``stabilized``, quarantine_begin→recall
                      latency is reported too, and quarantine_begin is treated
                      as the begin event for compute releases so
                      quarantine_begin→end (finalization) latency is reported
                      separately (``C1_stabilized_qf_p50``).
  C2 release safety — timeout/error client rows whose completion timestamp
                      falls within ``[begin−30, end+90]`` of a release event
                      of that tier (orphan begins use ``[begin−30, begin+180]``;
                      end-only rows — e.g. the ``immediate`` arm — use
                      ``[end−30, end+90]``). NotPrimary / NotPrimaryOrSecondary
                      mentions in request fields inside those windows are
                      counted separately. Eviction outcomes from
                      ``rs_evict_logs_lan{1,2}`` (ok vs non-ok, plus
                      fire-and-forget overlaps) and ghost members from
                      the newest ``rs_status_*.json``.
  C3 hold cost      — dynamic-container-seconds during the ``hold`` and
                      ``demand_drop`` phases (sum of overlap between dynamic
                      container lifetimes and the phase windows). Falls back
                      to per_node_stats phase rows when container_events has
                      no added/removed events; emits ``n/a`` otherwise.
  C4 re-entry       — time from ``return_storm`` phase onset to the first
                      scale-up decision (decision_log) and compute spawns
                      during ``return_storm`` (container_events). For
                      ``stabilized``, ``recall_missed`` flags a quarantine
                      begun during ``hold`` that produced no recall row and
                      demonstrably ran to a bad end (finalized without a
                      recall, or — compute only — a respawn with no recalled
                      quarantine at all). Per tier (compute / storage),
                      never pooled; success=0 audit recall rows are ignored.
                      Only computed for early-return cells (``hold`` ≤ 600 s,
                      recall expected at return); late-return cells
                      pre-register finalize-before-return and report n/a.
  C5 stability      — release-end → spawn pairs within 60 s (one-to-one
                      greedy match).

Usage:
    python -m source.scripts.testing.analysis.cli_release_compare \
        --run-dir <dir1> [--run-dir <dir2> ...] [--output-dir <dir>]
        [--arm <label>]

``--arm`` is a fallback label applied to runs whose arm cannot be inferred
from the folder label or the release_log ``mechanism`` column. Duplicate runs
of the same arm are kept as separate rows; per-arm aggregation is the
caller's job (the PNG groups per arm by averaging).

Outputs (under ``--output-dir``, default ``release_compare/`` in cwd):
  release_compare_table.csv   — one row per run
  release_safety_report.csv   — every attributed error row
  release_compare.png         — grouped bars per arm (needs matplotlib)

Hidden flag ``--output-csv-only`` skips the PNG so headless hosts without
matplotlib can still produce the CSVs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_NA = "n/a"
_RELEASE_LOG_FILES = ("release_log_lan1.csv", "release_log_lan2.csv")
_DECISION_LOG_FILES = ("decision_log_lan1.csv", "decision_log_lan2.csv")

# Release windows (seconds) around release events — see module docstring.
_WIN_PRE_S = 30.0
_WIN_POST_S = 90.0
_ORPHAN_POST_S = 180.0
_CHURN_WINDOW_S = 60.0

_TABLE_FIELDS = [
    "run_label", "arm", "mechanism",
    "C1_compute_p50", "C1_storage_p50", "C1_stabilized_recall_p50",
    "C1_stabilized_recall_storage_p50",
    "C1_stabilized_qf_p50", "C1_stabilized_qf_storage_p50",
    "orphan_begin",
    "C2_attributed_errors", "C2_notprimary",
    "C2_evict_ok", "C2_evict_nonok", "C2_evict_overlap", "C2_ghosts",
    "C3_hold_cont_s", "C3_drop_cont_s",
    "C4_time_to_scaleup_s", "C4_return_spawns", "C4_recall_missed",
    "C4_recall_missed_storage",
    "C5_churn_pairs",
]

_SAFETY_FIELDS = [
    "run_label", "arm", "tier", "release_container",
    "release_begin_ts", "release_end_ts",
    "error_ts", "error_kind", "in_window", "row_key",
]

_ARM_ORDER = ["off", "immediate", "drained", "stabilized", "stabilized-calibration"]


# ---------------------------------------------------------------------------
# Small stdlib helpers (kept local so non-plot paths never depend on the
# analysis package, numpy or matplotlib).
# ---------------------------------------------------------------------------

def _warn(msg: str) -> None:
    print(f"[cli_release_compare] {msg}", file=sys.stderr)


def _safe_float(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value) -> float | None:
    """Normalize a timestamp (unix epoch or ISO-8601 string) to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if ts > 0 else None
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "nan", "none", ""):
        return None
    try:
        ts = float(text)
        return ts if ts > 0 else None
    except ValueError:
        pass
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _fmt(value, nd: int = 2) -> str:
    if value is None:
        return _NA
    return f"{value:.{nd}f}"


def _fmt_int(value) -> str:
    if value is None:
        return _NA
    return str(int(value))


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f))
    except OSError as exc:
        _warn(f"{path}: could not read ({exc})")
        return []


def _tier_of_container(name: str, image: str = "") -> str:
    n = (name or "").lower()
    i = (image or "").lower()
    if n.startswith("edge_storage") or "storage" in i:
        return "storage"
    if n.startswith("edge_server") or n.startswith("edge_compute") or "server" in i:
        return "compute"
    return ""


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def _load_release_rows(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for name in _RELEASE_LOG_FILES:
        rows.extend(_read_csv(run_dir / name))
    return rows


def _load_client_rows(run_dir: Path) -> list[dict]:
    rows = _read_csv(run_dir / "client_requests.csv")
    for idx, row in enumerate(rows):
        row["_row_key"] = idx + 1  # 1-based row id for the safety report
    return rows


def _load_container_rows(run_dir: Path) -> list[dict]:
    return _read_csv(run_dir / "container_events.csv")


def _load_decision_rows(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for name in _DECISION_LOG_FILES:
        for row in _read_csv(run_dir / name):
            row["_log"] = name
            rows.append(row)
    return rows


def _load_phases(run_dir: Path) -> tuple[list[dict], float | None]:
    """Load phases_snapshot.json phases; return (phases, run_origin).

    ``run_origin`` is an absolute start timestamp taken from the snapshot
    when present, else None (callers derive offsets from request timestamps).
    """
    path = run_dir / "phases_snapshot.json"
    if not path.exists():
        _warn(f"{run_dir.name}: phases_snapshot.json missing")
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _warn(f"{run_dir.name}: phases_snapshot.json unreadable ({exc})")
        return [], None
    run_origin: float | None = None
    if isinstance(data, dict):
        run_origin = _parse_ts(data.get("start_ts")
                               or data.get("started_at")
                               or data.get("start_time"))
        phases = data.get("phases", [])
    else:
        phases = data
    return list(phases), run_origin


def _phase_windows(phases: list[dict], origin: float | None,
                   run_origin: float | None) -> dict[str, tuple[float, float]]:
    """Absolute (start, end) per phase name.

    Prefers per-phase absolute timestamps, then the snapshot's run-level
    origin, then cumulative durations anchored on *origin* (the run's first
    request ts). Returns {} when no anchor is available.
    """
    windows: dict[str, tuple[float, float]] = {}
    prev_end: float | None = None
    offset = 0.0
    for ph in phases:
        name = str(ph.get("name", ""))
        dur = _safe_float(ph.get("duration_s"), 0.0) or 0.0
        abs_ts = _parse_ts(ph.get("start_ts")
                           or ph.get("started_at")
                           or ph.get("start_time"))
        if abs_ts is not None:
            start = abs_ts
        elif prev_end is not None:
            start = prev_end
        elif run_origin is not None:
            start = run_origin + offset
        elif origin is not None:
            start = origin + offset
        else:
            break  # no anchor — remaining phases unmappable
        windows[name] = (start, start + dur)
        prev_end = start + dur
        offset += dur
    return windows


def _derive_origin(client_rows: list[dict], container_rows: list[dict]) -> float | None:
    candidates: list[float] = []
    for row in client_rows:
        ts = _parse_ts(row.get("sent_at") or row.get("timestamp"))
        if ts is not None:
            candidates.append(ts)
    if candidates:
        return min(candidates)
    for row in container_rows:
        ts = _parse_ts(row.get("timestamp_iso") or row.get("monotonic_s"))
        if ts is not None:
            candidates.append(ts)
    return min(candidates) if candidates else None


def _infer_arm_from_label(label: str) -> str | None:
    low = label.lower()
    if "research_q3_cal" in low or "rq3rel_cal" in low:
        return "stabilized-calibration"
    for prefix, arm in (("research_q3_o", "off"), ("rq3rel_o", "off"),
                        ("research_q3_i", "immediate"), ("rq3rel_i", "immediate"),
                        ("research_q3_d", "drained"), ("rq3rel_d", "drained"),
                        ("research_q3_s", "stabilized"), ("rq3rel_s", "stabilized")):
        if prefix in low:
            return arm
    return None


def _mechanism_from_rows(release_rows: list[dict]) -> str | None:
    for row in release_rows:
        mech = (row.get("mechanism") or "").strip()
        if mech:
            return mech
    return None


# ---------------------------------------------------------------------------
# Release-event pairing (shared by C1 / C2 / C4)
# ---------------------------------------------------------------------------

def _release_events(release_rows: list[dict]) -> dict:
    """Group release rows into pairs, orphan begins, end-only and quarantine.

    Join key is (network_id, container); begin/end lists are paired FIFO.
    """
    begins: dict[tuple, list[float]] = defaultdict(list)
    ends: dict[tuple, list[float]] = defaultdict(list)
    qbegins: dict[tuple, list[float]] = defaultdict(list)
    recalls: dict[tuple, list[float]] = defaultdict(list)
    audit_recalls: dict[tuple, list[float]] = defaultdict(list)
    tier_by_key: dict[tuple, str] = {}
    end_reason: dict[tuple, str] = {}

    for row in release_rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            continue
        key = ((row.get("network_id") or "").strip(),
               (row.get("container") or "").strip())
        if not key[1]:
            continue
        tier = (row.get("tier") or "").strip() or _tier_of_container(key[1])
        if tier and not tier_by_key.get(key):
            tier_by_key[key] = tier
        event = (row.get("event") or "").strip()
        if event == "begin":
            begins[key].append(ts)
        elif event == "end":
            ends[key].append(ts)
            end_reason[(key, ts)] = (row.get("reason") or "").strip()
        elif event == "quarantine_begin":
            qbegins[key].append(ts)
        elif event == "recall":
            # success=0 recall rows are audit rows (aborted retention via
            # absent-cleanup, or an unknown-state recall) — they are NOT
            # genuine re-admissions. They never feed latency/miss metrics;
            # they only truncate the quarantine's C2 attribution window.
            if (row.get("success") or "").strip() == "0":
                audit_recalls[key].append(ts)
            else:
                recalls[key].append(ts)

    pairs: list[dict] = []
    orphan_begins: list[dict] = []
    end_only: list[dict] = []
    used_ends: dict[tuple, set[int]] = defaultdict(set)
    for key, blist in begins.items():
        elist = ends.get(key, [])
        tier = tier_by_key.get(key, "")
        n = min(len(blist), len(elist))
        for i in range(n):
            delta = elist[i] - blist[i]
            if delta < 0:
                orphan_begins.append({"tier": tier, "container": key[1],
                                      "network_id": key[0], "begin": blist[i]})
                continue
            used_ends[key].add(i)
            pairs.append({"tier": tier, "container": key[1],
                          "network_id": key[0],
                          "begin": blist[i], "end": elist[i]})
        orphan_begins.extend(
            {"tier": tier, "container": key[1], "network_id": key[0],
             "begin": b}
            for b in blist[n:])
    # Ends not consumed by a begin — e.g. every row of the `immediate` arm,
    # which writes end rows only.
    for key, elist in ends.items():
        tier = tier_by_key.get(key, "")
        end_only.extend(
            {"tier": tier, "container": key[1], "network_id": key[0],
             "end": e}
            for i, e in enumerate(elist) if i not in used_ends.get(key, ()))

    quarantine: list[dict] = []
    for key, qlist in qbegins.items():
        rlist = sorted(recalls.get(key, []))
        alist = sorted(audit_recalls.get(key, []))
        tier = tier_by_key.get(key, "")
        # Chronological greedy pairing. Producer contract (main_n1/main_n2):
        # EVERY recall row — success 0 or 1 — is written only while a
        # retention is ACTIVE, and each one terminates it (gate ACTIVE→IDLE).
        # Each quarantine_begin therefore has at most one terminal event
        # (a genuine recall or an audit abort); the next retention requires
        # a fresh quarantine_begin. The greedy rule binds each qbegin to the
        # earliest of {next genuine recall, next audit abort} at-or-after
        # itself; audit aborts truncate the C2 window and keep later recalls
        # from binding to a lifecycle an abort already terminated.
        ri = ai = 0
        for qb in sorted(qlist):
            while ri < len(rlist) and rlist[ri] < qb:
                ri += 1
            while ai < len(alist) and alist[ai] < qb:
                ai += 1
            r = rlist[ri] if ri < len(rlist) else None
            a = alist[ai] if ai < len(alist) else None
            if r is not None and (a is None or r <= a):
                quarantine.append({"tier": tier, "container": key[1],
                                   "network_id": key[0],
                                   "qbegin": qb, "recall": r,
                                   "abort": None})
                ri += 1
            elif a is not None:
                # Abort ends this retention — the audit row truncates the
                # C2 attribution window and blocks any later recall from
                # binding to this qbegin.
                quarantine.append({"tier": tier, "container": key[1],
                                   "network_id": key[0],
                                   "qbegin": qb, "recall": None,
                                   "abort": a})
                ai += 1
            else:
                quarantine.append({"tier": tier, "container": key[1],
                                   "network_id": key[0],
                                   "qbegin": qb, "recall": None,
                                   "abort": None})

    # Stabilized compute finalization: pair each end with the most recent
    # quarantine_begin that is NOT followed by a recall before that end
    # (a recalled quarantine never finalizes).
    qf_pairs: list[dict] = []
    for key, qlist in qbegins.items():
        elist = ends.get(key, [])
        rlist = recalls.get(key, [])
        tier = tier_by_key.get(key, "")
        for e in elist:
            best = None
            for qi, qb in enumerate(qlist):
                if qb > e:
                    break
                if any(qb <= r <= e for r in rlist):
                    continue
                best = qi
            if best is not None:
                qf_pairs.append({"tier": tier, "container": key[1],
                                 "network_id": key[0],
                                 "begin": qlist[best], "end": e})

    return {"pairs": pairs, "orphan_begins": orphan_begins,
            "end_only": end_only, "quarantine": quarantine,
            "qf_pairs": qf_pairs,
            "recall_total": sum(len(v) for v in recalls.values()),
            "ends": [{"tier": tier_by_key.get(k, ""), "container": k[1],
                      "network_id": k[0], "end": t,
                      "reason": end_reason.get((k, t), "")}
                     for k, ts_list in ends.items() for t in ts_list]}


def _release_windows(events: dict) -> list[dict]:
    """Timed windows around release events, tagged with tier and container."""
    windows: list[dict] = []
    for p in events["pairs"]:
        windows.append({"tier": p["tier"], "container": p["container"],
                        "network_id": p["network_id"],
                        "begin": p["begin"], "end": p["end"],
                        "win_start": p["begin"] - _WIN_PRE_S,
                        "win_end": p["end"] + _WIN_POST_S})
    for b in events["orphan_begins"]:
        windows.append({"tier": b["tier"], "container": b["container"],
                        "network_id": b["network_id"],
                        "begin": b["begin"], "end": b["begin"],
                        "win_start": b["begin"] - _WIN_PRE_S,
                        "win_end": b["begin"] + _ORPHAN_POST_S})
    for e in events["end_only"]:
        # `immediate` arm only writes end rows — attribute errors around the
        # teardown moment. in_window stays 0 (no begin/end operation span).
        windows.append({"tier": e["tier"], "container": e["container"],
                        "network_id": e["network_id"],
                        "begin": e["end"], "end": e["end"],
                        "win_start": e["end"] - _WIN_PRE_S,
                        "win_end": e["end"] + _WIN_POST_S})
    # Stabilized quarantine/retention windows: quarantine_begin → recall (or
    # an audit abort, or an orphan-style post-window when never recalled).
    # Covers storage retention→recall transitions, which produce no
    # elasticity begin/end pair — errors during retention/recall must still
    # be attributable, but not beyond an abort.
    for q in events["quarantine"]:
        if q["recall"] is not None:
            end = q["recall"]
        elif q.get("abort") is not None:
            end = q["abort"]
        else:
            end = q["qbegin"] + _ORPHAN_POST_S
        windows.append({"tier": q["tier"], "container": q["container"],
                        "network_id": q["network_id"],
                        "begin": q["qbegin"], "end": end,
                        "win_start": q["qbegin"] - _WIN_PRE_S,
                        "win_end": end + _WIN_POST_S})
    return windows


# ---------------------------------------------------------------------------
# Metric computations
# ---------------------------------------------------------------------------

def _c1_release_speed(events: dict) -> dict:
    lat: dict[str, list[float]] = {"compute": [], "storage": []}
    pair_count = Counter()
    orphan_begin = len(events["orphan_begins"])
    for p in events["pairs"]:
        tier = p["tier"]
        if tier in lat:
            lat[tier].append(p["end"] - p["begin"])
            pair_count[tier] += 1
    # Stabilized finalizations already carry regular begin→end rows
    # (elasticity writes them for drained/stabilized), so C1_compute/C1_storage
    # are comparable across arms from ``pairs`` alone. The
    # quarantine→finalization span is reported separately per tier
    # (C1_stabilized_qf_p50 for compute, C1_stabilized_qf_storage_p50 for
    # storage retention→finalize spans).
    qf_lat: dict[str, list[float]] = {"compute": [], "storage": []}
    for p in events["qf_pairs"]:
        if p["tier"] in qf_lat:
            qf_lat[p["tier"]].append(p["end"] - p["begin"])
    recall_lat: dict[str, list[float]] = {"compute": [], "storage": []}
    for q in events["quarantine"]:
        if q["recall"] is not None and q["tier"] in recall_lat:
            recall_lat[q["tier"]].append(q["recall"] - q["qbegin"])
    return {
        "compute_p50": _percentile(lat["compute"], 0.50),
        "compute_p90": _percentile(lat["compute"], 0.90),
        "storage_p50": _percentile(lat["storage"], 0.50),
        "storage_p90": _percentile(lat["storage"], 0.90),
        "recall_p50": _percentile(recall_lat["compute"], 0.50),
        "recall_storage_p50": _percentile(recall_lat["storage"], 0.50),
        "qf_p50": _percentile(qf_lat["compute"], 0.50),
        "qf_storage_p50": _percentile(qf_lat["storage"], 0.50),
        "pair_count": dict(pair_count),
        "orphan_begin": orphan_begin,
    }


def _client_row_info(row: dict) -> dict:
    """Completion ts + error-kind flags for one client_requests row."""
    err_ts = (_parse_ts(row.get("completed_at"))
              or _parse_ts(row.get("timestamp")))
    if err_ts is None:
        sent = _parse_ts(row.get("sent_at"))
        lat = _safe_float(row.get("latency_s"))
        if sent is not None and lat is not None:
            err_ts = sent + lat
        else:
            err_ts = sent
    status = (row.get("status") or "").strip().lower()
    http = (row.get("http_status") or "").strip()
    is_timeout = status == "timeout" or (not status and http in ("000", "0"))
    is_error = False
    if status in ("completed", ""):
        is_error = http not in ("", "200", "201", "204")
    is_notprimary = any(
        ("notprimary" in v.lower()) or ("notprimaryorsecondary" in v.lower())
        for v in row.values() if isinstance(v, str))
    return {"err_ts": err_ts, "timeout": is_timeout,
            "error": is_error, "notprimary": is_notprimary}


def _c2_safety(run_dir: Path, windows: list[dict],
               client_rows: list[dict]) -> tuple[dict, list[dict]]:
    safety_rows: list[dict] = []
    attributed: set[int] = set()
    notprimary_rows: set[int] = set()

    def _row_id(row: dict) -> int:
        try:
            return int(row.get("_row_key", 0))
        except (TypeError, ValueError):
            return 0

    for row in client_rows:
        info = _client_row_info(row)
        err_ts = info["err_ts"]
        if err_ts is None:
            continue
        has_timeout_or_error = info["timeout"] or info["error"]
        if not has_timeout_or_error and not info["notprimary"]:
            continue
        for win in windows:
            if not (win["win_start"] <= err_ts <= win["win_end"]):
                continue
            if info["timeout"]:
                primary_kind = "timeout"
            elif info["error"]:
                primary_kind = "error"
            else:
                primary_kind = ""
            if primary_kind:
                safety_rows.append({
                    "tier": win["tier"], "release_container": win["container"],
                    "release_begin_ts": win["begin"], "release_end_ts": win["end"],
                    "error_ts": err_ts, "error_kind": primary_kind,
                    "in_window": int(win["begin"] <= err_ts <= win["end"]),
                    "row_key": row.get("_row_key", ""),
                })
                attributed.add(_row_id(row))
            if info["notprimary"]:
                safety_rows.append({
                    "tier": win["tier"], "release_container": win["container"],
                    "release_begin_ts": win["begin"], "release_end_ts": win["end"],
                    "error_ts": err_ts, "error_kind": "notprimary",
                    "in_window": int(win["begin"] <= err_ts <= win["end"]),
                    "row_key": row.get("_row_key", ""),
                })
                notprimary_rows.add(_row_id(row))

    evict = _load_evictions(run_dir)
    ghosts = _count_ghosts(run_dir)
    return {
        "attributed_errors": len(attributed) if client_rows else None,
        "notprimary": len(notprimary_rows) if client_rows else None,
        "evict_ok": evict["ok"],
        "evict_nonok": evict["nonok"],
        "evict_overlap": evict["overlap"],
        "ghosts": ghosts,
    }, safety_rows


def _load_evictions(run_dir: Path) -> dict:
    """Count eviction outcomes from rs_evict_logs_lan{1,2}/*.log (JSON lines).

    Legacy ``rs_evict_logs`` is still supported. fire-and-forget logs
    interleave raw mongosh stdout after the JSON line, so the real outcome is
    determined by scanning the REST of the same file for ``"ok":1`` /
    ``"ok": 1``; the JSON line's ``eviction_overlap`` flag feeds the overlap
    count. Non-JSON lines are skipped.
    """
    counts: Counter = Counter()
    overlap = 0
    found = False
    for dir_name in ("rs_evict_logs_lan1", "rs_evict_logs_lan2",
                     "rs_evict_logs"):
        evict_dir = run_dir / dir_name
        if not evict_dir.is_dir():
            continue
        for path in sorted(evict_dir.glob("*.log")):
            found = True
            try:
                lines = path.read_text(encoding="utf-8",
                                       errors="replace").splitlines()
            except OSError:
                continue
            for idx, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(doc, dict):
                    continue
                outcome = str(doc.get("outcome", "")).strip()
                if not outcome:
                    continue
                if outcome == "fire_and_forget":
                    tail = "\n".join(lines[idx + 1:])
                    if '"ok":1' in tail or '"ok": 1' in tail:
                        counts["ok"] += 1
                    else:
                        counts["nonok"] += 1
                    if doc.get("eviction_overlap"):
                        overlap += 1
                else:
                    counts[outcome] += 1
    if not found:
        return {"ok": None, "nonok": None, "overlap": None}
    nonok = (counts.get("rs_member_lingers", 0)
             + counts.get("rs_remove_non_ok", 0)
             + counts.get("primary_not_found", 0)
             + counts.get("nonok", 0))
    return {"ok": counts.get("ok", 0), "nonok": nonok, "overlap": overlap}


def _count_ghosts(run_dir: Path) -> int | None:
    """Ghost member count from the newest rs_status_*.json.

    The probe writes ``{"lan1": [members...], "lan2": [members...]}``. A
    member is a ghost when its stateStr is not PRIMARY/SECONDARY — ARBITER
    counts as non-ghost — anything else (REMOVED, RECOVERING, ...) counts.
    A legacy flat ``{"members": [...]}`` shape is still accepted.
    """
    files = sorted(run_dir.glob("rs_status_*.json"))
    if not files:
        return None
    try:
        path = max(files, key=lambda p: p.stat().st_mtime)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _warn(f"{run_dir.name}: rs_status_*.json unreadable ({exc})")
        return None
    member_lists: list[list] = []
    if isinstance(data, dict):
        lan1 = data.get("lan1")
        lan2 = data.get("lan2")
        if isinstance(lan1, list) and isinstance(lan2, list):
            member_lists = [lan1, lan2]
        elif isinstance(data.get("members"), list):
            member_lists = [data["members"]]
        else:
            # Partial probe (one LAN null/unreadable) — ghosts are
            # unknowable, not zero.
            _warn(f"{run_dir.name}: rs_status incomplete (lan1/lan2 missing) — ghosts n/a")
            return None
    ghosts = 0
    for members in member_lists:
        for m in members:
            if not isinstance(m, dict):
                continue
            state = str(m.get("stateStr", ""))
            if state not in ("PRIMARY", "SECONDARY", "ARBITER"):
                ghosts += 1
    return ghosts


def _dynamic_intervals(container_rows: list[dict]) -> tuple[list[tuple], float | None]:
    """(container, start, end) lifetimes from added/removed events.

    Containers never observed as ``removed`` stay active until the last
    event timestamp of the run.
    """
    added: dict[str, list[float]] = defaultdict(list)
    removed: dict[str, list[float]] = defaultdict(list)
    max_ts: float | None = None
    for row in container_rows:
        ts = _parse_ts(row.get("timestamp_iso"))
        if ts is None:
            continue
        max_ts = ts if max_ts is None else max(max_ts, ts)
        name = (row.get("container") or "").strip()
        if not name:
            continue
        event = (row.get("event") or "").strip()
        if event == "added":
            added[name].append(ts)
        elif event == "removed":
            removed[name].append(ts)
    intervals = []
    for name, added_ts in added.items():
        start = min(added_ts)
        end = min((t for t in removed.get(name, []) if t >= start),
                  default=None)
        intervals.append((name, start, end))
    return intervals, max_ts


def _container_seconds(intervals: list[tuple], win_start: float,
                       win_end: float, max_ts: float | None) -> float:
    total = 0.0
    for _, start, end in intervals:
        stop = end if end is not None else (max_ts or win_end)
        overlap = min(stop, win_end) - max(start, win_start)
        if overlap > 0:
            total += overlap
    return total


def _c3_node_fallback(run_dir: Path,
                      hold_phases: list[str],
                      drop_phases: list[str]) -> tuple[float | None, float | None]:
    """Fallback: count dynamic nodes per per_node_stats sample × interval."""
    rows = _read_csv(run_dir / "per_node_stats.csv")
    if not rows or not (hold_phases or drop_phases):
        return None, None
    # Static nodes appear from the first telemetry window; dynamic nodes later.
    first_seen: dict[str, float] = {}
    for row in rows:
        server_id = (row.get("server_id") or "").strip()
        we = _safe_float(row.get("window_end"))
        role = (row.get("role") or "").strip()
        if not server_id or we is None or role not in ("compute", "storage"):
            continue
        first_seen[server_id] = min(first_seen.get(server_id, float("inf")), we)
    global_first = min(first_seen.values()) if first_seen else None

    def _sum_for(names: list[str]) -> float | None:
        phase_rows = [r for r in rows if str(r.get("phase", "")) in names]
        if not phase_rows:
            return None
        by_window: dict[float, set[str]] = defaultdict(set)
        for row in phase_rows:
            we = _safe_float(row.get("window_end"))
            server_id = (row.get("server_id") or "").strip()
            role = (row.get("role") or "").strip()
            if we is None or not server_id or role not in ("compute", "storage"):
                continue
            if global_first is not None and \
                    first_seen.get(server_id, global_first) > global_first + 0.5:
                by_window[we].add(server_id)
        if not by_window:
            return None
        ends = sorted(by_window)
        diffs = [b - a for a, b in zip(ends, ends[1:]) if 0 < b - a <= 60]
        interval = (sorted(diffs)[len(diffs) // 2]) if diffs else 5.0
        return sum(len(ids) * interval for ids in by_window.values())

    return _sum_for(hold_phases), _sum_for(drop_phases)


def _c3_hold_cost(run_dir: Path, phase_windows: dict,
                  container_rows: list[dict]) -> dict:
    hold_names = [n for n in phase_windows if "hold" in n.lower()]
    drop_names = [n for n in phase_windows if "demand_drop" in n.lower()]
    out = {"hold": None, "drop": None, "note": ""}
    if not phase_windows:
        out["note"] = "no phase timeline (missing phases_snapshot or no anchor ts)"
        return out
    if not hold_names and not drop_names:
        out["note"] = "no hold/demand_drop phases in phases_snapshot"
        return out

    intervals, max_ts = _dynamic_intervals(container_rows)
    if intervals:
        if hold_names:
            total = 0.0
            for n in hold_names:
                start, end = phase_windows[n]
                total += _container_seconds(intervals, start, end, max_ts)
            out["hold"] = total
        if drop_names:
            total = 0.0
            for n in drop_names:
                start, end = phase_windows[n]
                total += _container_seconds(intervals, start, end, max_ts)
            out["drop"] = total
        return out

    hold, drop = _c3_node_fallback(run_dir, hold_names, drop_names)
    out["hold"], out["drop"] = hold, drop
    if hold is None and drop is None:
        out["note"] = ("no dynamic-container data (container_events has no "
                       "added/removed events and per_node_stats has no "
                       "hold/demand_drop rows)")
    else:
        out["note"] = ("per_node_stats fallback (container_events has no "
                       "added/removed events)")
    return out


def _c4_reentry(run_dir: Path, phase_windows: dict, decision_rows: list[dict],
                container_rows: list[dict], arm: str,
                events: dict) -> dict:
    out = {"time_to_scaleup": None, "return_spawns": None,
           "recall_missed": None, "recall_missed_storage": None, "note": ""}
    storm_name = next((n for n in phase_windows if "return_storm" in n.lower()),
                      None)
    if storm_name is None:
        out["note"] = "no return_storm phase in phases_snapshot"
        return out
    onset, storm_end = phase_windows[storm_name]

    decision_ts = []
    for row in decision_rows:
        if (row.get("action_type") or "").strip() != "scale_up":
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is not None:
            decision_ts.append(ts)
    candidates = sorted(ts for ts in decision_ts if ts >= onset)
    if candidates and candidates[0] <= storm_end:
        out["time_to_scaleup"] = candidates[0] - onset
    elif candidates:
        out["note"] = "first scale-up after return_storm end"
    else:
        out["note"] = "no scale_up decision rows"

    spawns = 0
    for row in container_rows:
        ts = _parse_ts(row.get("timestamp_iso"))
        if ts is None or not (onset <= ts <= storm_end):
            continue
        if (row.get("event") or "").strip() != "added":
            continue
        name = (row.get("container") or "").strip()
        if _tier_of_container(name, row.get("image", "")) == "compute":
            spawns += 1
    out["return_spawns"] = spawns

    if arm.startswith("stabilized"):
        # recall_missed = 1 iff a hold-born quarantine produced NO recall row
        # AND it demonstrably ran to a bad end: either it finalized (a
        # qf_pair anchored on the SAME hold-phase qbegin that never got a
        # recall) or — for compute only — no hold-born compute quarantine was
        # recalled at all and the return storm forced a compute respawn. An
        # absent-cleanup clearing the quarantine (no end row, no respawn) is
        # NOT a miss. Reported per tier: compute quarantines and storage
        # retentions are never pooled; success=0 audit recall rows are
        # ignored by the pairing.
        #
        # Cell-aware: a recall is expected only in early-return cells
        # (hold ≤ 600 s — return lands before H=480s expiry at the
        # pre-registered onsets). Late-return cells (hold=900 s)
        # pre-register finalize-before-return, so recall_missed is n/a there.
        hold_name = next((n for n in phase_windows if "hold" in n.lower()),
                         None)
        h_start = h_end = None
        if hold_name is not None:
            h_start, h_end = phase_windows[hold_name]
        if (h_start is None or h_end is None
                or (h_end - h_start) > 600.0):
            if h_start is not None and h_end is not None:
                out["note"] = (
                    f"late-return cell (hold={h_end - h_start:.0f}s): "
                    "recall_missed n/a — finalize-before-return pre-registered")
            return out
        qb_hold: dict[str, list[dict]] = {"compute": [], "storage": []}
        for q in events["quarantine"]:
            if h_start <= q["qbegin"] <= h_end:
                qb_hold[q["tier"] or "compute"].append(q)
        unrecaled_finalized: dict[str, int] = {"compute": 0, "storage": 0}
        for p in events["qf_pairs"]:
            tier = p["tier"] or "compute"
            if not (h_start <= p["begin"] <= h_end):
                continue
            # qf_pairs carry the same parsed float as the quarantine's
            # qbegin (both come from the release-log row), so exact equality
            # is correct; an abort-terminated entry (recall None, abort set)
            # counts as no-recall.
            if any(q["qbegin"] == p["begin"] and q["recall"] is None
                   for q in qb_hold[tier]):
                unrecaled_finalized[tier] += 1
        compute_recalls = any(q["recall"] is not None
                              for q in qb_hold["compute"])
        out["recall_missed"] = int(
            unrecaled_finalized["compute"] >= 1
            or (spawns > 0 and bool(qb_hold["compute"]) and not compute_recalls))
        out["recall_missed_storage"] = int(unrecaled_finalized["storage"] >= 1)
    return out


def _c5_churn(events: dict, container_rows: list[dict]) -> int | None:
    # veth_discovery_failed ends are no-op teardowns (container already gone)
    # — they must not count as release→spawn churn.
    release_ends = [e for e in events["ends"]
                    if e.get("reason") != "veth_discovery_failed"]
    spawns = []
    for row in container_rows:
        if (row.get("event") or "").strip() != "added":
            continue
        ts = _parse_ts(row.get("timestamp_iso"))
        name = (row.get("container") or "").strip()
        if ts is None or not name:
            continue
        spawns.append((ts, _tier_of_container(name, row.get("image", ""))))
    if not release_ends or not spawns:
        return 0 if (release_ends or spawns) else None

    # Tier-tagged pending map: a release end only pairs with a spawn of the
    # same role (compute↔compute, storage↔storage).
    pending = sorted((e["end"], e["tier"]) for e in release_ends)
    used: set[int] = set()
    pairs = 0
    for ts, tier in sorted(spawns):
        # latest unpaired release end within the churn window
        best = None
        for i, (end_ts, end_tier) in enumerate(pending):
            if i in used:
                continue
            if tier and end_tier and tier != end_tier:
                continue
            if 0 < ts - end_ts <= _CHURN_WINDOW_S:
                if best is None or end_ts > pending[best][0]:
                    best = i
        if best is not None:
            used.add(best)
            pairs += 1
    return pairs


# ---------------------------------------------------------------------------
# Per-run analysis
# ---------------------------------------------------------------------------

def _empty_metrics() -> dict:
    metrics = {"C1_compute_p50": None, "C1_compute_p90": None,
               "C1_storage_p50": None, "C1_storage_p90": None,
               "C1_stabilized_recall_p50": None,
               "C1_stabilized_recall_storage_p50": None,
               "C1_stabilized_qf_p50": None,
               "C1_stabilized_qf_storage_p50": None,
               "C1_pair_count": {},
               "orphan_begin": None,
               "C2_attributed_errors": None, "C2_notprimary": None,
               "C2_evict_ok": None, "C2_evict_nonok": None,
               "C2_evict_overlap": None, "C2_ghosts": None,
               "C3_hold_cont_s": None, "C3_drop_cont_s": None,
               "C4_time_to_scaleup_s": None, "C4_return_spawns": None,
               "C4_recall_missed": None, "C4_recall_missed_storage": None,
               "C5_churn_pairs": None}
    return metrics


def analyze_run(run_dir: Path, arm_override: str | None = None) -> dict:
    """Analyze one research_q3 run folder; never raises."""
    try:
        return _analyze_run(run_dir, arm_override)
    except Exception as exc:  # defensive: one broken run must not kill the CLI
        _warn(f"{run_dir}: analysis failed ({exc})")
        return {"run_dir": run_dir, "run_label": run_dir.name,
                "arm": _infer_arm_from_label(run_dir.name) or "unknown",
                "mechanism": _NA,
                "metrics": _empty_metrics(), "safety_rows": [],
                "notes": [f"analysis failed: {exc}"]}


def _analyze_run(run_dir: Path, arm_override: str | None) -> dict:
    run_dir = Path(run_dir)
    label = run_dir.name
    notes: list[str] = []

    release_rows = _load_release_rows(run_dir)
    client_rows = _load_client_rows(run_dir)
    container_rows = _load_container_rows(run_dir)
    decision_rows = _load_decision_rows(run_dir)
    phases, run_origin = _load_phases(run_dir)

    for name, rows in (("client_requests.csv", client_rows),
                       ("container_events.csv", container_rows),
                       ("decision_log_lan{1,2}.csv", decision_rows)):
        if not rows and name != "decision_log_lan{1,2}.csv":
            _warn(f"{label}: {name} missing or empty")
    if not decision_rows:
        _warn(f"{label}: decision_log_lan{{1,2}}.csv missing or empty")

    arm = _infer_arm_from_label(label)
    mechanism = _mechanism_from_rows(release_rows)
    if arm is None and mechanism:
        arm = mechanism
    if arm is None and arm_override:
        arm = arm_override
    if arm is None:
        arm = "unknown"
        _warn(f"{label}: arm not inferable (use --arm)")
    if not release_rows and arm != "off":
        _warn(f"{label}: no release_log rows (arm={arm})")
    if mechanism is None:
        mechanism = arm if arm in ("off", "immediate", "drained", "stabilized") \
            else _NA

    origin = _derive_origin(client_rows, container_rows)
    if origin is None:
        _warn(f"{label}: no timestamp anchor found — phase windows unavailable")
    phase_windows = _phase_windows(phases, origin, run_origin)

    metrics = _empty_metrics()
    events = _release_events(release_rows)

    c1 = _c1_release_speed(events)
    metrics["C1_compute_p50"] = c1["compute_p50"]
    metrics["C1_compute_p90"] = c1["compute_p90"]
    metrics["C1_storage_p50"] = c1["storage_p50"]
    metrics["C1_storage_p90"] = c1["storage_p90"]
    metrics["C1_stabilized_recall_p50"] = c1["recall_p50"]
    metrics["C1_stabilized_recall_storage_p50"] = c1["recall_storage_p50"]
    metrics["C1_stabilized_qf_p50"] = c1["qf_p50"]
    metrics["C1_stabilized_qf_storage_p50"] = c1["qf_storage_p50"]
    metrics["C1_pair_count"] = c1["pair_count"]
    metrics["orphan_begin"] = c1["orphan_begin"] if release_rows else None

    windows = _release_windows(events)
    c2, safety_rows = _c2_safety(run_dir, windows, client_rows)
    metrics["C2_attributed_errors"] = c2["attributed_errors"]
    metrics["C2_notprimary"] = c2["notprimary"]
    metrics["C2_evict_ok"] = c2["evict_ok"]
    metrics["C2_evict_nonok"] = c2["evict_nonok"]
    metrics["C2_evict_overlap"] = c2["evict_overlap"]
    metrics["C2_ghosts"] = c2["ghosts"]

    c3 = _c3_hold_cost(run_dir, phase_windows, container_rows)
    metrics["C3_hold_cont_s"] = c3["hold"]
    metrics["C3_drop_cont_s"] = c3["drop"]
    if c3["note"]:
        notes.append(f"C3: {c3['note']}")

    c4 = _c4_reentry(run_dir, phase_windows, decision_rows, container_rows,
                     arm, events)
    metrics["C4_time_to_scaleup_s"] = c4["time_to_scaleup"]
    metrics["C4_return_spawns"] = c4["return_spawns"]
    metrics["C4_recall_missed"] = c4["recall_missed"]
    metrics["C4_recall_missed_storage"] = c4["recall_missed_storage"]
    if c4["note"]:
        notes.append(f"C4: {c4['note']}")

    metrics["C5_churn_pairs"] = _c5_churn(events, container_rows)

    if arm == "off":
        # The control arm writes no release rows — release-derived metrics
        # are unmeasurable (n/a), not zero.
        metrics["C1_compute_p50"] = metrics["C1_compute_p90"] = None
        metrics["C1_storage_p50"] = metrics["C1_storage_p90"] = None
        metrics["C1_stabilized_recall_p50"] = None
        metrics["C1_stabilized_recall_storage_p50"] = None
        metrics["C1_stabilized_qf_p50"] = None
        metrics["C1_stabilized_qf_storage_p50"] = None
        metrics["orphan_begin"] = None
        metrics["C2_attributed_errors"] = metrics["C2_notprimary"] = None
        metrics["C5_churn_pairs"] = None

    for note in notes:
        _warn(f"{label}: {note}")

    return {"run_dir": run_dir, "run_label": label, "arm": arm,
            "mechanism": mechanism, "metrics": metrics,
            "safety_rows": safety_rows, "notes": notes}


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def _write_table_csv(results: list[dict], out_dir: Path) -> Path:
    path = out_dir / "release_compare_table.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_TABLE_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for res in results:
            m = res["metrics"]
            row = {"run_label": res["run_label"], "arm": res["arm"],
                   "mechanism": res["mechanism"],
                   "C1_compute_p50": _fmt(m["C1_compute_p50"], 3),
                   "C1_storage_p50": _fmt(m["C1_storage_p50"], 3),
                   "C1_stabilized_recall_p50": _fmt(
                       m["C1_stabilized_recall_p50"], 3),
                   "C1_stabilized_recall_storage_p50": _fmt(
                       m["C1_stabilized_recall_storage_p50"], 3),
                   "C1_stabilized_qf_p50": _fmt(
                       m["C1_stabilized_qf_p50"], 3),
                   "C1_stabilized_qf_storage_p50": _fmt(
                       m["C1_stabilized_qf_storage_p50"], 3),
                   "orphan_begin": _fmt_int(m["orphan_begin"]),
                   "C2_attributed_errors": _fmt_int(m["C2_attributed_errors"]),
                   "C2_notprimary": _fmt_int(m["C2_notprimary"]),
                   "C2_evict_ok": _fmt_int(m["C2_evict_ok"]),
                   "C2_evict_nonok": _fmt_int(m["C2_evict_nonok"]),
                   "C2_evict_overlap": _fmt_int(m["C2_evict_overlap"]),
                   "C2_ghosts": _fmt_int(m["C2_ghosts"]),
                   "C3_hold_cont_s": _fmt(m["C3_hold_cont_s"], 1),
                   "C3_drop_cont_s": _fmt(m["C3_drop_cont_s"], 1),
                   "C4_time_to_scaleup_s": _fmt(m["C4_time_to_scaleup_s"], 1),
                   "C4_return_spawns": _fmt_int(m["C4_return_spawns"]),
                   "C4_recall_missed": _fmt_int(m["C4_recall_missed"]),
                   "C4_recall_missed_storage": _fmt_int(
                       m["C4_recall_missed_storage"]),
                   "C5_churn_pairs": _fmt_int(m["C5_churn_pairs"])}
            writer.writerow(row)
    return path


def _write_safety_csv(results: list[dict], out_dir: Path) -> Path:
    path = out_dir / "release_safety_report.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SAFETY_FIELDS)
        writer.writeheader()
        for res in results:
            for srow in res["safety_rows"]:
                writer.writerow({
                    "run_label": res["run_label"], "arm": res["arm"],
                    "tier": srow["tier"],
                    "release_container": srow["release_container"],
                    "release_begin_ts": f"{srow['release_begin_ts']:.3f}",
                    "release_end_ts": f"{srow['release_end_ts']:.3f}",
                    "error_ts": f"{srow['error_ts']:.3f}",
                    "error_kind": srow["error_kind"],
                    "in_window": srow["in_window"],
                    "row_key": srow["row_key"],
                })
    return path


def _print_summary(results: list[dict]) -> None:
    cols = [
        ("run", 30), ("arm", 20),
        ("C1c_p50", 8), ("C1c_p90", 8),
        ("C1s_p50", 8), ("C1s_p90", 8),
        ("C1_rec_p50", 10), ("C1s_rec", 9),
        ("C1_qf_p50", 10), ("C1s_qf", 9),
        ("orph_beg", 8), ("pairs", 6),
        ("C2_err", 7), ("C2_np", 6),
        ("C2_ovlp", 8),
        ("C3_hold", 11), ("C3_drop", 11),
        ("C4_ttsu", 9), ("C4_spw", 8), ("C4_rm_s", 8), ("C5_churn", 9),
    ]
    header = "".join(name.ljust(width) for name, width in cols)
    print(header)
    print("-" * len(header))
    for res in results:
        m = res["metrics"]
        pairs = m["C1_pair_count"]
        pair_txt = "+".join(str(v) for v in pairs.values()) if pairs else _NA
        cells = [
            res["run_label"][:cols[0][1]], res["arm"][:cols[1][1]],
            _fmt(m["C1_compute_p50"]), _fmt(m["C1_compute_p90"]),
            _fmt(m["C1_storage_p50"]), _fmt(m["C1_storage_p90"]),
            _fmt(m["C1_stabilized_recall_p50"], 1),
            _fmt(m["C1_stabilized_recall_storage_p50"], 1),
            _fmt(m["C1_stabilized_qf_p50"], 1),
            _fmt(m["C1_stabilized_qf_storage_p50"], 1),
            _fmt_int(m["orphan_begin"]), pair_txt,
            _fmt_int(m["C2_attributed_errors"]), _fmt_int(m["C2_notprimary"]),
            _fmt_int(m["C2_evict_overlap"]),
            _fmt(m["C3_hold_cont_s"], 0), _fmt(m["C3_drop_cont_s"], 0),
            _fmt(m["C4_time_to_scaleup_s"], 1), _fmt_int(m["C4_return_spawns"]),
            _fmt_int(m["C4_recall_missed_storage"]),
            _fmt_int(m["C5_churn_pairs"]),
        ]
        print("".join(str(c).ljust(w) for c, (_, w) in zip(cells, cols)))


def _make_plot(results: list[dict], out_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        _warn(f"matplotlib not available — skipping PNG ({exc})")
        return

    arms = list(_ARM_ORDER) + [r["arm"] for r in results
                               if r["arm"] not in _ARM_ORDER]
    arms = [a for a in arms if any(r["arm"] == a for r in results)]

    metric_defs = [
        ("C1_compute_p50", "C1 compute p50 (s)", "#1a7abf"),
        ("C1_storage_p50", "C1 storage p50 (s)", "#bf5a1a"),
        ("C2_attributed_errors", "C2 attributed errors", "#1abf4a"),
        ("C3_hold_cont_s", "C3 hold container-s", "#8b1a8b"),
        ("C4_time_to_scaleup_s", "C4 time-to-scale-up (s)", "#bfa01a"),
    ]

    means: dict[str, dict[str, float]] = {key: {} for key, _, _ in metric_defs}
    for arm in arms:
        for key, _, _ in metric_defs:
            vals = [r["metrics"][key] for r in results if r["arm"] == arm]
            vals = [v for v in vals if v is not None]
            if vals:
                means[key][arm] = sum(vals) / len(vals)

    fig, ax = plt.subplots(figsize=(max(6, 2.2 * len(arms) + 4), 7))
    x = np.arange(len(arms))
    n_bars = len(metric_defs)
    width = 0.16
    for i, (key, title, color) in enumerate(metric_defs):
        offset = (i - (n_bars - 1) / 2.0) * width
        values = [means[key].get(arm, 0.0) for arm in arms]
        bars = ax.bar(x + offset, values, width, label=title, color=color,
                      alpha=0.85, edgecolor="black", linewidth=0.5)
        for bar, arm in zip(bars, arms):
            val = means[key].get(arm)
            if val is not None:
                ax.annotate(f"{val:.2g}", (bar.get_x() + bar.get_width() / 2,
                                           bar.get_height()),
                            xytext=(0, 2), textcoords="offset points",
                            ha="center", va="bottom", fontsize=7)

    ax.set_xticks(x)
    ax.set_xticklabels(arms, rotation=0, ha="center", fontsize=9)
    ax.set_title("Research Q3 release-mechanism comparison (per-arm means)")
    ax.set_ylabel("value (mixed units — see legend)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8, ncol=2)

    path = out_dir / "release_compare.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[cli_release_compare] wrote {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compare research_q3 release-mechanism arms across run folders.")
    parser.add_argument("--run-dir", action="append", required=True,
                        metavar="DIR", help="research_q3 run folder "
                        "(repeatable)")
    parser.add_argument("--output-dir", metavar="DIR",
                        help="output directory (default: release_compare/ "
                        "in cwd)")
    parser.add_argument("--arm", metavar="LABEL",
                        help="fallback arm label for runs whose arm cannot "
                        "be inferred (e.g. off/immediate/drained/stabilized)")
    parser.add_argument("--output-csv-only", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    out_dir = Path(args.output_dir) if args.output_dir \
        else Path.cwd() / "release_compare"
    out_dir.mkdir(parents=True, exist_ok=True)

    results = [analyze_run(Path(d), args.arm) for d in args.run_dir]
    if not results:
        _warn("no run directories provided")
        return

    table_path = _write_table_csv(results, out_dir)
    safety_path = _write_safety_csv(results, out_dir)
    print(f"[cli_release_compare] wrote {table_path}")
    print(f"[cli_release_compare] wrote {safety_path}")
    _print_summary(results)
    if not args.output_csv_only:
        _make_plot(results, out_dir)


if __name__ == "__main__":
    main()
