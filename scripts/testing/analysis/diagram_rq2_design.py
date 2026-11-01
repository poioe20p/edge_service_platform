"""
RQ2 Experimental Design Diagram — Thesis-grade.
Shows: independent variable (3 routing modes) → system under test →
dependent variables (TTFT, initial share, latency, cumulative load),
with fixed-parameter box and RQ annotation.
Matches RQ1 design diagram style.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
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

# Academic colour palette (muted, print-friendly)
C_IV     = "#2C5F8A"
C_IV_BG  = "#D6E6F2"
C_SUT    = "#3A3A3A"
C_SUT_BG = "#F0F0F0"
C_DV     = "#8B3A3A"
C_DV_BG  = "#F5E6E6"
C_HC     = "#5A7D5A"
C_HC_BG  = "#E0ECE0"
C_RQ     = "#4A4A4A"
C_ARROW  = "#555555"

fig, ax = plt.subplots(1, 1, figsize=(16, 10.4))
ax.set_xlim(-0.5, 15.5)
ax.set_ylim(0, 11)
ax.axis("off")
ax.set_facecolor("white")
fig.patch.set_facecolor("white")

def draw_box(ax, x, y, w, h, facecolor, edgecolor, linewidth=1.5, zorder=1):
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.15",
        facecolor=facecolor, edgecolor=edgecolor,
        linewidth=linewidth, zorder=zorder,
    )
    ax.add_patch(box)

def draw_arrow(ax, x1, y1, x2, y2, color=C_ARROW, lw=1.8, zorder=2):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="->", color=color,
                                lw=lw, connectionstyle="arc3,rad=0"),
                zorder=zorder)

# ═══════════════════════════════════════════════════════════
# TITLE
# ═══════════════════════════════════════════════════════════
ax.text(7.5, 10.60, "RQ2 Experimental Design",
        fontsize=17, fontweight="bold", color=C_SUT, ha="center", va="top")
ax.text(7.5, 10.10, "Routing-Plane Awareness Timing \u2192 Load Redistribution & Service Quality",
        fontsize=10.5, color=C_RQ, ha="center", va="top", style="italic")

# ═══════════════════════════════════════════════════════════
# SYSTEM UNDER TEST (center)
# ═══════════════════════════════════════════════════════════
draw_box(ax, 4.2, 5.3, 6.6, 4.3, C_SUT_BG, C_SUT)
ax.text(7.5, 9.45, "System Under Test", fontsize=13, fontweight="bold",
        color=C_SUT, ha="center", va="top")

# Sub-box: Controller (box centre y=8.05 — text block centred)
draw_box(ax, 4.6, 7.2, 2.7, 1.7, "white", "#999999", linewidth=1)
ax.text(5.95, 8.70, "SDN Controller", fontsize=12, fontweight="bold",
        color=C_SUT, ha="center", va="top")
ax.text(5.95, 8.34, "(OS-Ken / OpenFlow)", fontsize=10, color="#666666",
        ha="center", va="top")
ax.text(5.95, 8.02, "VIP Routing + WSM", fontsize=10, color="#666666",
        ha="center", va="top")
ax.text(5.95, 7.70, "Elasticity Manager", fontsize=10, color="#666666",
        ha="center", va="top")
ax.text(5.95, 7.40, "Warm-Lease Engine", fontsize=10, color="#666666",
        ha="center", va="top")

# Sub-box: Edge Infrastructure (box centre y=8.05 — text block centred)
draw_box(ax, 7.7, 7.2, 2.7, 1.7, "white", "#999999", linewidth=1)
ax.text(9.05, 8.45, "Edge Infrastructure", fontsize=12, fontweight="bold",
        color=C_SUT, ha="center", va="top")
ax.text(9.05, 8.00, "Edge HTTP Servers", fontsize=10, color="#666666",
        ha="center", va="top")

# Arrows between sub-boxes
draw_arrow(ax, 7.35, 8.05, 7.65, 8.05, C_ARROW, 1.3)

# Clients sub-box
draw_box(ax, 4.6, 5.65, 5.8, 0.85, "white", "#999999", linewidth=1)
ax.text(7.5, 6.15, "32 Clients  \u2022  6000 Content Items  \u2022  9-Phase RQ2 Workload",
        fontsize=10, color="#555555", ha="center", va="center")

# ═══════════════════════════════════════════════════════════
# INDEPENDENT VARIABLE (left)
# ═══════════════════════════════════════════════════════════
draw_box(ax, -0.2, 5.3, 3.9, 4.3, C_IV_BG, C_IV)
ax.text(1.75, 9.45, "Independent Variable", fontsize=10, fontweight="bold",
        color=C_IV, ha="center", va="top")
ax.text(1.75, 9.15, "Routing-Awareness Mode", fontsize=9, color=C_IV,
        ha="center", va="top", style="italic")

# Three mode sub-boxes — spread vertically with breathing room
mode_h = 0.60
mode_data = [
    ("topology_host",     "No ramp; cold-start\nWSM herd (unknown \u2192 0.0)",  8.20),
    ("topology_slowstart", "Invisible until telemetry\ndiscovery; graduated ramp",  6.90),
    ("topology_lifecycle", "Warm lease at spawn time\n(zero discovery gap)",      5.60),
]
for label, desc, y_center in mode_data:
    sub_y = y_center - 0.12
    box_centre = sub_y + mode_h / 2
    draw_box(ax, 0.07, sub_y, 3.4, mode_h, "white", C_IV, linewidth=1)
    # Bold label — above box centre
    ax.text(1.85, box_centre + 0.28, label, fontsize=10, fontweight="bold",
            color=C_IV, ha="center", va="top")
    # Description — below box centre, clear separation from label
    ax.text(1.85, box_centre - 0.31, desc, fontsize=9.2, color="#444444",
            ha="center", va="bottom", linespacing=1.20)

# ═══════════════════════════════════════════════════════════
# DEPENDENT VARIABLES (right)
# ═══════════════════════════════════════════════════════════
draw_box(ax, 11.3, 5.3, 4.0, 4.3, C_DV_BG, C_DV)
ax.text(13.4, 9.45, "Dependent Variables", fontsize=12, fontweight="bold",
        color=C_DV, ha="center", va="top")
ax.text(13.4, 9.15, "Measured Outcomes", fontsize=10, color=C_DV,
        ha="center", va="top", style="italic")

dv_data = [
    ("TTFT",                      "Spawn \u2192 first request served"),
    ("Initial Load Share",        "Traffic share in first workload cycle"),
    ("p50 / p95 / p99 Latency",  "Client-perceived response time"),
    ("Coordination-Gap Penalty",  "Cost of stale or absent routing state"),
    ("Failure Rate",              "Client request timeouts"),
]
for i, (name, desc) in enumerate(dv_data):
    y = 8.75 - i * 0.75
    ax.text(11.65, y + 0.08, name, fontsize=10, fontweight="bold",
            color=C_DV, ha="left", va="center", zorder=3)
    ax.text(11.65, y - 0.20, desc, fontsize=9.2, color="#777777",
            ha="left", va="center", zorder=3)

# ═══════════════════════════════════════════════════════════
# FIXED PARAMETER (bottom)
# ═══════════════════════════════════════════════════════════
draw_box(ax, -0.2, 1.2, 15.3, 2.0, C_HC_BG, C_HC)
ax.text(7.5, 3.0, "Fixed Parameter", fontsize=12, fontweight="bold",
        color=C_HC, ha="center", va="top")

ax.text(7.5, 2.25, "Workload: phases_rq2.json (9-phase, rate=4.0)  —  CLIENTS=32, RANDOM_SEED=42, WAN_RTT_MS=50",
        fontsize=11, color="#333333", ha="center", va="center")

ax.text(7.5, 1.6, "Telemetry: Push (ZMQ), SS_ENABLED=0, golden scaling thresholds  —  all 9 runs share identical conditions",
        fontsize=10.2, color="#727171", ha="center", va="center", style="italic")

# ═══════════════════════════════════════════════════════════
# ARROWS
# ═══════════════════════════════════════════════════════════
draw_arrow(ax, 3.75, 7.4, 4.15, 7.4, C_ARROW, 2.0)
draw_arrow(ax, 10.85, 7.4, 11.25, 7.4, C_ARROW, 2.0)

# ═══════════════════════════════════════════════════════════
# RQ STATEMENT (between FC and SUT)
# ═══════════════════════════════════════════════════════════
rq_text = (
    "RQ2: \"How does the timing of routing-plane awareness relative to backend spawn —\n"
    "at spawn time (warm lease), at discovery time (slow-start ramp), or with no ramp-up —\n"
    "affect load redistribution quality during scale-up events in a stateful edge system?\""
)
draw_box(ax, -0.35, 3.65, 15.5, 0.90, "#FAFAFA", "#CCCCCC", linewidth=1)
ax.text(7.5, 4.10, rq_text, fontsize=11.5, color=C_RQ, ha="center", va="center",
        style="italic", linespacing=1.45)

# ═══════════════════════════════════════════════════════════
# FOOTER
# ═══════════════════════════════════════════════════════════
ax.text(7.5, 0.60, "n = 3 replicates per mode (9 total runs)  \u2022  July 2026  \u2022  ISCTE-IUL",
        fontsize=7.5, color="#AAAAAA", ha="center", va="top")

OUT_DIR = "c:/Users/themo/Documents/Trabalhos Academicos/Mestrado - Tese/efficient-storage-in-edge-scenarios/docs/diagrams/rq2"
import os
os.makedirs(OUT_DIR, exist_ok=True)
path = os.path.join(OUT_DIR, "rq2_experimental_design.png")
fig.savefig(path, dpi=200, facecolor="white", edgecolor="none")
print("Saved " + path)
plt.close(fig)
