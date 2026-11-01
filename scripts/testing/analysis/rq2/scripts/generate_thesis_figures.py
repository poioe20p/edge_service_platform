#!/usr/bin/env python3
"""RQ2 thesis figures — latency pairs, consequence map (archive), engagement, relief.

Produces the three RQ2 figures referenced by section 5.3 (results, RQ2):

  1. rq2_consequence_map.png
     Episode endpoint latency per cell against the no-action comparator.
     Left panel: data-access-bound episode, p95 (the pre-registered storage
     metric); right panel: compute-bound episode, p50 (the null reading —
     p95 on this episode is dominated by the v3 start-up transient that the
     extension build lacks, so it is not comparable across eras).
     Convention: status=completed only, both lanes pooled per replicate,
     nearest-rank percentile (qa_verify.py / latency_summary convention).

  2. rq2_compute_engagement.png
     Compute-action engagement on the compute-bound episode.
     Left: share of each lane's requests served by the added backends after
     the first compute addition (dots = per-lane values, bars = medians;
     added vs original segments; grey = original infrastructure), with the
     storage-only cell as the scaled-nothing reference. Right: per-backend
     CPU as two bars per arm, original infrastructure (grey) against the
     added backends (arm colour), over the storage-only single-backend load
     as the dashed reference. Added backends are classified by first
     per_node_stats appearance at/after the lane's first compute add.
     Convention matches temp/qa_dist.py (per-server medians); the lane
     total (sum of per-server medians) stays in the printed summary and
     the CSV.

  3. rq2_storage_relief_v2.png
     Storage-tier relief on the data-access-bound episode, restyled from the
     B2 synthesis (rq2_v3_b2_synthesis.py): per-lane post/pre peak storage
     CPU ratio from each run's analysis/rq2_v3_windows.csv, per-replicate
     value = mean of the two lanes, bars = mean +/- std, dots = replicates,
     against the pre-registered 0.75 threshold.

  4. rq2_latency_data_access.png
     Data-access-bound episode at both metrics: p95 (left, log axis) and
     median (right), no-action comparator included.

  5. rq2_latency_compute_bound.png
     Compute-bound episode at both metrics: median (left) and p95 (right,
     log axis), no-action comparator included. In legacy mode the panel
     mixes builds (the original campaign's start-up transient, reproduced
     by the zero-compute-action control, dominates the p95); era-matched
     mode renders all four arms on one build and drops the cross-era
     label.

Run on the VM (reads the run folders):

    python3 generate_thesis_figures.py --out-dir /tmp/rq2_thesis_figs

Also writes rq2_thesis_figure_data.csv (every value drawn).

Era-matched mode (Part E, pre-registered):

    python3 generate_thesis_figures.py --cb-family rq2pe \
        --out-dir /tmp/rq2_thesis_figs

--cb-family rq2pe rebuilds only rq2_latency_compute_bound.png (cf/sf/ba
from era-matched rq2pe_<arm>_cb_<rep> folders, nn reused from the legacy
rq2_nn_cb_* set, cross-era annotation dropped) and rq2_compute_engagement.png,
and writes rq2_thesis_figure_data_era_matched.csv instead of the legacy CSV
(which stays intact as the historical record). Era-matched replicates are
deduplicated to the earliest folder per label (a trailing _r2 is ignored;
ignored spares are printed). --exclude <folder-or-label> (repeatable) drops
matching run folders in either mode: exact folder name or _<label> suffix
(a base label does not drop its _r2 sibling - use the full folder name to
disambiguate); empty and unmatched values are reported as warnings.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import re
import statistics
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

RUN_RE = re.compile(r"^\d{8}_\d{6}_rq2_(nn|cf|sf|ba)_(cb|db)_([1-6])$")
PE_RUN_RE = re.compile(r"^\d{8}_\d{6}_rq2pe_(cf|sf|ba)_cb_([1-6])(_r2)?$")
EP_PHASE = {"cb": "compute_bound_episode", "db": "data_bound_episode"}
COLOR = {"cf": "#4C72B0", "sf": "#DD8452", "ba": "#55A868", "nn": "#9E9E9E"}
GREY = "#BFBFBF"
LABEL = {"nn": "No action", "cf": "Compute-only", "sf": "Storage-only",
         "ba": "Bottleneck-aware"}

plt.rcParams.update({"font.size": 12, "axes.labelsize": 12,
                     "xtick.labelsize": 10.5, "ytick.labelsize": 11,
                     "figure.dpi": 150, "savefig.dpi": 200,
                     "savefig.bbox": "tight"})


# --------------------------------------------------------------------------
# helpers
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
    try:
        return datetime.fromisoformat((s or "").strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ts_el(s):
    s = (s or "").strip().strip('"')
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    return None


def run_files(path):
    agg = os.path.join(path, "client_requests.csv")
    if os.path.exists(agg):
        return [agg]
    return sorted(glob.glob(os.path.join(path, "client_requests_lan*_client_*.csv")))


def excluded(name, excludes):
    """--exclude match: exact folder name or _<label> suffix (stripped)."""
    for value in excludes:
        value = (value or "").strip()
        if not value:
            continue
        if name == value or name.endswith("_" + value):
            return True
    return False


def unmatched_excludes(metrics, excludes):
    """--exclude values matching no candidate run folder under metrics."""
    names = []
    try:
        for name in os.listdir(metrics):
            if os.path.isdir(os.path.join(metrics, name)) and (
                    RUN_RE.match(name) or PE_RUN_RE.match(name)):
                names.append(name)
    except OSError:
        pass
    out = []
    for value in excludes:
        if value not in out and not any(excluded(name, [value]) for name in names):
            out.append(value)
    return out


_RUNS_CACHE = {}


def runs(metrics, policy, episode, cb_family="legacy", excludes=()):
    """Run folders for a cell (--exclude honored; empty excludes = unchanged).

    legacy: *_rq2_<policy>_<episode>_[1-6] (unchanged).
    rq2pe: cb cf/sf/ba from era-matched *_rq2pe_<policy>_cb_<rep>[_r2]
    folders, deduplicated to the earliest folder per replicate label (a
    trailing _r2 is ignored; ignored spares are printed); nn keeps the
    legacy match.
    """
    key = (metrics, policy, episode, cb_family, tuple(excludes))
    if key in _RUNS_CACHE:
        return _RUNS_CACHE[key]
    out = []
    if cb_family == "rq2pe" and episode == "cb" and policy in ("cf", "sf", "ba"):
        for p in sorted(glob.glob(os.path.join(metrics, f"*_rq2pe_{policy}_cb_[1-6]*"))):
            name = os.path.basename(p)
            if os.path.isdir(p) and PE_RUN_RE.match(name) and not excluded(name, excludes):
                out.append((name, p))
        groups = {}
        for name, path in out:  # sorted: fixed-width timestamps => earliest first
            label = name.split("_", 2)[2]
            if label.endswith("_r2"):
                label = label[:-3]
            if label in groups:
                print(f"spare (ignored): {name}")
            else:
                groups[label] = (name, path)
        out = list(groups.values())
    else:
        for p in sorted(glob.glob(os.path.join(metrics, f"*_rq2_{policy}_{episode}_[1-6]"))):
            name = os.path.basename(p)
            if os.path.isdir(p) and RUN_RE.match(name) and not excluded(name, excludes):
                out.append((name, p))
    _RUNS_CACHE[key] = out
    return out


def era_matched_count(metrics):
    """Raw era-matched rq2pe folder count (exclusions not applied)."""
    n = 0
    for pol in ("cf", "sf", "ba"):
        for p in glob.glob(os.path.join(metrics, f"*_rq2pe_{pol}_cb_[1-6]*")):
            if os.path.isdir(p) and PE_RUN_RE.match(os.path.basename(p)):
                n += 1
    return n


def episode_lat(path, phase):
    """Completed-only episode latencies (both lanes pooled)."""
    out = []
    for f in run_files(path):
        with open(f, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                if (r.get("phase") or "").strip() != phase:
                    continue
                if (r.get("status") or "").strip() != "completed":
                    continue
                try:
                    out.append(float(r.get("latency_s") or "nan"))
                except ValueError:
                    pass
    return out


def first_compute_add(path):
    """lan -> first compute add epoch (node_lifecycle_timings.csv)."""
    out = {}
    p = os.path.join(path, "node_lifecycle_timings.csv")
    if not os.path.exists(p):
        return out
    with open(p, newline="", encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if (r.get("operation") or "").strip() != "add":
                continue
            if (r.get("node_type") or "").strip() != "compute":
                continue
            t = ts_el(r.get("timestamp"))
            lan = (r.get("controller") or "").strip()
            if t is None or not lan:
                continue
            if lan not in out or t < out[lan]:
                out[lan] = t
    return out


def engagement_stats(path):
    """Per-lane added/original request shares and per-backend CPU."""
    adds = first_compute_add(path)
    rows = []
    pns = os.path.join(path, "per_node_stats.csv")
    if not os.path.exists(pns):
        return {}
    with open(pns, newline="", encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if (r.get("phase") or "").strip() != "compute_bound_episode":
                continue
            if (r.get("role") or "").strip() != "compute":
                continue
            t = ts_iso(r.get("timestamp"))
            if t is None:
                continue
            try:
                cpu = float(r.get("cpu_percent") or "nan")
            except ValueError:
                cpu = float("nan")
            try:
                rc = int(float(r.get("request_count") or 0))
            except ValueError:
                rc = 0
            rows.append((t, (r.get("network_id") or "").strip(),
                         (r.get("server_id") or "").strip(), cpu, rc))
    out = {}
    for lan in sorted({r[1] for r in rows}):
        lr = [r for r in rows if r[1] == lan]
        add_t = adds.get(lan)
        srv = {}
        for t, _, sid, cpu, rc in lr:
            s = srv.setdefault(sid, dict(cpu=[], rc_post=0, first=1e18))
            s["cpu"].append(cpu)
            # without a compute add the whole episode is the reference window
            if add_t is None or t >= add_t:
                s["rc_post"] += rc
            s["first"] = min(s["first"], t)
        if add_t is None:
            orig, added = dict(srv), {}
        else:
            orig = {s: v for s, v in srv.items() if v["first"] < add_t - 10.0}
            added = {s: v for s, v in srv.items() if s not in orig}
        post_tot = sum(v["rc_post"] for v in srv.values())
        post_orig = sum(v["rc_post"] for v in orig.values())
        out[lan] = dict(
            nserv=len(srv), n_orig=len(orig), n_add=len(added),
            added_share_post=100.0 * (post_tot - post_orig) / post_tot if post_tot else None,
            orig_cpu=med([med(v["cpu"]) for v in orig.values()]),
            added_cpu=med([med(v["cpu"]) for v in added.values()]) if added else None,
            total_cpu=sum(med(v["cpu"]) for v in srv.values()),
        )
    return out


def relief_lane_ratios(path):
    """(lan1_ratio, lan2_ratio) from the run's pinned-window analysis CSV."""
    w = os.path.join(path, "analysis", "rq2_v3_windows.csv")
    if not os.path.exists(w):
        return None
    with open(w, newline="", encoding="utf-8", errors="replace") as fh:
        row = next(csv.DictReader(fh), None)
    if not row:
        return None
    out = []
    for lan in (1, 2):
        v = row.get(f"stor_{lan}_cpu_ratio")
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(None)
    return out


# --------------------------------------------------------------------------
# figure 1 - consequence map
# --------------------------------------------------------------------------
def fig_consequence_map(metrics, out_dir, data_rows, excludes=()):
    cells = {
        "db": ["nn_db", "cf_db", "sf_db", "ba_db"],
        "cb": ["nn_cb", "cf_cb", "sf_cb", "ba_cb"],
    }
    values = {}
    for ep, tags in cells.items():
        for tag in tags:
            pol, epsilon = tag.split("_")
            vals = []
            for name, path in runs(metrics, pol, epsilon, excludes=excludes):
                lats = episode_lat(path, EP_PHASE[epsilon])
                p50, p95 = pct(lats, .5), pct(lats, .95)
                vals.append(dict(run=name, p50=p50, p95=p95))
                data_rows.append(dict(figure="map", cell=tag, run=name,
                                      metric="ep_p50_s", value=p50))
                data_rows.append(dict(figure="map", cell=tag, run=name,
                                      metric="ep_p95_s", value=p95))
            values[tag] = vals

    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.6))
    x = range(4)
    for ax, ep, metric, ylab, log in (
            (axes[0], "db", "p95", "Episode latency p95 (s)", True),
            (axes[1], "cb", "p50", "Episode latency p50 (ms)", False)):
        tags = cells[ep]
        meds = []
        for i, tag in enumerate(tags):
            pol = tag.split("_")[0]
            v = [d[metric] for d in values[tag]
                 if d[metric] is not None and math.isfinite(d[metric])]
            if metric == "p50":
                v = [x_ * 1000 for x_ in v]
            m = med(v)
            meds.append(m)
            if m is None or not math.isfinite(m):
                print(f"warning: no data for {tag}; skipping")
                continue
            ax.bar(i, m, width=0.6, color=COLOR[pol], alpha=0.88)
            jit = [i + (j - (len(v) - 1) / 2.0) * 0.07 for j in range(len(v))]
            ax.scatter(jit, v, color="black", s=20, zorder=4, alpha=0.75)
        ax.set_xticks(list(x))
        ax.set_xticklabels([LABEL[t.split("_")[0]] for t in tags])
        ax.set_ylabel(ylab)
        if log:
            ax.set_yscale("log")
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        # cross-era label on the no-action comparator
        if meds[0] is not None and math.isfinite(meds[0]):
            ax.annotate("extension campaign\n(cross-era label)", xy=(0, meds[0]),
                        xytext=(0.0, 1.02), textcoords="axes fraction",
                        ha="left", va="bottom", fontsize=8.5, color="#666666")

    fig.tight_layout()
    out = os.path.join(out_dir, "rq2_consequence_map.png")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")
    for tag, vals in values.items():
        p95s = [d["p95"] for d in vals
                if d["p95"] is not None and math.isfinite(d["p95"])]
        p50s = [d["p50"] * 1000 for d in vals
                if d["p50"] is not None and math.isfinite(d["p50"])]
        if not p95s or not p50s:
            print(f"  {tag}: no data")
            continue
        print(f"  {tag}: n={len(vals)} p95 median={med(p95s):.3f}s "
              f"[{min(p95s):.3f}-{max(p95s):.3f}] p50 median={med(p50s):.2f}ms")


# --------------------------------------------------------------------------
# figure 2 - compute engagement
# --------------------------------------------------------------------------
def fig_compute_engagement(metrics, out_dir, data_rows, cb_family="legacy",
                           excludes=()):
    stats = {}
    for pol, ep in (("cf", "cb"), ("ba", "cb"), ("sf", "cb")):
        lane_rows = []
        for name, path in runs(metrics, pol, ep, cb_family=cb_family,
                               excludes=excludes):
            st = engagement_stats(path)
            for lan, s in sorted(st.items()):
                s = dict(s)
                s["run"] = name
                s["lan"] = lan
                lane_rows.append(s)
                data_rows.append(dict(figure="engagement", cell=f"{pol}_{ep}",
                                      run=name, metric=f"added_share_post_{lan}",
                                      value=s["added_share_post"]))
                data_rows.append(dict(figure="engagement", cell=f"{pol}_{ep}",
                                      run=name, metric=f"total_cpu_{lan}",
                                      value=s["total_cpu"]))
                data_rows.append(dict(figure="engagement", cell=f"{pol}_{ep}",
                                      run=name, metric=f"orig_cpu_{lan}",
                                      value=s["orig_cpu"]))
                data_rows.append(dict(figure="engagement", cell=f"{pol}_{ep}",
                                      run=name, metric=f"added_cpu_{lan}",
                                      value=s["added_cpu"]))
        stats[f"{pol}_{ep}"] = lane_rows

    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.6))
    groups = ["cf_cb", "ba_cb", "sf_cb"]
    # -- panel a: request share
    ax = axes[0]
    for i, tag in enumerate(groups):
        pol = tag.split("_")[0]
        rows_g = stats[tag]
        add_sh = [s["added_share_post"] for s in rows_g
                  if s["added_share_post"] is not None
                  and math.isfinite(s["added_share_post"])]
        m_add = med(add_sh)
        if m_add is None:
            print(f"warning: no data for {tag}; skipping")
            continue
        ax.bar(i, m_add, width=0.6, color=COLOR[pol], alpha=0.88)
        ax.bar(i, 100 - m_add, width=0.6, bottom=m_add, color=GREY, alpha=0.55)
        if add_sh and max(add_sh) > 0:
            jit = [i + (j - (len(add_sh) - 1) / 2.0) * 0.06 for j in range(len(add_sh))]
            ax.scatter(jit, add_sh, color="black", s=20, zorder=4, alpha=0.75)
        if m_add and m_add > 8:
            ax.text(i, m_add / 2, f"{m_add:.0f}%", ha="center", va="center",
                    fontsize=10, color="black", fontweight="bold")
        if m_add < 99:
            ax.text(i, m_add + (100 - m_add) / 2, f"{100 - m_add:.0f}%",
                    ha="center", va="center", fontsize=10, color="black")
        else:
            ax.text(i, 50, "not scaled", ha="center", va="center", fontsize=9.5,
                    color="#333333")
    ax.set_xticks(range(3))
    ax.set_xticklabels(["Compute-only", "Bottleneck-aware", "Storage-only"])
    ax.set_ylabel("Request share after first addition (%)")
    ax.set_ylim(0, 108)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # -- panel b: per-backend CPU (original infrastructure vs added backends)
    ax = axes[1]
    bw = 0.30
    for i, tag in enumerate(groups):
        pol = tag.split("_")[0]
        rows_g = stats[tag]
        orig_vals = [s["orig_cpu"] for s in rows_g
                     if s["orig_cpu"] is not None and math.isfinite(s["orig_cpu"])]
        added_vals = [s["added_cpu"] for s in rows_g
                      if s["added_cpu"] is not None and math.isfinite(s["added_cpu"])]
        xo = i if not added_vals else i - bw / 2.0
        if orig_vals:
            m = med(orig_vals)
            ax.bar(xo, m, width=bw, color=GREY, alpha=0.9)
            jit = [xo + (j - (len(orig_vals) - 1) / 2.0) * 0.05
                   for j in range(len(orig_vals))]
            ax.scatter(jit, orig_vals, color="black", s=20, zorder=4, alpha=0.75)
            ax.annotate(f"{m:.0f}", xy=(xo, m), xytext=(0, 4),
                        textcoords="offset points", ha="center", fontsize=9,
                        color="#333333")
        if added_vals:
            m = med(added_vals)
            xa = i + bw / 2.0
            ax.bar(xa, m, width=bw, color=COLOR[pol], alpha=0.88)
            jit = [xa + (j - (len(added_vals) - 1) / 2.0) * 0.05
                   for j in range(len(added_vals))]
            ax.scatter(jit, added_vals, color="black", s=20, zorder=4, alpha=0.75)
            ax.annotate(f"{m:.0f}", xy=(xa, m), xytext=(0, 4),
                        textcoords="offset points", ha="center", fontsize=9,
                        color="#333333")
    base = med([s["orig_cpu"] for s in stats["sf_cb"]
                if s["orig_cpu"] is not None and math.isfinite(s["orig_cpu"])])
    if base and math.isfinite(base):
        ax.axhline(base, color="#666666", linestyle="--", lw=1)
        ax.text(2.45, base + 2.5, "single-backend load", ha="right", va="bottom",
                fontsize=9, color="#666666")
    ax.set_xticks(range(3))
    ax.set_xticklabels(["Compute-only", "Bottleneck-aware", "Storage-only"])
    ax.set_ylabel("Per-backend CPU (%, medians)")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    handles = [Patch(facecolor=GREY, alpha=0.9, label="original infrastructure"),
               Patch(facecolor=COLOR["cf"], alpha=0.88, label="added backends")]
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False,
               fontsize=10)
    fig.tight_layout(rect=(0.0, 0.045, 1.0, 1.0))
    out = os.path.join(out_dir, "rq2_compute_engagement.png")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")
    for tag in groups:
        rows_g = stats[tag]
        add_sh = [s["added_share_post"] for s in rows_g
                  if s["added_share_post"] is not None
                  and math.isfinite(s["added_share_post"])]
        tot = med([s["total_cpu"] for s in rows_g
                   if s["total_cpu"] is not None and math.isfinite(s["total_cpu"])])
        if not add_sh or tot is None:
            print(f"  {tag}: no data")
            continue
        orig_m = med([s["orig_cpu"] for s in rows_g
                      if s["orig_cpu"] is not None and math.isfinite(s["orig_cpu"])])
        orig_s = f"{orig_m:.1f}" if orig_m is not None else "n/a"
        added_m = med([s["added_cpu"] for s in rows_g
                       if s["added_cpu"] is not None and math.isfinite(s["added_cpu"])])
        print(f"  {tag}: n_lanes={len(rows_g)} added_share med={med(add_sh):.1f} "
              f"[{min(add_sh):.1f}-{max(add_sh):.1f}] total med={tot:.1f} "
              f"orig med={orig_s} added med={added_m}")
    return base


# --------------------------------------------------------------------------
# figure 3 - storage relief (restyle)
# --------------------------------------------------------------------------
def fig_storage_relief(metrics, out_dir, data_rows, excludes=()):
    cells = ["sf_db", "ba_db", "cf_db"]
    per_run = {}
    for tag in cells:
        pol, ep = tag.split("_")
        per_run[tag] = []
        for name, path in runs(metrics, pol, ep, excludes=excludes):
            ratios = relief_lane_ratios(path)
            if ratios is None:
                continue
            good = [r for r in ratios if r is not None]
            mean_ratio = statistics.mean(good) if good else None
            per_run[tag].append(dict(run=name, lanes=ratios, mean=mean_ratio))
            for k, r in enumerate(ratios, 1):
                data_rows.append(dict(figure="relief", cell=tag, run=name,
                                      metric=f"lane{k}_ratio", value=r))

    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    for i, tag in enumerate(cells):
        pol = tag.split("_")[0]
        vals = [d["mean"] for d in per_run[tag] if d["mean"] is not None]
        if vals:
            m = statistics.mean(vals)
            s = statistics.stdev(vals) if len(vals) > 1 else 0.0
            ax.bar(i, m, width=0.6, color=COLOR[pol], alpha=0.88, yerr=s,
                   error_kw=dict(ecolor="black", lw=1, capsize=3))
            jit = [i + (j - (len(vals) - 1) / 2.0) * 0.07 for j in range(len(vals))]
            ax.scatter(jit, vals, color="black", s=22, zorder=4, alpha=0.8)
        else:
            ax.annotate("no reserve\nactivated", xy=(i, 0.35), ha="center",
                        fontsize=9.5, color="#666666")
    ax.axhline(0.75, color="#666666", linestyle="--", lw=1)
    ax.text(2.42, 0.75, "pre-registered threshold", ha="right", va="bottom",
            fontsize=9, color="#666666")
    ax.set_xticks(range(3))
    ax.set_xticklabels(["Storage-only", "Bottleneck-aware", "Compute-only"])
    ax.set_ylabel("Post/pre peak storage CPU (ratio)")
    ax.set_ylim(0, 1.0)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    out = os.path.join(out_dir, "rq2_storage_relief_v2.png")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")
    for tag in cells:
        vals = [d["mean"] for d in per_run[tag] if d["mean"] is not None]
        lanes_ok = [r for d in per_run[tag] for r in d["lanes"] if r is not None]
        if vals:
            print(f"  {tag}: n_reps={len(vals)} mean-of-lanes med={med(vals):.3f} "
                  f"[{min(vals):.3f}-{max(vals):.3f}] lanes_below_0.75="
                  f"{sum(1 for r in lanes_ok if r < 0.75)}/{len(lanes_ok)}")


# --------------------------------------------------------------------------
# figures 4-5 - episode latency at both metrics, one figure per regime
# --------------------------------------------------------------------------
def _latency_values(metrics, suite, cb_family="legacy", excludes=()):
    vals = {}
    for pol in ("nn", "cf", "sf", "ba"):
        tag = f"{pol}_{suite}"
        rows = []
        for name, path in runs(metrics, pol, suite, cb_family=cb_family,
                               excludes=excludes):
            lats = episode_lat(path, EP_PHASE[suite])
            rows.append((name, pct(lats, .5), pct(lats, .95)))
        vals[tag] = rows
    return vals


def _latency_panel(ax, vals, tags, metric, ylab, logscale, cross_era_label=True):
    meds = []
    for i, tag in enumerate(tags):
        pol = tag.split("_")[0]
        v = [(d[1] if metric == "p50" else d[2]) for d in vals[tag]]
        v = [x for x in v if x is not None]
        if metric == "p50":
            v = [x * 1000.0 for x in v]
        if not v:
            print(f"warning: no data for {tag}; skipping")
            meds.append(None)
            continue
        meds.append(med(v))
        ax.bar(i, meds[-1], width=0.6, color=COLOR[pol], alpha=0.88)
        jit = [i + (j - (len(v) - 1) / 2.0) * 0.07 for j in range(len(v))]
        ax.scatter(jit, v, color="black", s=20, zorder=4, alpha=0.75)
    ax.set_xticks(range(len(tags)))
    ax.set_xticklabels([LABEL[t.split("_")[0]] for t in tags])
    ax.set_ylabel(ylab)
    if logscale:
        ax.set_yscale("log")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if cross_era_label and meds[0] is not None:
        ax.annotate("extension campaign\n(cross-era label)", xy=(0, meds[0]),
                    xytext=(0.0, 1.02), textcoords="axes fraction",
                    ha="left", va="bottom", fontsize=8.5, color="#666666")
    return meds


def fig_latency_pairs(metrics, out_dir, data_rows, cb_family="legacy",
                      excludes=(), suites=("db", "cb"), cross_era_label=True):
    if cb_family == "rq2pe" and tuple(suites) != ("cb",):
        raise ValueError("rq2pe mode is compute-bound-only: suites must be ('cb',)")
    specs = {
        "db": ("rq2_latency_data_access.png",
               [("p95", True, "Episode latency p95 (s)"),
                ("p50", False, "Episode latency p50 (ms)")]),
        "cb": ("rq2_latency_compute_bound.png",
               [("p50", False, "Episode latency p50 (ms)"),
                ("p95", True, "Episode latency p95 (s)")]),
    }
    for suite in suites:
        fname, panels = specs[suite]
        tags = [f"{p}_{suite}" for p in ("nn", "cf", "sf", "ba")]
        vals = _latency_values(metrics, suite, cb_family=cb_family,
                               excludes=excludes)
        fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.6))
        for ax, (metric, logsc, ylab) in zip(axes, panels):
            meds = _latency_panel(ax, vals, tags, metric, ylab, logsc,
                                  cross_era_label=cross_era_label)
            print(f"[{suite}] {metric}: " + " | ".join(
                f"{t.split('_')[0]} med={meds[i]:.4g}" if meds[i] is not None
                else f"{t.split('_')[0]} med=n/a"
                for i, t in enumerate(tags)))
        for tag in tags:
            for name, p50, p95 in vals[tag]:
                data_rows.append(dict(figure=f"latency_{suite}", cell=tag,
                                      run=name, metric="ep_p50_s", value=p50))
                data_rows.append(dict(figure=f"latency_{suite}", cell=tag,
                                      run=name, metric="ep_p95_s", value=p95))
        fig.tight_layout()
        out = os.path.join(out_dir, fname)
        fig.savefig(out)
        plt.close(fig)
        print(f"wrote {out}")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metrics-dir",
                    default=os.path.expanduser(
                        "~/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--cb-family", choices=("legacy", "rq2pe"),
                    default="legacy",
                    help="compute-bound cell source: 'legacy' = v3-era "
                         "*_rq2_* folders (default, current output); 'rq2pe' "
                         "= era-matched *_rq2pe_<arm>_cb_* folders (cf/sf/ba) "
                         "with the legacy nn set, writing only the cb latency "
                         "panel + engagement figure and the era-matched CSV")
    ap.add_argument("--exclude", action="append", default=[],
                    metavar="FOLDER_OR_LABEL",
                    help="run folder name or label to drop (repeatable); a "
                         "folder matches by exact name or _<label> suffix; a "
                         "base label does not drop its _r2 sibling (use the "
                         "full folder name to disambiguate); empty/unmatched "
                         "values are reported")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    excludes = []
    empty_warned = False
    for value in args.exclude:
        value = value.strip()
        if not value:
            if not empty_warned:
                print("warning: ignoring empty --exclude value")
                empty_warned = True
            continue
        excludes.append(value)
    excludes = tuple(excludes)
    for value in unmatched_excludes(args.metrics_dir, excludes):
        print(f"warning: --exclude value not matched: {value}")
    data_rows = []

    if args.cb_family == "rq2pe":
        if not any(runs(args.metrics_dir, pol, "cb", cb_family="rq2pe",
                        excludes=excludes) for pol in ("cf", "sf", "ba")):
            raw = era_matched_count(args.metrics_dir)
            if raw:
                print(f"warning: {raw} era-matched rq2pe_* folder(s) "
                      "present but all excluded")
            elif not os.path.isdir(args.metrics_dir):
                print(f"warning: metrics dir not found: {args.metrics_dir}")
            else:
                print("warning: no era-matched rq2pe_* folders found")
            return
        fig_latency_pairs(args.metrics_dir, args.out_dir, data_rows,
                          cb_family="rq2pe", excludes=excludes,
                          suites=("cb",), cross_era_label=False)
        fig_compute_engagement(args.metrics_dir, args.out_dir, data_rows,
                               cb_family="rq2pe", excludes=excludes)
        csv_path = os.path.join(args.out_dir,
                                "rq2_thesis_figure_data_era_matched.csv")
    else:
        fig_consequence_map(args.metrics_dir, args.out_dir, data_rows,
                            excludes=excludes)
        fig_latency_pairs(args.metrics_dir, args.out_dir, data_rows,
                          excludes=excludes)
        fig_compute_engagement(args.metrics_dir, args.out_dir, data_rows,
                               excludes=excludes)
        fig_storage_relief(args.metrics_dir, args.out_dir, data_rows,
                           excludes=excludes)
        csv_path = os.path.join(args.out_dir, "rq2_thesis_figure_data.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["figure", "cell", "run", "metric", "value"])
        w.writeheader()
        for r in data_rows:
            w.writerow(r)
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
