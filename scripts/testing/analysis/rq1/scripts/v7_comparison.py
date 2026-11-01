"""v7 Test A — Push vs Poll-30s cross-mode comparison graphs."""
import csv, sys
from pathlib import Path
import numpy as np

from ...client_status import is_timeout_legacy

BASE = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics")
PUSH_DIRS = [
    BASE / "20260721_051833_rq1_v7_gap_push_1",
    # A2 (20260721_061925) excluded: 73% http_status=0 rate, anomalous
]
POLL_DIRS = [
    BASE / "20260721_064626_rq1_v7_gap_poll30_1",
    BASE / "20260721_073750_rq1_v7_gap_poll30_2",
]
OUT = Path("/home/testop/efficient-storage-in-edge-scenarios/docs/operation/testing/experiment/rq1_thesis_final/v7/graphs/comparison")
OUT.mkdir(parents=True, exist_ok=True)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("No matplotlib")
    sys.exit(1)

MODE_COLORS = ["#2196F3", "#F44336"]
MODE_LABELS = ["Push", "Poll-30s"]

def safe_read(path):
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))

def collect(mode_dirs):
    total_reqs = 0
    total_timeouts = 0
    phase_reqs = {}
    phase_fails = {}
    reaction_lats = []

    for d in mode_dirs:
        for row in safe_read(d / "client_requests.csv"):
            total_reqs += 1
            ph = row.get("phase", "unknown")
            phase_reqs[ph] = phase_reqs.get(ph, 0) + 1
            if is_timeout_legacy(row):
                total_timeouts += 1
                phase_fails[ph] = phase_fails.get(ph, 0) + 1

        for row in safe_read(d / "analysis" / "rq1_reaction_latency.csv"):
            reaction_lats.append(float(row.get("total_reaction_s", 0)))

    timeout_rate = (total_timeouts / total_reqs * 100) if total_reqs else 0
    return {
        "total_reqs": total_reqs,
        "total_timeouts": total_timeouts,
        "timeout_rate": timeout_rate,
        "phase_reqs": phase_reqs,
        "phase_fails": phase_fails,
        "reaction_lats": reaction_lats,
    }

def blind_spot_rate(mode_dirs):
    breached = 0
    blind = 0
    for d in mode_dirs:
        for row in safe_read(d / "analysis" / "rq1" / "rq1_blind_spot_windows.csv"):
            if row.get("breached") == "True":
                breached += 1
            if row.get("blind_spot") == "True":
                blind += 1
    rate = (blind / breached * 100) if breached > 0 else 0
    return rate, breached, blind

push = collect(PUSH_DIRS)
poll = collect(POLL_DIRS)
push_bsr, push_br, push_bl = blind_spot_rate(PUSH_DIRS)
poll_bsr, poll_br, poll_bl = blind_spot_rate(POLL_DIRS)

# Pre-format strings to avoid f-string backslash issues
push_reqs_str = f"{push['total_reqs']:,}"
push_to_str = f"{push['total_timeouts']:,}"
push_tr_str = f"{push['timeout_rate']:.1f}"
poll_reqs_str = f"{poll['total_reqs']:,}"
poll_to_str = f"{poll['total_timeouts']:,}"
poll_tr_str = f"{poll['timeout_rate']:.1f}"

print(f"Push: {push_reqs_str} reqs, {push_to_str} timeouts ({push_tr_str}%)")
print(f"Poll: {poll_reqs_str} reqs, {poll_to_str} timeouts ({poll_tr_str}%)")
print(f"Push blind spots: {push_bl}/{push_br} = {push_bsr:.1f}%")
print(f"Poll blind spots: {poll_bl}/{poll_br} = {poll_bsr:.1f}%")

KEY_PHASES = ["storage_storm", "tier1_hotspot", "reverse_hotspot", "compute_spike"]
FIG_SIZE = (10, 6)

# ── Graph 1: Throughput by Phase ──
fig, ax = plt.subplots(figsize=FIG_SIZE)
x = np.arange(len(KEY_PHASES))
w = 0.35
push_vals = [push["phase_reqs"].get(p, 0) for p in KEY_PHASES]
poll_vals = [poll["phase_reqs"].get(p, 0) for p in KEY_PHASES]
ax.bar(x - w/2, push_vals, w, label="Push", color=MODE_COLORS[0], edgecolor="black", alpha=0.75)
ax.bar(x + w/2, poll_vals, w, label="Poll-30s", color=MODE_COLORS[1], edgecolor="black", alpha=0.75)
for i, (pv, plv) in enumerate(zip(push_vals, poll_vals)):
    if pv > 0:
        gap = (pv - plv) / pv * 100
        ax.annotate(f"{gap:.0f}% gap", (i, max(pv, plv) + 2000),
                     ha="center", fontsize=9, fontweight="bold", color="#D32F2F")
ax.set_xticks(x)
ax.set_xticklabels([p.replace("_", "\n") for p in KEY_PHASES], fontsize=10)
ax.set_ylabel("Requests", fontsize=11)
ax.set_title("v7 Test A \u2014 Throughput by Phase: Push vs Poll-30s", fontsize=13, fontweight="bold")
ax.legend(fontsize=10)
ax.grid(axis="y", alpha=0.25, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
fig.savefig(OUT / "v7_throughput_by_phase.png", dpi=150)
plt.close(fig)
print(f"Wrote {OUT / 'v7_throughput_by_phase.png'}")

# ── Graph 2: Blind Spot Rate ──
fig, ax = plt.subplots(figsize=(6, 5))
x2 = np.arange(2)
ax.bar(x2, [push_bsr, poll_bsr], color=MODE_COLORS, edgecolor="black", alpha=0.75)
labels = [
    f"Push\n{push_bl}/{push_br} blind\n({push_bsr:.1f}%)",
    f"Poll-30s\n{poll_bl}/{poll_br} blind\n({poll_bsr:.1f}%)",
]
ax.set_xticks(x2)
ax.set_xticklabels(labels, fontsize=11)
ax.set_ylabel("Blind Spot Rate (%)", fontsize=11)
ax.set_title("v7 Test A \u2014 Blind Spot Rate: Push vs Poll-30s", fontsize=13, fontweight="bold")
ax.grid(axis="y", alpha=0.25, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.set_ylim(0, max(push_bsr, poll_bsr) * 1.3)
plt.tight_layout()
fig.savefig(OUT / "v7_blind_spot_rate.png", dpi=150)
plt.close(fig)
print(f"Wrote {OUT / 'v7_blind_spot_rate.png'}")

# ── Graph 3: Timeout Rate ──
fig, ax = plt.subplots(figsize=(6, 5))
ax.bar(x2, [push["timeout_rate"], poll["timeout_rate"]], color=MODE_COLORS, edgecolor="black", alpha=0.75)
tl = [
    f"Push\n{push_to_str} timeouts\n({push_tr_str}%)",
    f"Poll-30s\n{poll_to_str} timeouts\n({poll_tr_str}%)",
]
ax.set_xticks(x2)
ax.set_xticklabels(tl, fontsize=11)
ax.set_ylabel("Timeout Rate (%)", fontsize=11)
ax.set_title("v7 Test A \u2014 Curl Timeout Rate: Push vs Poll-30s", fontsize=13, fontweight="bold")
ax.grid(axis="y", alpha=0.25, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.set_ylim(0, max(push["timeout_rate"], poll["timeout_rate"]) * 1.3)
plt.tight_layout()
fig.savefig(OUT / "v7_timeout_rate.png", dpi=150)
plt.close(fig)
print(f"Wrote {OUT / 'v7_timeout_rate.png'}")

# ── Graph 4: Reaction Latency Distribution ──
fig, ax = plt.subplots(figsize=(8, 5))
all_lats = [push["reaction_lats"], poll["reaction_lats"]]
positions = [1, 2]
bp = ax.boxplot(all_lats, positions=positions, widths=0.4, patch_artist=True)
for patch, color in zip(bp["boxes"], MODE_COLORS):
    patch.set_facecolor(color)
    patch.set_alpha(0.6)
for i, lats in enumerate(all_lats):
    if lats:
        jitter = np.random.RandomState(42).uniform(-0.1, 0.1, len(lats))
        ax.scatter(np.full(len(lats), positions[i]) + jitter, lats,
                   color="black", s=30, zorder=5, alpha=0.6)
ax.set_xticks(positions)
ax.set_xticklabels(MODE_LABELS, fontsize=12)
ax.set_ylabel("Reaction Latency (s)", fontsize=11)
ax.set_title("v7 Test A \u2014 Reaction Latency Distribution", fontsize=13, fontweight="bold")
ax.grid(axis="y", alpha=0.25, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
fig.savefig(OUT / "v7_reaction_latency.png", dpi=150)
plt.close(fig)
print(f"Wrote {OUT / 'v7_reaction_latency.png'}")

# ── Graph 5: Per-Phase Failure Rate ──
fig, ax = plt.subplots(figsize=FIG_SIZE)
push_fail = []
poll_fail = []
for p in KEY_PHASES:
    pr = push["phase_reqs"].get(p, 0)
    pf = push["phase_fails"].get(p, 0)
    push_fail.append((pf / pr * 100) if pr else 0)
    pr2 = poll["phase_reqs"].get(p, 0)
    pf2 = poll["phase_fails"].get(p, 0)
    poll_fail.append((pf2 / pr2 * 100) if pr2 else 0)

ax.bar(x - w/2, push_fail, w, label="Push", color=MODE_COLORS[0], edgecolor="black", alpha=0.75)
ax.bar(x + w/2, poll_fail, w, label="Poll-30s", color=MODE_COLORS[1], edgecolor="black", alpha=0.75)
ax.set_xticks(x)
ax.set_xticklabels([p.replace("_", "\n") for p in KEY_PHASES], fontsize=10)
ax.set_ylabel("Failure Rate (%)", fontsize=11)
ax.set_title("v7 Test A \u2014 Per-Phase Curl Failure Rate", fontsize=13, fontweight="bold")
ax.legend(fontsize=10)
ax.grid(axis="y", alpha=0.25, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
fig.savefig(OUT / "v7_per_phase_failure_rate.png", dpi=150)
plt.close(fig)
print(f"Wrote {OUT / 'v7_per_phase_failure_rate.png'}")

print("ALL COMPARISON GRAPHS DONE")
