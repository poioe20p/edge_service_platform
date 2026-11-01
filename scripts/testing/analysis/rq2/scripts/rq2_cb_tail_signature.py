#!/usr/bin/env python3
"""RQ2 Part E - compute-bound tail-signature extractor (O1/O2/O3).

Pre-registered tool of docs/operation/testing/experiment/rq2_extension/
parte_addendum.md ("§6.2 Signature extractor"; observables, thresholds and
verdict rules in §4).

Measures, per run, completed-only and both lanes pooled, nearest-rank
percentiles (the pinned figure-battery convention):

  * ep_p50 / ep_p95    - percentiles over every episode latency value that
                         parses (a row with an unparseable sent_at but a valid
                         latency still contributes to these, but to no timed
                         window; parse failures are counted in the flags,
                         never silently dropped).
  * O1 (onset)         - p50 of the episode rows with
                         sent_at - first_episode_t < 60 s (minute 0), where
                         first_episode_t = min sent_at over the pooled episode
                         rows. Cell-level: present when the median m0_p50 is
                         >= 0.10 s; reported, but does not enter the verdict.
  * steady             - p50 of the episode rows with dt >= 60 s; plus
                         o1_ratio = m0_p50 / steady_p50 (None-safe).
  * O2 (decides)       - episode p95: >= 1.0 s reproduced, >= 0.05 s and
                         < 1.0 s partial, < 0.05 s absent.
  * O3 (informational) - 30 s buckets (floor(dt / 30)) whose nearest-rank p50
                         is >= 0.25 s: count plus first/last bucket start
                         (episode-relative seconds).
  * compute adds       - per-lane epoch times and counts of the
                         node_lifecycle_timings.csv rows with operation
                         "add" and node_type "compute" (candidates whose
                         timestamp or controller does not parse are counted
                         as compute_add_parse_errors).

sent_at is ISO-8601; a naive value (no offset) is pinned to UTC so absolute
epochs share one time frame regardless of host timezone (relative O1/O3 are
unaffected).

Folder discovery and selection:
  * legacy family ("..._rq2_<nn|cf|sf|ba>_cb_<1..6>", timestamped folder
    names): all matches are kept - historical record, no dedup.
  * era-matched family ("..._rq2pe_<cf|sf|ba>_cb_<1..6>[_r2]"): the §5
    first-valid / earliest dedup is applied here by folder selection.
    Folders are grouped by label with a trailing "_r2" ignored
    (rq2pe_cf_cb_1 and rq2pe_cf_cb_1_r2 are the same replicate); the earliest
    folder (lexicographically smallest name = earliest timestamp) is chosen
    and the later duplicates are reported as spares (kept on disk, never
    pooled, never counted elsewhere). The validity battery itself is the
    operator's gate step, not this tool's: the operator drives the selection
    with --exclude for folders the battery rejects - full folder names
    disambiguate same-label duplicates, labels are accepted as suffix matches
    - and the dedup then picks the earliest remaining folder. The chosen
    folder per replicate is recorded in runs[] (JSON) for the Part E record.
  * --exclude is repeatable; a folder is excluded when its basename equals a
    value or ends with "_" + value. Excluding a base label therefore does not
    drop its "_r2" relaunch sibling (its name does not end with the base
    label; pass its own value or the full folder name to remove the whole
    replicate). The tool prints a note whenever an excluded era-matched folder
    still has eligible folders under the same label (e.g. a relaunch `_r2`
    sibling or a retry duplicate) and warns about values that match no run
    folder (empty values included);
    both are carried in the JSON notes[]. The matchable forms are the full
    folder name or the run-label suffix (`rq2pe_cf_cb_1`, `rq2pe_cf_cb_1_r2`
    for the relaunch) - the printed cell name (`pe_cf_cb_1`) is not a
    folder-label form and never matches.

Validity flags (per run; printed and carried in the JSON): missing_client_csv,
missing_lifecycle_csv (absent, unreadable, or missing a required column),
no_episode_rows, sent_at_parse_errors, latency_parse_errors,
compute_add_parse_errors (add/compute rows whose timestamp or controller does
not parse - a format drift never reads as a clean "0 adds" run). Parse errors
are counted among the kept rows, never silently dropped; a non-finite latency
value counts as a parse error. These are extraction-health flags, not
the §4 battery: flagged runs still contribute their parseable values, the
battery is the operator's gate (rejected folders are removed with --exclude),
and the cell floor counts runs that contribute the deciding ep_p95.

Per-cell verdict table (era-matched cells; §4): cell value = median over the
chosen runs of the per-run metric; a cell with fewer than 5 usable runs is
descriptive-only. Secondary (informational) read: era-matched cell median
ep_p95 divided by the pinned figure-battery comparator (cf_cb 5.07 s,
sf_cb 3.96 s, ba_cb 3.35 s - "pinned figure-battery comparator,
parte_addendum §4"): <= 0.1 build-era artifact signature; > 0.1 and <= 0.5
strongly attenuated; > 0.5 broadly reproduced. §4 sensitivities are reported
per era-matched cell: the seed-42 subset median (replicates 1-5 of the chosen
runs) and, for cf/ba, the ratio against the pinned 512m-subset comparators
(cf 5.51 s, ba 3.48 s - "pinned 512m-subset comparator, parte_addendum §4");
every era-matched run carries cap_label "EDGE_MEMORY=512m". When an arm has
no era-matched runs yet, the table shows its legacy cell as an informational
reference line (descriptive-only, never a verdict).

Read-only with respect to the metrics tree (this tool never writes there);
the optional --json PATH writes only where told. The JSON carries the eight
top-level keys generated_at, metrics_dir, exclude, runs, cells, legacy_cells,
spares, excluded, plus notes[] (selection warnings: unmatched --exclude
values, still-eligible same-label folders, a missing metrics dir).

Usage:
    python3 rq2_cb_tail_signature.py \
        [--metrics-dir ~/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics] \
        [--exclude FOLDER_OR_LABEL ...] [--json /tmp/rq2_cb_tail_signature.json]
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import statistics
from datetime import datetime, timezone

LEGACY_RE = re.compile(r"^\d{8}_\d{6}_rq2_(nn|cf|sf|ba)_cb_([1-6])$")
RQ2PE_RE = re.compile(r"^\d{8}_\d{6}_rq2pe_(cf|sf|ba)_cb_([1-6])(_r2)?$")

DEFAULT_METRICS = "~/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"

EP_PHASE = "compute_bound_episode"
ARM_ORDER = ["nn", "cf", "sf", "ba"]
ERA_ARMS = ["cf", "sf", "ba"]

O1_MIN_S = 0.10
O2_REPRODUCED_S = 1.0
O2_PARTIAL_S = 0.05
O3_BUCKET_S = 30
O3_P50_MIN_S = 0.25
MIN_RUNS = 5

PINNED_EP_P95_S = {"cf": 5.07, "sf": 3.96, "ba": 3.35}
PINNED_LABEL = "pinned figure-battery comparator, parte_addendum \u00a74"
PINNED_EP_P95_512M_S = {"cf": 5.51, "ba": 3.48}
PINNED_512M_LABEL = "pinned 512m-subset comparator, parte_addendum \u00a74"
CAP_LABEL = "EDGE_MEMORY=512m"


# --------------------------------------------------------------------------
# helpers (same convention as generate_thesis_figures.py)
# --------------------------------------------------------------------------
def pct(v, q):
    """Nearest-rank percentile (matches qa_verify.py)."""
    v = sorted(v)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def ts_iso(s):
    """ISO-8601 sent_at -> epoch (whitespace/quotes stripped, Z -> +00:00);
    a naive value (no offset) is pinned to UTC so absolute epochs share one
    time frame regardless of host timezone (relative O1/O3 unaffected)."""
    try:
        s = (s or "").strip().strip('"').strip()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def ts_el(s):
    """Log-style lifecycle timestamp -> epoch, pinned to UTC (the sent_at
    time base; the campaign VM runs UTC)."""
    s = (s or "").strip().strip('"').strip()
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def run_files(path):
    agg = os.path.join(path, "client_requests.csv")
    if os.path.exists(agg):
        return [agg]
    return sorted(glob.glob(os.path.join(path, "client_requests_lan*_client_*.csv")))


def fnum(v, nd=4):
    return "-" if v is None else ("%.*f" % (nd, v))


def print_table(rows):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


# --------------------------------------------------------------------------
# folder discovery / selection
# --------------------------------------------------------------------------
def is_excluded(name, excludes):
    for value in excludes:
        value = (value or "").strip()
        if not value:
            continue
        if name == value or name.endswith("_" + value):
            return True
    return False


def scan_folders(metrics_dir, excludes):
    """(entries, excluded_names, matched_exclude_values)."""
    entries = []
    excluded = []
    matched_values = set()
    try:
        names = sorted(os.listdir(metrics_dir))
    except OSError:
        return entries, excluded, matched_values
    for name in names:
        path = os.path.join(metrics_dir, name)
        if not os.path.isdir(path):
            continue
        m = LEGACY_RE.match(name)
        if m:
            family, arm, rep, relaunch = "legacy", m.group(1), int(m.group(2)), False
        else:
            m = RQ2PE_RE.match(name)
            if not m:
                continue
            family, arm, rep, relaunch = "rq2pe", m.group(1), int(m.group(2)), bool(m.group(3))
        hit = [v.strip() for v in excludes if v.strip() and is_excluded(name, [v])]
        if hit:
            for v in hit:
                matched_values.add(v)
            excluded.append(name)
            continue
        label = "%s_%s_cb_%d" % ("rq2pe" if family == "rq2pe" else "rq2", arm, rep)
        cell = ("%s_cb" % arm) if family == "legacy" else ("pe_%s_cb" % arm)
        entries.append({
            "folder": name, "path": path, "family": family, "arm": arm,
            "replicate": rep, "relaunch": relaunch, "label": label, "cell": cell,
        })
    return entries, excluded, matched_values


def select_runs(entries):
    """Legacy kept whole; rq2pe deduplicated to the earliest folder per label."""
    chosen = [e for e in entries if e["family"] == "legacy"]
    spares = []
    groups = {}
    for e in sorted([e for e in entries if e["family"] == "rq2pe"], key=lambda x: x["folder"]):
        groups.setdefault(e["label"], []).append(e)
    for label in sorted(groups):
        members = groups[label]
        pick = members[0]
        chosen.append(pick)
        for e in members[1:]:
            spares.append({
                "folder": e["folder"], "path": e["path"], "label": label,
                "duplicate_of": pick["folder"],
            })
    return chosen, spares


# --------------------------------------------------------------------------
# per-run computation
# --------------------------------------------------------------------------
def compute_adds(path):
    """(lan -> sorted epoch times of compute adds, parse-error count); the
    first element is None when the CSV is missing, unreadable, or lacks a
    required column. Candidates (operation=add, node_type=compute) whose
    timestamp fails to parse or whose controller is empty are counted, so a
    format drift never reads as a clean "0 adds"."""
    p = os.path.join(path, "node_lifecycle_timings.csv")
    if not os.path.exists(p):
        return None, 0
    per_lane = {}
    add_err = 0
    required = ("timestamp", "controller", "operation", "node_type")
    try:
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            rdr = csv.DictReader(fh)
            if any(col not in (rdr.fieldnames or []) for col in required):
                return None, 0
            for r in rdr:
                if (r.get("operation") or "").strip() != "add":
                    continue
                if (r.get("node_type") or "").strip() != "compute":
                    continue
                t = ts_el(r.get("timestamp"))
                lan = (r.get("controller") or "").strip()
                if t is None or not lan:
                    add_err += 1
                    continue
                per_lane.setdefault(lan, []).append(t)
    except OSError:
        return None, 0
    for lan in per_lane:
        per_lane[lan].sort()
    return per_lane, add_err


def analyze_run(entry):
    path = entry["path"]
    flags = []

    files = run_files(path)
    if not files:
        flags.append("missing_client_csv")

    ep_rows = 0
    sent_err = 0
    lat_err = 0
    sent_times = []
    latencies = []
    timed = []
    for f in files:
        try:
            with open(f, newline="", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    if (r.get("phase") or "").strip() != EP_PHASE:
                        continue
                    if (r.get("status") or "").strip() != "completed":
                        continue
                    ep_rows += 1
                    sent = ts_iso(r.get("sent_at"))
                    try:
                        lat = float((r.get("latency_s") or "").strip())
                    except (TypeError, ValueError):
                        lat = None
                    if lat is not None and (math.isnan(lat) or math.isinf(lat)):
                        lat = None
                    if sent is None:
                        sent_err += 1
                    else:
                        sent_times.append(sent)
                    if lat is None:
                        lat_err += 1
                    else:
                        latencies.append(lat)
                    if sent is not None and lat is not None:
                        timed.append((sent, lat))
        except OSError:
            if "missing_client_csv" not in flags:
                flags.append("missing_client_csv")
    if ep_rows == 0:
        flags.append("no_episode_rows")
    if sent_err:
        flags.append("sent_at_parse_errors")
    if lat_err:
        flags.append("latency_parse_errors")

    first = min(sent_times) if sent_times else None
    m0 = []
    steady = []
    if first is not None:
        for s, lat in timed:
            if s - first < 60.0:
                m0.append(lat)
            else:
                steady.append(lat)
    m0_p50 = pct(m0, 0.5)
    steady_p50 = pct(steady, 0.5)
    o1_ratio = None
    if m0_p50 is not None and steady_p50 is not None and steady_p50 > 0:
        o1_ratio = m0_p50 / steady_p50

    ep_p50 = pct(latencies, 0.5)
    ep_p95 = pct(latencies, 0.95)

    buckets = {}
    if first is not None:
        for s, lat in timed:
            idx = int(math.floor((s - first) / O3_BUCKET_S))
            buckets.setdefault(idx, []).append(lat)
    hot = sorted(idx for idx, v in buckets.items() if pct(v, 0.5) >= O3_P50_MIN_S)

    adds, add_err = compute_adds(path)
    if adds is None:
        flags.append("missing_lifecycle_csv")
        counts = {"lan1": 0, "lan2": 0}
        lan1 = []
        lan2 = []
    else:
        if add_err:
            flags.append("compute_add_parse_errors")
        lan1 = list(adds.get("lan1", []))
        lan2 = list(adds.get("lan2", []))
        counts = {"lan1": len(lan1), "lan2": len(lan2)}
        for lan in sorted(adds):
            if lan not in counts:
                counts[lan] = len(adds[lan])

    info = dict(entry)
    info.update({
        "n_episode_rows": ep_rows,
        "n_samples": len(timed),
        "sent_at_parse_errors": sent_err,
        "latency_parse_errors": lat_err,
        "compute_add_parse_errors": add_err,
        "flags": flags,
        "first_episode_t": first,
        "m0_p50_s": m0_p50,
        "steady_p50_s": steady_p50,
        "o1_ratio": o1_ratio,
        "ep_p50_s": ep_p50,
        "ep_p95_s": ep_p95,
        "o3_buckets_ge_025": len(hot),
        "o3_first_s": (hot[0] * O3_BUCKET_S) if hot else None,
        "o3_last_s": (hot[-1] * O3_BUCKET_S) if hot else None,
        "compute_adds_lan1": lan1,
        "compute_adds_lan2": lan2,
        "compute_add_lane_counts": counts,
    })
    return info


# --------------------------------------------------------------------------
# per-cell aggregation / classification
# --------------------------------------------------------------------------
def cell_medians(runs):
    return {
        "n_runs": len(runs),
        "n_used": len([r for r in runs if r.get("ep_p95_s") is not None]),
        "median_m0_p50_s": med([r.get("m0_p50_s") for r in runs]),
        "median_steady_p50_s": med([r.get("steady_p50_s") for r in runs]),
        "median_o1_ratio": med([r.get("o1_ratio") for r in runs]),
        "median_ep_p50_s": med([r.get("ep_p50_s") for r in runs]),
        "median_ep_p95_s": med([r.get("ep_p95_s") for r in runs]),
    }


def classify_o1(v):
    if v is None:
        return None
    return "present" if v >= O1_MIN_S else "absent"


def classify_o2(v):
    if v is None:
        return None
    if v >= O2_REPRODUCED_S:
        return "reproduced"
    if v >= O2_PARTIAL_S:
        return "partial"
    return "absent"


def ratio_class(v):
    if v is None:
        return None
    if v <= 0.1:
        return "build-era artifact signature"
    if v <= 0.5:
        return "strongly attenuated"
    return "broadly reproduced"


def run_replicate(r):
    """Replicate number from the run dict, else derived from the folder label."""
    rep = r.get("replicate")
    if rep is not None:
        return rep
    m = RQ2PE_RE.match(r.get("folder", "")) or LEGACY_RE.match(r.get("folder", ""))
    return int(m.group(2)) if m else None


def analyze_era_cell(arm, rq2pe_runs, legacy_runs):
    cell = "pe_%s_cb" % arm
    chosen = [r for r in rq2pe_runs if r["arm"] == arm]
    source = "rq2pe" if chosen else "none"
    stats = cell_medians(chosen)
    o1 = classify_o1(stats["median_m0_p50_s"])
    o2 = classify_o2(stats["median_ep_p95_s"])
    floor = {"min_runs": MIN_RUNS, "met": stats["n_used"] >= MIN_RUNS}
    if not floor["met"]:
        verdict = "descriptive-only"
        reason = "%d usable era-matched run(s) < floor %d" % (stats["n_used"], MIN_RUNS)
    else:
        verdict = o2
        reason = "median ep_p95 = %.4f s (O2 decides)" % stats["median_ep_p95_s"]

    comparator = PINNED_EP_P95_S.get(arm)
    ratio = None
    rclass = None
    if source == "rq2pe" and stats["median_ep_p95_s"] is not None and comparator:
        ratio = stats["median_ep_p95_s"] / comparator
        rclass = ratio_class(ratio)

    seed42_runs = [r for r in chosen if (run_replicate(r) or 0) in (1, 2, 3, 4, 5)]
    med_ep_p95_seed42 = med([r.get("ep_p95_s") for r in seed42_runs])
    subset_comparator = PINNED_EP_P95_512M_S.get(arm)
    ratio_512m = None
    if (source == "rq2pe" and stats["median_ep_p95_s"] is not None
            and subset_comparator):
        ratio_512m = stats["median_ep_p95_s"] / subset_comparator

    legacy_ref = None
    if not chosen:
        lr = [r for r in legacy_runs if r["arm"] == arm]
        if lr:
            lstats = cell_medians(lr)
            legacy_ref = {
                "cell": "%s_cb" % arm,
                "n_runs": lstats["n_runs"],
                "n_used": lstats["n_used"],
                "median_m0_p50_s": lstats["median_m0_p50_s"],
                "median_steady_p50_s": lstats["median_steady_p50_s"],
                "median_o1_ratio": lstats["median_o1_ratio"],
                "median_ep_p50_s": lstats["median_ep_p50_s"],
                "median_ep_p95_s": lstats["median_ep_p95_s"],
                "note": "legacy source - informational only, never a verdict",
            }

    flagged = [r for r in chosen if r.get("flags")]
    if flagged:
        reason += ("; %d chosen run(s) carry extraction flags (see runs[]) - the "
                   "section-4 validity battery remains the operator's gate (--exclude)"
                   % len(flagged))

    out = {"cell": cell, "arm": arm, "family": "rq2pe", "source": source}
    out.update(stats)
    out.update({
        "chosen_folders": [r["folder"] for r in chosen],
        "n_flagged": len(flagged),
        "flagged_folders": [r["folder"] for r in flagged],
        "o1": o1,
        "o1_threshold_s": O1_MIN_S,
        "o2": o2,
        "verdict": verdict,
        "verdict_reason": reason,
        "floor": floor,
        "comparator": {"value_s": comparator, "label": PINNED_LABEL},
        "ratio_vs_v3": ratio,
        "ratio_class": rclass,
        "cap_label": CAP_LABEL,
        "median_ep_p95_seed42_s": med_ep_p95_seed42,
        "ratio_vs_v3_512m_subset": ratio_512m,
        "comparator_512m": ({"value_s": subset_comparator, "label": PINNED_512M_LABEL}
                            if subset_comparator else None),
        "legacy_reference": legacy_ref,
    })
    return out


def analyze_legacy_cell(arm, legacy_runs):
    cell = "%s_cb" % arm
    chosen = [r for r in legacy_runs if r["arm"] == arm]
    stats = cell_medians(chosen)
    out = {"cell": cell, "arm": arm, "family": "legacy", "source": "legacy"}
    out.update(stats)
    out.update({
        "chosen_folders": [r["folder"] for r in chosen],
        "o1": classify_o1(stats["median_m0_p50_s"]),
        "o2": classify_o2(stats["median_ep_p95_s"]),
        "note": "informational era-contrast; no verdict (verdicts need >= %d era-matched runs)" % MIN_RUNS,
    })
    return out


# --------------------------------------------------------------------------
# stdout tables
# --------------------------------------------------------------------------
def print_run_table(runs):
    print("")
    print("Per-run signature (completed-only, both lanes pooled; seconds)")
    header = ["run", "fam", "cell", "rep", "n_ep", "n_samp", "m0_p50_s",
              "steady_p50_s", "o1_ratio", "ep_p50_s", "ep_p95_s", "o3>=0.25",
              "o3_first_s", "o3_last_s", "adds_lan1", "adds_lan2", "flags"]
    rows = [header]
    for r in runs:
        flags = []
        for name in r["flags"]:
            if name == "sent_at_parse_errors":
                flags.append("sent_at_parse_errors=%d" % r["sent_at_parse_errors"])
            elif name == "latency_parse_errors":
                flags.append("latency_parse_errors=%d" % r["latency_parse_errors"])
            elif name == "compute_add_parse_errors":
                flags.append("compute_add_parse_errors=%d" % r["compute_add_parse_errors"])
            else:
                flags.append(name)
        rows.append([
            r["folder"], r["family"], r["cell"], str(r["replicate"]),
            str(r["n_episode_rows"]), str(r["n_samples"]),
            fnum(r["m0_p50_s"]), fnum(r["steady_p50_s"]), fnum(r["o1_ratio"], 2),
            fnum(r["ep_p50_s"]), fnum(r["ep_p95_s"]),
            str(r["o3_buckets_ge_025"]), fnum(r["o3_first_s"], 1),
            fnum(r["o3_last_s"], 1),
            str(r["compute_add_lane_counts"].get("lan1", 0)),
            str(r["compute_add_lane_counts"].get("lan2", 0)),
            ("!" + ",".join(flags)) if flags else "-",
        ])
    print_table(rows)


def print_cell_table(cells):
    print("")
    print("Era-matched per-cell verdicts (cell = median over chosen runs; O2 decides; floor = %d usable runs)" % MIN_RUNS)
    header = ["cell", "source", "runs/used", "m0_p50_s", "steady_p50_s", "ep_p50_s",
              "ep_p95_s", "ep95_seed42", "O1", "O2", "verdict", "ratio_vs_v3",
              "ratio_class", "ratio_512m"]
    rows = [header]
    for c in cells:
        verdict = c["verdict"]
        if verdict == "descriptive-only":
            verdict = "descriptive-only (%d/%d)" % (c["n_used"], MIN_RUNS)
        rows.append([
            c["cell"], c["source"], "%d/%d" % (c["n_runs"], c["n_used"]),
            fnum(c["median_m0_p50_s"]), fnum(c["median_steady_p50_s"]),
            fnum(c["median_ep_p50_s"]), fnum(c["median_ep_p95_s"]),
            fnum(c["median_ep_p95_seed42_s"]),
            c["o1"] or "-", c["o2"] or "-", verdict,
            fnum(c["ratio_vs_v3"], 2), c["ratio_class"] or "-",
            fnum(c["ratio_vs_v3_512m_subset"], 2),
        ])
    print_table(rows)
    print("  cap label: %s for all era-matched (Part E) runs (ratio_512m informational)"
          % CAP_LABEL)
    for c in cells:
        if c["verdict"] == "descriptive-only" or c.get("n_flagged"):
            print("  note %s: %s" % (c["cell"], c["verdict_reason"]))
    for c in cells:
        lr = c.get("legacy_reference")
        if not lr:
            continue
        print("  legacy ref %s: %d run(s) (%s) m0_p50_s=%s steady_p50_s=%s ep_p95_s=%s"
              " -> descriptive-only (legacy source, informational only)"
              % (lr["cell"], lr["n_used"],
                 "< 5 floor" if lr["n_used"] < MIN_RUNS else ">= 5 floor",
                 fnum(lr["median_m0_p50_s"]), fnum(lr["median_steady_p50_s"]),
                 fnum(lr["median_ep_p95_s"])))


def print_legacy_table(legacy_cells):
    print("")
    print("Legacy cells (informational era-contrast; medians over all chosen legacy runs; no verdicts)")
    header = ["cell", "runs/used", "m0_p50_s", "steady_p50_s", "o1_ratio",
              "ep_p50_s", "ep_p95_s", "O1", "O2"]
    rows = [header]
    for c in legacy_cells:
        rows.append([
            c["cell"], "%d/%d" % (c["n_runs"], c["n_used"]),
            fnum(c["median_m0_p50_s"]), fnum(c["median_steady_p50_s"]),
            fnum(c["median_o1_ratio"], 2), fnum(c["median_ep_p50_s"]),
            fnum(c["median_ep_p95_s"]), c["o1"] or "-", c["o2"] or "-",
        ])
    print_table(rows)


def print_aux(spares, excluded, notes):
    print("")
    print("Spares (rq2pe duplicate labels; kept on disk, not pooled, not counted): %d" % len(spares))
    for s in spares:
        print("  %s  ->  spare of %s (%s)" % (s["folder"], s["duplicate_of"], s["label"]))
    print("")
    print("Excluded (--exclude FULL_NAME_OR_LABEL): %d" % len(excluded))
    for name in excluded:
        print("  %s" % name)
    if notes:
        print("")
        print("Selection notes: %d" % len(notes))
        for note in notes:
            print("  %s" % note)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="RQ2 Part E compute-bound tail-signature extractor (O1/O2/O3; "
                    "parte_addendum sections 4 and 6.2)")
    ap.add_argument("--metrics-dir", default=DEFAULT_METRICS,
                    help="run-folder root (default: %(default)s)")
    ap.add_argument("--exclude", action="append", default=[], metavar="FOLDER_OR_LABEL",
                    help="drop a folder whose basename equals the value or ends with "
                         "'_' + value; repeatable; use the full folder name or the "
                         "rq2pe_<arm>_cb_<rep>[_r2] label suffix (a base label does "
                         "not drop its _r2 sibling); unmatched/empty values are reported")
    ap.add_argument("--json", metavar="PATH", default=None,
                    help="also write the machine-readable JSON to PATH "
                         "(the JSON is always printed to stdout)")
    args = ap.parse_args()

    metrics_dir = os.path.abspath(os.path.expanduser(args.metrics_dir))
    entries, excluded, matched_excludes = scan_folders(metrics_dir, args.exclude)
    unmatched = []
    for v in (args.exclude or []):
        key = v.strip()
        if key and key in matched_excludes:
            continue
        shown = key if key else "(empty)"
        if shown not in unmatched:
            unmatched.append(shown)
    chosen, spares = select_runs(entries)
    chosen.sort(key=lambda e: (0 if e["family"] == "legacy" else 1,
                               ARM_ORDER.index(e["arm"]), e["replicate"], e["folder"]))
    runs = [analyze_run(e) for e in chosen]
    legacy_runs = [r for r in runs if r["family"] == "legacy"]
    rq2pe_runs = [r for r in runs if r["family"] == "rq2pe"]
    cells = [analyze_era_cell(arm, rq2pe_runs, legacy_runs) for arm in ERA_ARMS]
    legacy_cells = [analyze_legacy_cell(arm, legacy_runs) for arm in ARM_ORDER]

    notes = []
    for value in unmatched:
        notes.append("exclude value matched no run folder: %s (use the full folder name "
                     "or the rq2pe_<arm>_cb_<rep>[_r2] label; cell-style names such as "
                     "pe_cf_cb_1 do not match)" % value)
    for name in excluded:
        m = RQ2PE_RE.match(name)
        if not m:
            continue
        label = "rq2pe_%s_cb_%d" % (m.group(1), int(m.group(2)))
        others = sorted(e["folder"] for e in entries
                        if e["family"] == "rq2pe" and e["label"] == label)
        if others:
            notes.append("%s excluded, but folder(s) with the same label are still "
                         "eligible: %s (the dedup selects the earliest eligible folder "
                         "per label; pass each folder's own name to drop them)"
                         % (name, ", ".join(others)))

    if not os.path.isdir(metrics_dir):
        notes.append("metrics dir not found: %s" % metrics_dir)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics_dir": metrics_dir,
        "exclude": list(args.exclude or []),
        "runs": runs,
        "cells": cells,
        "legacy_cells": legacy_cells,
        "spares": spares,
        "excluded": excluded,
        "notes": notes,
    }

    print("RQ2 Part E - compute-bound tail-signature extractor (O1/O2/O3; parte_addendum 4/6.2)")
    print("metrics dir : %s" % metrics_dir)
    print("generated at: %s" % doc["generated_at"])
    print("selection   : %d chosen (legacy %d, rq2pe %d), %d spare(s), %d excluded"
          % (len(runs), len(legacy_runs), len(rq2pe_runs), len(spares), len(excluded)))
    if args.exclude:
        print("exclude     : %s" % ", ".join(args.exclude))
    if unmatched:
        print("warning     : --exclude value(s) matched no run folder: %s" % ", ".join(unmatched))
    if not os.path.isdir(metrics_dir):
        print("warning: metrics dir not found")

    print_run_table(runs)
    print_cell_table(cells)
    print_legacy_table(legacy_cells)
    print_aux(spares, excluded, notes)

    payload = json.dumps(doc, indent=2, allow_nan=False)
    print("")
    print(payload)
    if args.json:
        out_path = os.path.abspath(os.path.expanduser(args.json))
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
            print("")
            print("JSON written to: %s" % out_path)
        except OSError as exc:
            print("")
            print("warning: could not write JSON to %s: %s" % (out_path, exc))


if __name__ == "__main__":
    main()
