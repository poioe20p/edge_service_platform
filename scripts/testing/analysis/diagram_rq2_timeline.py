"""
RQ2 Traffic-Share Evolution — Thesis-grade.
Conceptual diagram showing how a newly spawned backend's share of
client traffic evolves under three BACKEND_SELECTION_POLICY modes.
This captures the essence of RQ2: routing-plane awareness timing →
load redistribution quality.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch
import numpy as np

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif"],
    "font.size": 11,
    "text.usetex": False,
    "figure.dpi": 200,
    "savefig.dpi": 200,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.1,
})

# ── Colours ─────────────────────────────────────────────
C_HOST   = "#F44336"
C_SS     = "#FF9800"
C_TL     = "#2196F3"
C_GAP    = "#B71C1C"
C_LEASE  = "#0D47A1"
C_TEXT   = "#333333"
C_LIGHT  = "#888888"
C_GRID   = "#E0E0E0"
C_BG     = "#FAFAFA"

# ── Figure ──────────────────────────────────────────────
fig, ax = plt.subplots(1, 1, figsize=(14, 7.5))
ax.set_xlim(-3, 90)
ax.set_ylim(-5, 108)
ax.set_facecolor("white")
fig.patch.set_facecolor("white")

# ── Grid ────────────────────────────────────────────────
for t in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]:
    ax.axvline(x=t, color=C_GRID, linewidth=0.5, zorder=0)
for s in [0, 25, 50, 75, 100]:
    ax.axhline(y=s, color=C_GRID, linewidth=0.5, zorder=0)

# ── Axis labels ─────────────────────────────────────────
ax.set_xlabel("Time since spawn (seconds)", fontsize=11, color=C_TEXT,
              labelpad=8)
ax.set_ylabel("New backend traffic share (%)", fontsize=11, color=C_TEXT,
              labelpad=8)
ax.tick_params(axis="both", labelsize=9.5, colors=C_LIGHT)
ax.set_xticks([0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
ax.set_yticks([0, 25, 50, 75, 100])

# Clean spines
for spine in ax.spines.values():
    spine.set_visible(False)
ax.tick_params(left=False, bottom=False)

# ── Title ───────────────────────────────────────────────
ax.text(42, 112, "Traffic-Share Evolution After a New Backend Spawn",
        fontsize=15, fontweight="bold", color=C_TEXT, ha="center", va="top",
        transform=ax.transData)
ax.text(42, 105.5, "How each BACKEND_SELECTION_POLICY mode shapes the transition of load to a freshly spawned edge node",
        fontsize=9, color=C_LIGHT, ha="center", va="top", style="italic",
        transform=ax.transData)

# ────────────────────────────────────────────────────────
# CURVE 1: topology_host — no ramp, cold-start latency penalty
# ────────────────────────────────────────────────────────
t_host = np.array([0, 2, 5, 10, 15, 20, 30, 40, 50, 60, 75, 90])
s_host = np.array([0, 8, 12, 20, 25, 28, 31, 33, 30, 31, 29, 30])
# Gradual rise to ~30 % fair share — no prioritisation, competes equally in WSM
ax.plot(t_host, s_host, color=C_HOST, linewidth=2.8, alpha=0.85, zorder=4)
ax.fill_between(t_host, s_host - 2, s_host + 2, color=C_HOST, alpha=0.10)

# Annotations
ax.annotate("Round-robin fair share (~30%):\ntraffic arrives immediately but\ndiluted across all backends;\nbackend never gets enough\nconcentrated load to warm up fast",
            xy=(8, 17), xytext=(22, 38),
            fontsize=7.8, color=C_HOST, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=C_HOST, lw=1.5,
                            connectionstyle="arc3,rad=0.15"),
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=C_HOST, alpha=0.85),
            ha="center", va="center", zorder=6, linespacing=1.2)

ax.annotate("Mismatch: traffic at t\u22480,\nbackend warm at t\u22485\u201310 s\n\u2192 elevated latency in\nnon-stress phases",
            xy=(30, 31), xytext=(55, 44),
            fontsize=7.3, color=C_HOST, style="italic",
            arrowprops=dict(arrowstyle="->", color=C_HOST, lw=1.2,
                            connectionstyle="arc3,rad=-0.1", alpha=0.7),
            ha="center", va="center", zorder=6, linespacing=1.2)

# ────────────────────────────────────────────────────────
# CURVE 2: topology_slowstart — discovery gap + graduated ramp
# ────────────────────────────────────────────────────────
t_ss = np.array([0, 5,   8,  10,   12,  15,   20,  25,  30,  40,  50,  60, 75, 90])
s_ss = np.array([0, 0,   0,  2,    8,   18,   35,  50,  60,  65,  66,  64, 65, 65])
# Flat at 0, then sigmoid ramp after discovery
ax.plot(t_ss, s_ss, color=C_SS, linewidth=2.8, alpha=0.85, zorder=4)
ax.fill_between(t_ss, s_ss - 2, s_ss + 2, color=C_SS, alpha=0.08)

# Invisible period shaded
ax.axvspan(0, 10, facecolor=C_GAP, alpha=0.10, zorder=1)
ax.text(5, 8, "Invisible\n(penalty 1.0)\n\u2192 backend warms\n  up undisturbed", fontsize=7.2, color=C_GAP,
        ha="center", va="bottom", fontweight="bold", linespacing=1.15, zorder=5)

# Discovery marker
ax.axvline(x=10, color=C_SS, linewidth=1.2, linestyle="--", alpha=0.7, zorder=2)
ax.annotate("Telemetry\ndiscovery\n(~10 s)",
            xy=(10, 2), xytext=(17, -2),
            fontsize=7.8, color=C_SS, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=C_SS, lw=1.5,
                            connectionstyle="arc3,rad=0.2"),
            ha="center", va="top", zorder=6, linespacing=1.15)

# Graduated ramp shaded
ax.axvspan(10, 28, facecolor=C_SS, alpha=0.09, zorder=1)
ax.annotate("Graduated\npenalty ramp\n(10→28 s)",
            xy=(19, 30), xytext=(32, 38),
            fontsize=7.8, color=C_SS, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=C_SS, lw=1.5,
                            connectionstyle="arc3,rad=-0.15"),
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=C_SS, alpha=0.85),
            ha="center", va="center", zorder=6, linespacing=1.15)

# ────────────────────────────────────────────────────────
# CURVE 3: topology_lifecycle — warm lease + expiry dip
# ────────────────────────────────────────────────────────
t_tl = np.array([0, 1,   2,  5,  10,  20,  30,   38,  42,    45,  50,  60, 75, 90])
s_tl = np.array([0, 35,  55, 70,  75,  78,  80,   82,  80,    68,  64,  65, 64, 65])
# Fast ramp via warm lease, slight dip after lease expiry at t≈42
ax.plot(t_tl, s_tl, color=C_TL, linewidth=2.8, alpha=0.85, zorder=4)
ax.fill_between(t_tl, s_tl - 2, s_tl + 2, color=C_TL, alpha=0.08)

# Warm-lease window shaded
ax.axvspan(0, 42, facecolor=C_TL, alpha=0.09, zorder=1)
ax.annotate("Warm-lease window (0\u201342 s):\n100% traffic concentration\naccelerates cache warming\n\u2192 matches slowstart latency",
            xy=(20, 78), xytext=(38, 92),
            fontsize=7.8, color=C_TL, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=C_TL, lw=1.5,
                            connectionstyle="arc3,rad=-0.2"),
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=C_TL, alpha=0.85),
            ha="center", va="center", zorder=6, linespacing=1.15)

# Lease expiry marker
ax.axvline(x=42, color=C_LEASE, linewidth=1.2, linestyle="--", alpha=0.7, zorder=2)
ax.annotate("Lease expires\n(~42 s)\n→ WSM rebalance",
            xy=(42, 80), xytext=(52, 74),
            fontsize=7.8, color=C_LEASE, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=C_LEASE, lw=1.5,
                            connectionstyle="arc3,rad=0.2"),
            ha="left", va="center", zorder=6, linespacing=1.15)

# Post-lease dip annotation
ax.annotate("Post-lease\ndip & rebalance",
            xy=(47, 66), xytext=(60, 55),
            fontsize=7.3, color=C_TL, style="italic",
            arrowprops=dict(arrowstyle="->", color=C_TL, lw=1.2,
                            connectionstyle="arc3,rad=0.15", alpha=0.7),
            ha="center", va="center", zorder=6, linespacing=1.15)

# ── Backend warm-up window (shared across all modes) ────
ax.axvspan(0, 10, ymin=0.0, ymax=0.045, facecolor="#795548", alpha=0.25, zorder=1)
ax.text(5, -1.0, "Backend warm-up\n(~0\u201310 s)", fontsize=7.5, color="#795548",
        ha="center", va="top", fontweight="bold", linespacing=1.15, zorder=5)

# ── Spawn marker (t=0 for all) ──────────────────────────
ax.axvline(x=0, color="#333333", linewidth=1.5, linestyle="-", alpha=0.5, zorder=2)
ax.text(0, 101.5, "Backend\nspawn  ", fontsize=8, color="#333333", fontweight="bold",
        ha="center", va="bottom", linespacing=1.1, zorder=6,
        bbox=dict(boxstyle="round,pad=0.25", facecolor="white",
                  edgecolor="#BBBBBB", alpha=0.85))

# ── Legend ──────────────────────────────────────────────
legend_handles = [
    mpatches.Patch(facecolor=C_HOST, alpha=0.7, label="topology_host (no ramp) — cold-start latency"),
    mpatches.Patch(facecolor=C_SS, alpha=0.7, label="topology_slowstart (discovery delay) — graduated ramp"),
    mpatches.Patch(facecolor=C_TL, alpha=0.7, label="topology_lifecycle (warm lease) — immediate priority"),
]
leg = ax.legend(handles=legend_handles, loc="upper right", fontsize=7.5,
                framealpha=0.9, edgecolor="#DDDDDD",
                bbox_to_anchor=(1.0, 1.01), ncol=1)
leg.set_zorder(10)

# ── Results annotations (per curve, right side) ─────────
results = [
    (80, 30, C_HOST, "TTFT: ~51 s\nInitial share: 29.6 %\np50 latency: 317 ms"),
    (80, 58, C_SS,   "TTFT: ~71 s\nInitial share: 54.8 %\np50 latency: 140 ms"),
    (80, 72, C_TL,   "TTFT: ~40 s\nInitial share: 72.8 %\np50 latency: 144 ms"),
]
for rx, ry, rc, rtext in results:
    ax.text(rx, ry, rtext, fontsize=7.5, color=rc, fontweight="bold",
            ha="left", va="center", linespacing=1.25, zorder=8,
            bbox=dict(boxstyle="round,pad=0.35", facecolor="white",
                      edgecolor=rc, alpha=0.88, linewidth=1.0))

# ── Footer ──────────────────────────────────────────────
ax.text(42, -3.5, "Conceptual curves annotated with measured outcomes from 9-run RQ2 campaign "
        "(3 modes \u00d7 3 replicates).  CLIENTS=32, RANDOM_SEED=42, rate=4.0.  July 2026.",
        fontsize=7, color="#AAAAAA", ha="center", va="top", style="italic", zorder=8)

# ═══════════════════════════════════════════════════════
OUT_DIR = "c:/Users/themo/Documents/Trabalhos Academicos/Mestrado - Tese/efficient-storage-in-edge-scenarios/docs/diagrams/rq2"
import os
os.makedirs(OUT_DIR, exist_ok=True)
path = os.path.join(OUT_DIR, "rq2_modes_timeline.png")
fig.savefig(path, dpi=200, facecolor="white", edgecolor="none")
print("Saved " + path)
plt.close(fig)

