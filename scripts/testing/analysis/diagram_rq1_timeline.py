"""
RQ1 Telemetry Modes Timeline — Thesis-grade.
Shows how push vs poll modes differ in information delivery,
staleness, and reaction latency.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle
import numpy as np

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif"],
    "font.size": 10,
    "text.usetex": False,
    "figure.dpi": 200,
    "savefig.dpi": 200,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.1,
})

C_PUSH    = "#2D5A3D"   # muted dark green — push
C_PUSH_BG = "#86EFA3"
C_POLL5   = "#3A5F7A"
C_POLL5_BG= "#7CBCFC"
C_POLL12  = "#7A5A3A"
C_POLL12_BG="#F0C16E"
C_POLL30  = "#7A3A3A"
C_POLL30_BG="#FB8B8B"
C_EVENT   = "#AA4444"   # muted red — breach event marker
C_SPAWN   = "#4A7A4A"   # muted green — spawn event marker
C_LINE    = "#555555"
C_LIGHT   = "#999999"

MODES = [
    ("Push",       C_PUSH,    C_PUSH_BG,   "0 s",       "~0 s",        "~33 s",  "5.9%"),
    ("Poll 5 s",   C_POLL5,   C_POLL5_BG,  "Every 5 s", "~5 s",        "~40 s",  "8.1%"),
    ("Poll 12 s",  C_POLL12,  C_POLL12_BG, "Every 12 s","~10 s*",      "~48 s",  "14.7%"),
    ("Poll 30 s",  C_POLL30,  C_POLL30_BG, "Every 30 s","~10 s*",      "~76 s",  "25.5%"),
]

fig, axes = plt.subplots(5, 1, figsize=(14, 7.5),
                          gridspec_kw={"height_ratios": [1.2, 2.2, 2.2, 2.2, 2.2]})
fig.patch.set_facecolor("white")

# ═══════════════════════════════════════════════════════════
# TOP PANEL — Title + Legend
# ═══════════════════════════════════════════════════════════
ax0 = axes[0]
ax0.set_xlim(0, 14)
ax0.set_ylim(0, 1.5)
ax0.axis("off")
ax0.text(7, 1.35, "Telemetry Delivery Cadence & Its Impact on Elasticity",
         fontsize=15, fontweight="bold", color="#333333", ha="center", va="top")
ax0.text(7, 0.85, "How each mode delivers information to the controller, and the resulting staleness, reaction latency, and service quality",
         fontsize=9, color="#777777", ha="center", va="top", style="italic")

# Legend
legend_items = [
    ("D  Breach event (load spike)", C_EVENT),
    ("^  Controller learns of event", "#333333"),
    ("p  Elasticity spawn", C_SPAWN),
    ("-- Staleness gap", C_LIGHT),
    (".. Reaction latency", C_LINE),
]
for i, (label, color) in enumerate(legend_items):
    x = 1.2 + i * 2.8
    ax0.text(x, 0.3, label, fontsize=7.8, color=color, ha="left", va="center")

# ═══════════════════════════════════════════════════════════
# TIMELINE PANELS (4 modes)
# ═══════════════════════════════════════════════════════════
T = np.linspace(0, 60, 300)  # 60-second timeline

# Breach event at t=5s
BREACH_T = 5.0

for mode_idx, (name, color, bg_color, poll_label, staleness, reaction, failure) in enumerate(MODES):
    ax = axes[mode_idx + 1]
    ax.set_xlim(0, 60)
    ax.set_ylim(-1.5, 3.8)
    ax.set_facecolor("#FAFAFA")

    # Mode label on left
    ax.text(-0.5, 1.9, name, fontsize=10.5, fontweight="bold",
            color=color, ha="left", va="center")

    # Background band for mode
    ax.axhspan(-1.5, 3.8, facecolor=bg_color, alpha=0.10, zorder=0)

    # ── Time axis ──
    ax.axhline(y=0, color="#777777", linewidth=1.0, zorder=1)
    for t in [0, 10, 20, 30, 40, 50, 60]:
        ax.axvline(x=t, color="#CCCCCC", linewidth=0.5, zorder=0)
        if t < 60:
            ax.text(t, -0.35, f"{t}s", fontsize=7, color="#AAAAAA", ha="center")

    # ── Breach event marker (same for all modes) ──
    ax.plot(BREACH_T, 0.5, marker="D", color=C_EVENT, markersize=9, zorder=5,
            markeredgecolor="white", markeredgewidth=1)
    ax.text(BREACH_T, 0.85, "Load spike\n(breach)", fontsize=7.2, color=C_EVENT,
            ha="center", va="bottom", fontweight="bold")

    # ── Mode-specific: when controller learns ──
    if name == "Push":
        # Push: learns instantly
        learn_t = BREACH_T + 0.3
        ax.plot(learn_t, 2.2, marker="^", color="#333333", markersize=9, zorder=5,
                markeredgecolor="white", markeredgewidth=1)
        ax.text(learn_t, 2.55, "Controller\nnotified", fontsize=7.2,
                color="#333333", ha="center", va="bottom", fontweight="bold")
        # No staleness — draw a tiny gap
        ax.annotate("", xy=(BREACH_T + 0.1, 1.8), xytext=(learn_t - 0.1, 1.8),
                    arrowprops=dict(arrowstyle="<->", color=C_LIGHT, lw=1.5))
        ax.text((BREACH_T + learn_t) / 2, 1.5, "~0 s", fontsize=7,
                color=C_LIGHT, ha="center", va="top", style="italic")

    elif name == "Poll 5 s":
        # Poll-5s: next poll at t=10s (5s after breach at t=5)
        learn_t = 10.0
        # Poll markers every 5s
        for pt in [0, 5, 10, 15, 20]:
            ax.plot(pt, 1.8, marker="|", color=color, markersize=10, zorder=3,
                    markeredgewidth=1.5, alpha=0.5)
        ax.plot(learn_t, 2.2, marker="^", color="#333333", markersize=9, zorder=5,
                markeredgecolor="white", markeredgewidth=1)
        ax.text(learn_t, 2.55, "Poll captures\nbreach at t=10s",
                fontsize=7.2, color="#333333", ha="center", va="bottom",
                fontweight="bold")
        # Staleness arrow
        ax.annotate("", xy=(BREACH_T + 0.1, 1.15), xytext=(learn_t - 0.1, 1.15),
                    arrowprops=dict(arrowstyle="<->", color=C_LIGHT, lw=1.5))
        ax.text((BREACH_T + learn_t) / 2, 0.85, "staleness\n~5 s",
                fontsize=7.2, color=C_LIGHT, ha="center", va="top", style="italic")

    elif name == "Poll 12 s":
        # Poll-12s: polls at 0, 12, 24, ...
        # Breach at t=5, next poll at t=12
        learn_t = 12.0
        for pt in [0, 12, 24, 36]:
            ax.plot(pt, 1.8, marker="|", color=color, markersize=10, zorder=3,
                    markeredgewidth=1.5, alpha=0.5)
        ax.plot(learn_t, 2.2, marker="^", color="#333333", markersize=9, zorder=5,
                markeredgecolor="white", markeredgewidth=1)
        ax.text(learn_t, 2.55, "Poll captures\nbreach at t=12s",
                fontsize=7.2, color="#333333", ha="center", va="bottom",
                fontweight="bold")
        ax.annotate("", xy=(BREACH_T + 0.1, 1.15), xytext=(learn_t - 0.1, 1.15),
                    arrowprops=dict(arrowstyle="<->", color=C_LIGHT, lw=1.5))
        ax.text((BREACH_T + learn_t) / 2, 0.85, "staleness*\n~10 s (window)",
                fontsize=7, color=C_LIGHT, ha="center", va="top", style="italic")

    else:  # Poll 30 s
        learn_t = 30.0
        for pt in [0, 30, 60]:
            ax.plot(pt, 1.8, marker="|", color=color, markersize=12, zorder=3,
                    markeredgewidth=1.5, alpha=0.5)
        ax.plot(learn_t, 2.2, marker="^", color="#333333", markersize=9, zorder=5,
                markeredgecolor="white", markeredgewidth=1)
        ax.text(learn_t, 2.55, "Poll captures\nbreach at t=30s",
                fontsize=7.2, color="#333333", ha="center", va="bottom",
                fontweight="bold")
        ax.annotate("", xy=(BREACH_T + 0.1, 1.15), xytext=(learn_t - 0.1, 1.15),
                    arrowprops=dict(arrowstyle="<->", color=C_LIGHT, lw=1.5))
        ax.text((BREACH_T + learn_t) / 2, 0.85, "staleness*\n~10 s (window)",
                fontsize=7, color=C_LIGHT, ha="center", va="top", style="italic")

    # ── Spawn marker (reaction latency) ──
    spawn_t = learn_t + {"Push": 33, "Poll 5 s": 40, "Poll 12 s": 48, "Poll 30 s": 76}[name] * 0.06
    # Scale the reaction latency to fit in 60s timeline
    # Actually, reaction latency is ~33-76s which doesn't fit well.
    # Use a compressed representation with a "break" annotation
    react_label = {"Push": "~33 s", "Poll 5 s": "~40 s", "Poll 12 s": "~48 s", "Poll 30 s": "~76 s"}[name]
    
    # Show spawn as an arrow going right from learn_t with label
    ax.annotate("", xy=(learn_t + 15, 3.3), xytext=(learn_t + 0.5, 3.3),
                arrowprops=dict(arrowstyle="->", color=C_LINE, lw=1.8,
                                connectionstyle="arc3,rad=0"))
    ax.plot(learn_t + 15, 3.3, marker="p", color=C_SPAWN, markersize=9, zorder=5,
            markeredgecolor="white", markeredgewidth=1)
    ax.text(learn_t + 15, 3.6, f"Elasticity\nspawn", fontsize=7.2,
            color=C_SPAWN, ha="center", va="bottom", fontweight="bold")
    ax.text(learn_t + 7.5, 2.95, f"Reaction latency\n{react_label}",
            fontsize=7.2, color=C_LINE, ha="center", va="top", style="italic")

    # ── Right-side summary box ──
    summary_x = 50
    ax.text(summary_x, 2.8, f"Timeout rate", fontsize=7.5, color="#777777",
            ha="left", va="top")
    ax.text(summary_x, 2.4, failure, fontsize=10.5, fontweight="bold",
            color=color, ha="left", va="top")
    ax.text(summary_x, 1.9, f"Staleness: {staleness}", fontsize=7.5,
            color="#999999", ha="left", va="top")
    ax.text(summary_x, 1.5, f"Reaction: {react_label}", fontsize=7.5,
            color="#999999", ha="left", va="top")

    # Clean spines
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)

# ═══════════════════════════════════════════════════════════
# FOOTNOTE
# ═══════════════════════════════════════════════════════════
fig.text(0.5, 0.01,
         "*Poll-12s and Poll-30s staleness bounded by 10 s aggregation window, not by polling interval. "
         "Time axis compressed for illustration; actual reaction latencies span 33–76 s.  "
         "n = 3 replicates per mode, RANDOM_SEED = 42.",
         fontsize=7, color="#AAAAAA", ha="center", va="bottom", style="italic")

fig.tight_layout(rect=[0, 0.03, 1, 1])
out = "docs/diagrams/rq1/rq1_telemetry_timeline.png"
fig.savefig(out, dpi=200, facecolor="white", edgecolor="none")
print(f"Saved {out}")
plt.close(fig)
