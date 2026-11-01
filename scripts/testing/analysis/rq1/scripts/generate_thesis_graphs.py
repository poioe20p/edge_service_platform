"""Unified RQ1 thesis graphs — matches comparison/ graph styling.

v4 4-mode data (n=3) + v5 Pilot B data (n=2).
Per-phase latency with explicit variance (min–max range).
"""

import csv
from pathlib import Path
import numpy as np
import matplotlib

from ...client_status import is_completed, is_failure
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MODE_COLORS_4 = ["#2196F3", "#4CAF50", "#FF9800", "#F44336"]
PUSH_C  = "#2196F3"
POLL_C  = "#F44336"
POLL5_C = "#4CAF50"
POLL12_C= "#FF9800"

FIG_W   = (9.5, 5.5)
TITLE_SZ = 13
LABEL_SZ = 12
TICK_SZ  = 11
ANNO_SZ  = 10
GRID_A   = 0.25
BAR_A    = 0.80

plt.rcParams.update({
    "font.size": LABEL_SZ, "axes.titlesize": TITLE_SZ, "axes.labelsize": LABEL_SZ,
    "xtick.labelsize": TICK_SZ, "ytick.labelsize": TICK_SZ,
    "legend.fontsize": 10, "figure.dpi": 150, "savefig.dpi": 200, "savefig.bbox": "tight",
})

BASE = Path(r"c:\Users\themo\Documents\Trabalhos Academicos\Mestrado - Tese\efficient-storage-in-edge-scenarios\source\scripts\testing\metrics")
OUT  = Path(r"c:\Users\themo\Documents\Trabalhos Academicos\Mestrado - Tese\efficient-storage-in-edge-scenarios\docs\operation\testing\experiment\rq1_thesis_final\graphs\thesis")
OUT.mkdir(parents=True, exist_ok=True)

V5_PUSH = [BASE / "20260720_142606_rq1_v5_pilotB_push_1",
           BASE / "20260720_152126_rq1_v5_pilotB_push_2"]
V5_POLL = [BASE / "20260720_160318_rq1_v5_pilotB_poll30_1",
           BASE / "20260720_164530_rq1_v5_pilotB_poll30_2"]

PHASES   = ["storage_storm","tier1_hotspot","reverse_hotspot","compute_spike"]
PH_LABELS= ["Storage Storm","Tier-1 Hotspot","Reverse Hotspot","Compute Spike"]
ENDPOINT = "service_pressure"

def csv_rows(p): 
    if not p.exists(): return []
    with open(p, newline="") as f: return list(csv.DictReader(f))

def per_run(dirs, fn): return [fn(d) for d in dirs]

def style_ax(ax, x, labels, ylabel, title):
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=TICK_SZ)
    ax.set_ylabel(ylabel, fontsize=LABEL_SZ)
    ax.set_title(title, fontsize=TITLE_SZ, fontweight="bold")
    ax.grid(axis="y", alpha=GRID_A, linestyle="--")
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)

def paired_bars(ax, x, p_means, pl_means, p_err=None, pl_err=None):
    BAR_W = 0.35
    b1 = ax.bar(x - BAR_W/2, p_means, BAR_W, label="Push", color=PUSH_C, alpha=BAR_A, edgecolor="white")
    b2 = ax.bar(x + BAR_W/2, pl_means, BAR_W, label="Poll-30s", color=POLL_C, alpha=BAR_A, edgecolor="white")
    if p_err:
        for i,(bar,e) in enumerate(zip(b1, p_err)):
            if e[0]!=e[1]:
                ax.errorbar(bar.get_x()+bar.get_width()/2, bar.get_height(),
                           yerr=[[bar.get_height()-e[0]],[e[1]-bar.get_height()]],
                           fmt="none", ecolor="#333", capsize=3, linewidth=1)
    if pl_err:
        for i,(bar,e) in enumerate(zip(b2, pl_err)):
            if e[0]!=e[1]:
                ax.errorbar(bar.get_x()+bar.get_width()/2, bar.get_height(),
                           yerr=[[bar.get_height()-e[0]],[e[1]-bar.get_height()]],
                           fmt="none", ecolor="#333", capsize=3, linewidth=1)
    return b1, b2

def min_max_range(vals_list):
    """Return [(min, max), ...] for each position across replicates."""
    return [(min(v), max(v)) for v in vals_list] if vals_list else []


def collect_phase_latency(dirs, metric="mean"):
    """Return dict: {phase: [run1_val, run2_val, ...]} for the given metric.
    
    Reads from rq1_endpoint_latency.csv which has: phase, endpoint, p50, mean, p95, p99, std, min, max, count.
    If metric not in CSV, computes from client_requests.csv directly.
    """
    result = {ph: [] for ph in PHASES}
    for d in dirs:
        ep_path = d / "analysis" / "rq1" / "rq1_endpoint_latency.csv"
        if metric in ["p50","mean","p95","p99","std","min","max","count"] and ep_path.exists():
            for r in csv_rows(ep_path):
                if r.get("phase") in PHASES and r.get("endpoint") == ENDPOINT:
                    result[r["phase"]].append(float(r.get(metric, 0)))
        else:
            cr_path = d / "client_requests.csv"
            if cr_path.exists():
                phase_lats = {ph: [] for ph in PHASES}
                for r in csv_rows(cr_path):
                    if not is_completed(r):
                        continue
                    ph = r.get("phase","")
                    if ph in PHASES and r.get("endpoint","") == ENDPOINT:
                        phase_lats[ph].append(float(r.get("latency_s", 0)))
                for ph in PHASES:
                    lats = phase_lats[ph]
                    if lats:
                        if metric == "mean": result[ph].append(np.mean(lats))
                        elif metric == "p50": result[ph].append(np.median(lats))
                        elif metric == "p95": result[ph].append(np.percentile(lats, 95))
                        elif metric == "p99": result[ph].append(np.percentile(lats, 99))
                        elif metric == "std": result[ph].append(np.std(lats))
                        elif metric == "min": result[ph].append(np.min(lats))
                        elif metric == "max": result[ph].append(np.max(lats))
    return result


def fig01():
    def rate(d):
        rows = csv_rows(d / "analysis" / "rq1" / "rq1_blind_spot_windows.csv")
        breached = sum(1 for r in rows if r.get("breached","")=="True")
        blind   = sum(1 for r in rows if r.get("blind_spot","")=="True")
        return (blind/breached*100) if breached>0 else 0
    p_v=per_run(V5_PUSH,rate); pl_v=per_run(V5_POLL,rate)
    fig,ax=plt.subplots(figsize=(7,5)); x=np.arange(1); BAR_W=0.35
    b1=ax.bar(x-BAR_W/2,[np.mean(p_v)],BAR_W,label="Push",color=PUSH_C,alpha=BAR_A,edgecolor="white")
    b2=ax.bar(x+BAR_W/2,[np.mean(pl_v)],BAR_W,label="Poll-30s",color=POLL_C,alpha=BAR_A,edgecolor="white")
    style_ax(ax,x,["Overload Windows\nUnseen by Controller"],"Blind Spot Rate (%)","RQ1 v5 — Telemetry Blind Spot")
    ax.legend(frameon=False); ax.set_ylim(0,max(np.mean(pl_v)*1.3,10))
    for bar,val in zip([b1[0],b2[0]],[np.mean(p_v),np.mean(pl_v)]):
        ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+1,f"{val:.0f}%",ha="center",fontweight="bold",fontsize=ANNO_SZ)
    fig.tight_layout(); fig.savefig(OUT/"fig01_blind_spot.png"); plt.close(fig)
    print(f"✓ fig01  blind_spot  Push={np.mean(p_v):.0f}%  Poll={np.mean(pl_v):.0f}%")


def fig02():
    def spawns(d):
        return sum(1 for r in csv_rows(d/"node_lifecycle_timings.csv")
                   if r.get("operation")=="add" and r.get("node_type")=="compute")
    p_v=per_run(V5_PUSH,spawns); pl_v=per_run(V5_POLL,spawns)
    fig,ax=plt.subplots(figsize=(7,5)); x=np.arange(1); BAR_W=0.35
    b1=ax.bar(x-BAR_W/2,[np.mean(p_v)],BAR_W,label="Push",color=PUSH_C,alpha=BAR_A,edgecolor="white")
    b2=ax.bar(x+BAR_W/2,[np.mean(pl_v)],BAR_W,label="Poll-30s",color=POLL_C,alpha=BAR_A,edgecolor="white")
    style_ax(ax,x,["Compute Nodes\nProvisioned"],"Count","RQ1 v5 — Dynamic Compute Provisioning")
    ax.legend(frameon=False); gap=(1-np.mean(pl_v)/np.mean(p_v))*100
    for bar,val in zip([b1[0],b2[0]],[np.mean(p_v),np.mean(pl_v)]):
        ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+0.3,f"{val:.0f}",ha="center",fontweight="bold",fontsize=ANNO_SZ)
    fig.tight_layout(); fig.savefig(OUT/"fig02_spawns.png"); plt.close(fig)
    print(f"✓ fig02  spawns  Push={np.mean(p_v):.1f}  Poll={np.mean(pl_v):.1f}  gap={gap:.0f}%")


def fig03():
    def ph_reqs(d,ph):
        for r in csv_rows(d/"latency_summary.csv"):
            if r.get("phase")==ph: return int(r["count"])
        return 0
    p_m=[np.mean([ph_reqs(d,ph) for d in V5_PUSH]) for ph in PHASES]
    pl_m=[np.mean([ph_reqs(d,ph) for d in V5_POLL]) for ph in PHASES]
    fig,ax=plt.subplots(figsize=FIG_W); x=np.arange(len(PHASES))
    paired_bars(ax,x,p_m,pl_m)
    style_ax(ax,x,PH_LABELS,"Completed Requests","RQ1 v5 — Throughput by Workload Phase")
    ax.legend(frameon=False)
    for i in range(len(PHASES)):
        gap=(1-pl_m[i]/p_m[i])*100 if p_m[i] else 0
        ax.annotate(f"−{gap:.0f}%",(x[i],max(p_m[i],pl_m[i])*1.04),ha="center",fontsize=9,fontweight="bold",color=POLL_C if gap>10 else "#888")
    fig.tight_layout(); fig.savefig(OUT/"fig03_throughput.png"); plt.close(fig)
    print("✓ fig03  throughput")


def fig04():
    """Per-phase mean latency on service_pressure with min–max error bars."""
    p_data = collect_phase_latency(V5_PUSH, "mean")
    pl_data = collect_phase_latency(V5_POLL, "mean")

    p_means = [np.mean(p_data[ph]) for ph in PHASES]
    pl_means = [np.mean(pl_data[ph]) for ph in PHASES]

    p_range = [(min(p_data[ph]), max(p_data[ph])) for ph in PHASES]
    pl_range = [(min(pl_data[ph]), max(pl_data[ph])) for ph in PHASES]

    fig, ax = plt.subplots(figsize=FIG_W)
    x = np.arange(len(PHASES))
    b1, b2 = paired_bars(ax, x, p_means, pl_means, p_err=p_range, pl_err=pl_range)
    style_ax(ax, x, PH_LABELS, "Mean Latency (s)",
             "RQ1 v5 — Mean Request Latency by Phase (service_pressure)")
    ax.legend(frameon=False)

    for i in range(len(PHASES)):
        ratio = pl_means[i] / p_means[i] if p_means[i] > 0 else 0
        color = POLL_C if ratio > 1.15 else "#666"
        ax.annotate(f"{ratio:.1f}×", (x[i], max(p_means[i], pl_means[i]) * 1.08),
                    ha="center", fontsize=9, fontweight="bold", color=color)
        ax.annotate(f"[{p_range[i][0]*1000:.0f}–{p_range[i][1]*1000:.0f} ms]",
                    (x[i] - 0.15, p_means[i] * 0.15), ha="center", fontsize=7, color=PUSH_C, rotation=90)
        ax.annotate(f"[{pl_range[i][0]*1000:.0f}–{pl_range[i][1]*1000:.0f} ms]",
                    (x[i] + 0.15, pl_means[i] * 0.15), ha="center", fontsize=7, color=POLL_C, rotation=90)

    ax.text(0.5, -0.12, "Error bars: min–max range across n=2 replicates",
            transform=ax.transAxes, ha="center", fontsize=8, style="italic", color="#888")
    fig.tight_layout(); fig.savefig(OUT / "fig04_latency.png"); plt.close(fig)
    print("✓ fig04  mean latency per phase with min–max range")


def fig05():
    """Per-phase latency std dev on service_pressure — explicit variance."""
    p_data = collect_phase_latency(V5_PUSH, "std")
    pl_data = collect_phase_latency(V5_POLL, "std")

    p_means = [np.mean(p_data[ph]) for ph in PHASES]
    pl_means = [np.mean(pl_data[ph]) for ph in PHASES]
    p_range = [(min(p_data[ph]), max(p_data[ph])) for ph in PHASES]
    pl_range = [(min(pl_data[ph]), max(pl_data[ph])) for ph in PHASES]

    fig, ax = plt.subplots(figsize=FIG_W)
    x = np.arange(len(PHASES))
    paired_bars(ax, x, p_means, pl_means, p_err=p_range, pl_err=pl_range)
    style_ax(ax, x, PH_LABELS, "Latency Std Dev (s)",
             "RQ1 v5 — Request Latency Variability by Phase (service_pressure)")
    ax.legend(frameon=False)

    for i in range(len(PHASES)):
        ratio = pl_means[i] / p_means[i] if p_means[i] > 0 else 0
        color = POLL_C if ratio > 1.15 else "#666"
        ax.annotate(f"{ratio:.1f}×", (x[i], max(p_means[i], pl_means[i]) * 1.08),
                    ha="center", fontsize=9, fontweight="bold", color=color)

    ax.text(0.5, -0.12, "Error bars: min–max range across n=2 replicates. Higher std dev = more variability in user experience.",
            transform=ax.transAxes, ha="center", fontsize=8, style="italic", color="#888")
    fig.tight_layout(); fig.savefig(OUT / "fig05_variance.png"); plt.close(fig)
    print("✓ fig05  latency std dev per phase")


def fig06():
    def fail_rate(d):
        rows=csv_rows(d/"client_requests.csv")
        if not rows: return 0
        total=len(rows); failed=sum(1 for r in rows if is_failure(r))
        return (failed/total)*100
    p_v=per_run(V5_PUSH,fail_rate); pl_v=per_run(V5_POLL,fail_rate)
    fig,ax=plt.subplots(figsize=(7,5)); x=np.arange(1); BAR_W=0.35
    b1=ax.bar(x-BAR_W/2,[np.mean(p_v)],BAR_W,label="Push",color=PUSH_C,alpha=BAR_A,edgecolor="white")
    b2=ax.bar(x+BAR_W/2,[np.mean(pl_v)],BAR_W,label="Poll-30s",color=POLL_C,alpha=BAR_A,edgecolor="white")
    p_lo, p_hi = min(p_v), max(p_v)
    pl_lo, pl_hi = min(pl_v), max(pl_v)
    ax.errorbar(x-BAR_W/2, np.mean(p_v), yerr=[[np.mean(p_v)-p_lo],[p_hi-np.mean(p_v)]],
                fmt="none", ecolor="#333", capsize=4, linewidth=1.2)
    ax.errorbar(x+BAR_W/2, np.mean(pl_v), yerr=[[np.mean(pl_v)-pl_lo],[pl_hi-np.mean(pl_v)]],
                fmt="none", ecolor="#333", capsize=4, linewidth=1.2)
    style_ax(ax,x,["Connection Failures\n(http_status = 0)"],"% of Requests","RQ1 v5 — Client-Observed Failures")
    ax.legend(frameon=False)
    ratio=np.mean(pl_v)/np.mean(p_v) if np.mean(p_v)>0 else 0
    for bar,val,lo,hi in zip([b1[0],b2[0]],[np.mean(p_v),np.mean(pl_v)],[p_lo,pl_lo],[p_hi,pl_hi]):
        ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+0.1,f"{val:.2f}%",ha="center",fontweight="bold",fontsize=ANNO_SZ)
    ax.text(0.5,-0.22,f"Error bars: min–max range (n=2). Poll/Push = {ratio:.1f}×.",
            transform=ax.transAxes,ha="center",fontsize=8,style="italic",color="#888")
    fig.tight_layout(); fig.savefig(OUT/"fig06_failures.png"); plt.close(fig)
    print(f"✓ fig06  failures  Push={np.mean(p_v):.2f}% [{p_lo:.2f}–{p_hi:.2f}]  Poll={np.mean(pl_v):.2f}% [{pl_lo:.2f}–{pl_hi:.2f}]  ratio={ratio:.1f}×")


def fig07():
    def cats(dirs):
        c={}
        for d in dirs:
            for r in csv_rows(d/"analysis"/"rq1"/"rq1_timeout_root_cause.csv"):
                c[r.get("category","unclassified")]=c.get(r.get("category","unclassified"),0)+1
        return c
    p_c=cats(V5_PUSH); pl_c=cats(V5_POLL)
    order=["storage_bound","transient_spike","unclassified"]
    names=["Storage Bound","Transient Spike","Unclassified"]
    clrs=["#E53935","#FF9800","#BDBDBD"]
    p_tot=sum(p_c.get(k,0) for k in order); pl_tot=sum(pl_c.get(k,0) for k in order)
    p_pct=[p_c.get(k,0)/p_tot*100 for k in order]; pl_pct=[pl_c.get(k,0)/pl_tot*100 for k in order]
    fig,ax=plt.subplots(figsize=(9,5.5)); x=np.arange(2); w=0.55; bp=0; bpl=0
    for i,(name,clr) in enumerate(zip(names,clrs)):
        ax.bar(0,p_pct[i],w,bottom=bp,label=name,color=clr,alpha=BAR_A,edgecolor="white")
        if p_pct[i]>5: ax.text(0,bp+p_pct[i]/2,f"{p_pct[i]:.0f}%",ha="center",va="center",fontsize=10,fontweight="bold",color="white")
        bp+=p_pct[i]
        ax.bar(1,pl_pct[i],w,bottom=bpl,color=clr,alpha=BAR_A,edgecolor="white")
        if pl_pct[i]>5: ax.text(1,bpl+pl_pct[i]/2,f"{pl_pct[i]:.0f}%",ha="center",va="center",fontsize=10,fontweight="bold",color="white")
        bpl+=pl_pct[i]
    ax.set_xticks([0,1])
    ax.set_xticklabels([f"Push (ZMQ)\n{p_tot:,} timeouts",f"Poll-30s\n{pl_tot:,} timeouts"])
    ax.set_ylabel("Proportion of Timeouts")
    ax.set_title("RQ1 v5 — Root Cause of Request Timeouts", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(frameon=False); ax.set_ylim(0,105)
    ax.grid(axis="y", alpha=GRID_A, linestyle="--"); ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT/"fig07_root_cause.png"); plt.close(fig)
    print("✓ fig07  root_cause")


def fig08():
    push_d=sorted(BASE.glob("*rq1_v4_push_*")); poll5_d=sorted(BASE.glob("*rq1_v4_poll5_*"))
    poll12_d=sorted(BASE.glob("*rq1_v4_poll12_*")); poll30_d=sorted(BASE.glob("*rq1_v4_poll30_*"))
    def max_s(d):
        rows=csv_rows(d/"analysis"/"rq1_staleness.csv")
        if not rows: return 0
        return max(float(r.get("staleness_s",0)) for r in rows)
    modes=[
        ("Push",    MODE_COLORS_4[0], [max_s(d) for d in push_d]),
        ("Poll-5s", MODE_COLORS_4[1], [max_s(d) for d in poll5_d]),
        ("Poll-12s",MODE_COLORS_4[2], [max_s(d) for d in poll12_d]),
        ("Poll-30s",MODE_COLORS_4[3], [max_s(d) for d in poll30_d]),
    ]
    fig,ax=plt.subplots(figsize=(9,5.5)); x=np.arange(len(modes))
    for i,(label,color,v) in enumerate(modes):
        mu=np.mean(v)
        ax.bar(i,mu,0.55,color=color,alpha=BAR_A,edgecolor="white")
        if len(v)>1:
            lo,hi=min(v),max(v)
            ax.errorbar(i,mu,yerr=[[mu-lo],[hi-mu]],fmt="none",ecolor="#333",capsize=4)
        ax.text(i,mu+0.15,f"{mu:.1f}s",ha="center",fontweight="bold",fontsize=ANNO_SZ)
    style_ax(ax,x,[m[0] for m in modes],"Information Age (s)","RQ1 v4 — Telemetry Freshness at Consumption")
    ax.text(0.5,-0.10,"Error bars: min–max range (n=3). All modes receive fresh data.",transform=ax.transAxes,ha="center",fontsize=8,style="italic",color="#888")
    fig.tight_layout(); fig.savefig(OUT/"fig08_staleness.png"); plt.close(fig)
    print("✓ fig08  staleness")


def fig09():
    push_d=sorted(BASE.glob("*rq1_v4_push_*")); poll5_d=sorted(BASE.glob("*rq1_v4_poll5_*"))
    poll12_d=sorted(BASE.glob("*rq1_v4_poll12_*")); poll30_d=sorted(BASE.glob("*rq1_v4_poll30_*"))
    def cpu_m(d):
        rows=csv_rows(d/"controller_stats.csv")
        if not rows: return 0
        return np.mean([float(r.get("cpu_percent",0) or 0) for r in rows])
    def ram_m(d):
        rows=csv_rows(d/"controller_stats.csv")
        if not rows: return 0
        return np.mean([float(r.get("mem_usage_mb",0) or 0) for r in rows])
    modes=[
        ("Push",MODE_COLORS_4[0],push_d),("Poll-5s",MODE_COLORS_4[1],poll5_d),
        ("Poll-12s",MODE_COLORS_4[2],poll12_d),("Poll-30s",MODE_COLORS_4[3],poll30_d),
    ]
    fig,axes=plt.subplots(1,2,figsize=(14,5.5)); names=[m[0] for m in modes]; colors=[m[1] for m in modes]; x=np.arange(len(names))
    cpu_mns=[np.mean([cpu_m(d) for d in m[2]]) for m in modes]; cpu_stds=[np.std([cpu_m(d) for d in m[2]]) for m in modes]
    ram_mns=[np.mean([ram_m(d) for d in m[2]]) for m in modes]; ram_stds=[np.std([ram_m(d) for d in m[2]]) for m in modes]
    for ax,vals,stds,ylab,ttl in [
        (axes[0],cpu_mns,cpu_stds,"CPU (%)","Controller CPU Usage"),
        (axes[1],ram_mns,ram_stds,"Memory (MB)","Controller Memory Usage")]:
        ax.bar(x,vals,0.55,color=colors,alpha=BAR_A,edgecolor="white")
        ax.errorbar(x,vals,yerr=stds,fmt="none",ecolor="#333",capsize=4)
        for i,v in enumerate(vals): ax.text(i,v+0.3 if ylab=="CPU (%)" else v+1,f"{v:.1f}%" if ylab=="CPU (%)" else f"{v:.0f} MB",ha="center",fontweight="bold",fontsize=ANNO_SZ)
        style_ax(ax,x,names,ylab,ttl)
    fig.suptitle("RQ1 v4 — Control-Plane Overhead by Telemetry Mode",fontsize=TITLE_SZ+1,fontweight="bold",y=1.02)
    fig.tight_layout(); fig.savefig(OUT/"fig09_overhead.png"); plt.close(fig)
    print("✓ fig09  overhead")


def fig10():
    push_d=sorted(BASE.glob("*rq1_v4_push_*")); poll5_d=sorted(BASE.glob("*rq1_v4_poll5_*"))
    poll12_d=sorted(BASE.glob("*rq1_v4_poll12_*")); poll30_v4=sorted(BASE.glob("*rq1_v4_poll30_*"))
    def blind_rate(d):
        rows=csv_rows(d/"analysis"/"rq1"/"rq1_blind_spot_windows.csv")
        breached=sum(1 for r in rows if r.get("breached","")=="True")
        blind=sum(1 for r in rows if r.get("blind_spot","")=="True")
        return (blind/breached*100) if breached>0 else 0
    all_modes=[
        ("Push\n(v4)",   MODE_COLORS_4[0], [blind_rate(d) for d in push_d]),
        ("Poll-5s\n(v4)", MODE_COLORS_4[1], [blind_rate(d) for d in poll5_d]),
        ("Poll-12s\n(v4)",MODE_COLORS_4[2], [blind_rate(d) for d in poll12_d]),
        ("Poll-30s\n(v4)",MODE_COLORS_4[3], [blind_rate(d) for d in poll30_v4]),
        ("Push\n(v5)",   PUSH_C,           [blind_rate(d) for d in V5_PUSH]),
        ("Poll-30s\n(v5)",POLL_C,           [blind_rate(d) for d in V5_POLL]),
    ]
    fig,ax=plt.subplots(figsize=(10,5.5)); x=np.arange(len(all_modes))
    for i,(label,color,vals) in enumerate(all_modes):
        mu=np.mean(vals)
        ax.bar(i,mu,0.55,color=color,alpha=BAR_A,edgecolor="white")
        ax.text(i,mu+1.5,f"{mu:.0f}%",ha="center",fontweight="bold",fontsize=ANNO_SZ)
        if len(vals)>1:
            lo,hi=min(vals),max(vals)
            ax.errorbar(i,mu,yerr=[[mu-lo],[hi-mu]],fmt="none",ecolor="#333",capsize=4)
    style_ax(ax,x,[l for l,_,_ in all_modes],"Blind Spot Rate (%)",
             "RQ1 — Decision Quality: Blind Spot Rate (v4 48 clients, n=3 | v5 96 clients, n=2)")
    ax.axvline(x=3.5,color="#999",linestyle="--",linewidth=1,alpha=0.5)
    ymax=ax.get_ylim()[1]
    ax.text(1.5,ymax*0.95,"v4 (48 clients)",ha="center",fontsize=9,color="#666")
    ax.text(4.5,ymax*0.95,"v5 (96 clients)",ha="center",fontsize=9,color="#666")
    ax.text(0.5,-0.12,"Error bars: min–max range across replicates.",transform=ax.transAxes,ha="center",fontsize=8,style="italic",color="#888")
    fig.tight_layout(); fig.savefig(OUT/"fig10_decision_quality.png"); plt.close(fig)
    print("✓ fig10  decision_quality")


if __name__ == "__main__":
    print(f"RQ1 thesis graphs → {OUT}\n")
    fig01(); fig02(); fig03(); fig04(); fig05(); fig06()
    fig07(); fig08(); fig09(); fig10()
    print(f"\n✓ {len(list(OUT.glob('fig*.png')))} graphs ready.")
