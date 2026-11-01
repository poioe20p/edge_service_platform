#!/usr/bin/env python3
"""Build rq2v5_cell_stats.csv — one row per completed RQ2 v5 campaign run.

Sources (all inside the run folder): window_log_lanX.jsonl (episode medians),
elasticity_events.csv (admissions), controller_lanX.log (broadcasts, band
picks, culls), client_requests_lanX_client_*.csv (episode completion + latency).

usage: rq2v5_cell_stats.py [--metrics DIR] [--status CSV] [--out CSV] [--limit N]
"""
import argparse
import csv
import glob
import json
import os
import statistics
import subprocess
import sys

EPISODE_PHASE = {"db": "data_bound_episode", "mx": "mixed_episode"}
EPISODE_MIN_REQ = {"db": 200, "mx": 100}

FIELDS = [
    "label", "mode", "regime", "seed", "ec", "sc", "run_ts", "rc", "run_completed",
    "oom_lines", "culls",
    "lan1_t_db_ms", "lan2_t_db_ms", "lan1_t_proc_ms", "lan2_t_proc_ms",
    "lan1_members", "lan2_members", "lan1_sto_cpu", "lan2_sto_cpu",
    "lan1_adds_storage", "lan2_adds_storage", "lan1_adds_compute", "lan2_adds_compute", "lan1_bcast", "lan2_bcast",
    "lan1_band", "lan2_band", "lan1_spread_band", "lan2_spread_band",
    "lan1_completion_pct", "lan2_completion_pct",
    "lan1_p50_s", "lan2_p50_s", "lan1_p95_s", "lan2_p95_s",
    "ep_windows_lan1", "ep_windows_lan2",
]


def med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else ""


def window_stats(run_dir, lane, regime):
    path = os.path.join(run_dir, f"window_log_lan{lane}.jsonl")
    if not os.path.exists(path):
        return {}
    ds = []
    for line in open(path):
        try:
            w = json.loads(line)
        except json.JSONDecodeError:
            continue
        if w.get("domain_summary"):
            ds.append(w)
    thr = EPISODE_MIN_REQ[regime]
    ep = [w for w in ds if (w["domain_summary"].get("total_requests") or 0) >= thr]
    d = {}
    d[f"lan{lane}_t_db_ms"] = med([w["domain_summary"].get("avg_time_db_ms") for w in ep])
    d[f"lan{lane}_t_proc_ms"] = med([w["domain_summary"].get("avg_time_proc_ms") for w in ep])
    d[f"lan{lane}_members"] = med([len(w.get("storage_servers") or {}) for w in ep])
    d[f"lan{lane}_sto_cpu"] = med([w["domain_summary"].get("avg_storage_cpu_percent") for w in ep])
    d[f"ep_windows_lan{lane}"] = len(ep)
    return d


def elastic_stats(run_dir, lane):
    """Split node_add_timing by tier.

    The tier lives in the container name (`edge_storage_lanX_dynN` = storage
    member, `edge_server_lanX_dynN` = compute node); the `node_type` column is
    empty on add events, so filtering on it silently yields zero for both.
    """
    path = os.path.join(run_dir, "elasticity_events.csv")
    sto = com = 0
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                if row.get("controller") != f"lan{lane}" or row.get("event_type") != "node_add_timing":
                    continue
                c = row.get("container") or ""
                if c.startswith("edge_storage"):
                    sto += 1
                elif c.startswith("edge_server"):
                    com += 1
    return {f"lan{lane}_adds_storage": sto, f"lan{lane}_adds_compute": com}


def controller_stats(run_dir, lane):
    log = os.path.join(run_dir, f"controller_lan{lane}.log")
    empty = {f"lan{lane}_bcast": "", f"lan{lane}_band": "", f"lan{lane}_spread_band": ""}
    if not os.path.exists(log):
        return empty
    def count(token):
        out = subprocess.run(["grep", "-c", "-F", token, log], capture_output=True, text=True)
        try:
            return int(out.stdout.strip() or 0)
        except ValueError:
            return 0
    return {
        f"lan{lane}_bcast": count("[pool_renew] broadcast"),
        f"lan{lane}_band": count("storage_band="),
        f"lan{lane}_spread_band": count("spread band="),
    }


def client_stats(run_dir, lane, regime):
    phase = EPISODE_PHASE[regime]
    ok = tot = 0
    lat = []
    for fn in glob.glob(os.path.join(run_dir, f"client_requests_lan{lane}_client_*.csv")):
        with open(fn) as f:
            for line in f:
                p = line.split(",")
                if len(p) > 9 and p[1] == phase:
                    tot += 1
                    if p[8] == "200":
                        ok += 1
                        try:
                            lat.append(float(p[9]))
                        except ValueError:
                            pass
    lat.sort()
    def q(frac):
        return round(lat[int(len(lat) * frac)], 3) if lat else ""
    return {
        f"lan{lane}_completion_pct": round(100.0 * ok / tot, 1) if tot else "",
        f"lan{lane}_p50_s": q(0.50),
        f"lan{lane}_p95_s": q(0.95),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", default=os.path.expanduser(
        "~/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"))
    ap.add_argument("--status", default=os.path.expanduser("~/rq2v5_battery/rq2v5_run_status.csv"))
    ap.add_argument("--out", default=os.path.expanduser("~/rq2v5_battery/rq2v5_cell_stats.csv"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.status)))
    rows = [r for r in rows if "pilot" not in r["label"] and r["run_completed"] == "yes"]
    if args.limit:
        rows = rows[: args.limit]

    out_rows = []
    for i, r in enumerate(rows, 1):
        regime = r["regime"]
        folders = sorted(glob.glob(os.path.join(args.metrics, f"*_{r['label']}")))
        if not folders:
            print(f"  !! no folder for {r['label']}", file=sys.stderr)
            continue
        run_dir = folders[-1]
        d = {
            "label": r["label"], "mode": r["mode"], "regime": regime, "seed": r["seed"],
            "ec": r["ec"], "sc": r["sc"], "run_ts": r["ts_utc"], "rc": r["rc"],
            "run_completed": r["run_completed"], "oom_lines": r["oom_lines"], "culls": r["culls"],
        }
        for lane in (1, 2):
            d.update(window_stats(run_dir, lane, regime))
            d.update(elastic_stats(run_dir, lane))
            d.update(controller_stats(run_dir, lane))
            d.update(client_stats(run_dir, lane, regime))
        out_rows.append(d)
        print(f"  [{i}/{len(rows)}] {r['label']}", file=sys.stderr)

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for d in out_rows:
            w.writerow({k: d.get(k, "") for k in FIELDS})
    print(f"wrote {args.out} rows={len(out_rows)}")


if __name__ == "__main__":
    main()
