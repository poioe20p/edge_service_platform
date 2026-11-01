"""
RQ1 Experimental Design Diagram — Thesis-grade.
Shows: independent variable (4 telemetry modes) -> system under test ->
dependent variables (reaction latency, staleness, service quality, mechanism exercise),
with fixed-parameter box and RQ annotation.
Matches RQ2 design diagram style.
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

# ====================================================================
# TITLE
# ====================================================================
ax.text(7.5, 10.60, "RQ1 Experimental Design",
        fontsize=17, fontweight="bold", color=C_SUT, ha="center", va="top")
ax.text(7.5, 10.10, "Telemetry Delivery Cadence \u2192 Elasticity & Service Quality",
        fontsize=10.5, color=C_RQ, ha="center", va="top", style="italic")

# ====================================================================
# SYSTEM UNDER TEST (center)
# ====================================================================
draw_box(ax, 4.2, 5.3, 6.6, 4.3, C_SUT_BG, C_SUT)
ax.text(7.5, 9.45, "System Under Test", fontsize=13, fontweight="bold",
        color=C_SUT, ha="center", va="top")

# Sub-box: Controller
draw_box(ax, 4.6, 7.2, 2.7, 1.7, "white", "#999999", linewidth=1)
ax.text(5.95, 8.70, "SDN Controller", fontsize=12, fontweight="bold",
        color=C_SUT, ha="center", va="top")
ax.text(5.95, 8.34, "(OS-Ken / OpenFlow)", fontsize=11, color="#666666",
        ha="center", va="top")
ax.text(5.95, 8.02, "VIP Routing + WSM", fontsize=11, color="#666666",
        ha="center", va="top")
ax.text(5.95, 7.70, "Elasticity Manager", fontsize=11, color="#666666",
        ha="center", va="top")

# Sub-box: Edge Infrastructure
draw_box(ax, 7.7, 7.2, 2.7, 1.7, "white", "#999999", linewidth=1)
ax.text(9.05, 8.70, "Edge Infrastructure", fontsize=12, fontweight="bold",
        color=C_SUT, ha="center", va="top")
ax.text(9.05, 8.30, "Edge HTTP Servers", fontsize=11, color="#666666",
        ha="center", va="center")
ax.text(9.05, 8.00, "Edge Storage", fontsize=11, color="#666666",
        ha="center", va="center")

# Arrows between sub-boxes
draw_arrow(ax, 7.35, 8.05, 7.65, 8.05, C_ARROW, 1.3)

# Clients sub-box
draw_box(ax, 4.6, 5.65, 5.8, 0.85, "white", "#999999", linewidth=1)
ax.text(7.5, 6.15, "48 Clients  \u2022  100 Content Items  \u2022  7-Phase RQ1 Workload",
        fontsize=11, color="#555555", ha="center", va="center")

# ====================================================================
# INDEPENDENT VARIABLE (left)
# ====================================================================
draw_box(ax, -0.2, 5.3, 3.9, 4.3, C_IV_BG, C_IV)
ax.text(1.75, 9.61, "Independent Variable", fontsize=11, fontweight="bold",
        color=C_IV, ha="center", va="top")
ax.text(1.75, 9.31, "Telemetry Mode", fontsize=10, color=C_IV,
        ha="center", va="top", style="italic")

# Four mode sub-boxes — spread vertically with breathing room
mode_h = 0.40
mode_data = [
    ("Push (ZMQ)",       "Instant push on event\n(~0 s staleness)",              8.60),
    ("Poll 5 s",         "Controller polls every 5 s\n(~5 s staleness)",         7.60),
    ("Poll 12 s",        "Controller polls every 12 s\n(~10 s staleness*)",      6.60),
    ("Poll 30 s",        "Controller polls every 30 s\n(~10 s staleness*)",      5.60),
]
for label, desc, y_center in mode_data:
    sub_y = y_center - 0.12
    box_centre = sub_y + mode_h / 2
    draw_box(ax, 0.07, sub_y, 3.4, mode_h, "white", C_IV, linewidth=1)
    # Bold label — above box centre
    ax.text(1.85, box_centre + 0.27, label, fontsize=11, fontweight="bold",
            color=C_IV, ha="center", va="top")
    # Description — below box centre, clear separation from label
    ax.text(1.85, box_centre - 0.30, desc, fontsize=10.2, color="#444444",
            ha="center", va="bottom", linespacing=1.20)

# ====================================================================
# DEPENDENT VARIABLES (right)
# ====================================================================
draw_box(ax, 11.3, 5.3, 4.0, 4.3, C_DV_BG, C_DV)
ax.text(13.4, 9.45, "Dependent Variables", fontsize=12, fontweight="bold",
        color=C_DV, ha="center", va="top")
ax.text(13.4, 9.15, "Measured Outcomes", fontsize=11, color=C_DV,
        ha="center", va="top", style="italic")

dv_data = [
    ("Reaction Latency",         "Breach detection \u2192 spawn time"),
    ("Information Staleness",    "Age of telemetry at decision"),
    ("Service Quality",          "Client request timeout rate and perceived latency"),
    ("Mechanism Exercise",       "All 4 elasticity mechanisms"),
]
for i, (name, desc) in enumerate(dv_data):
    y = 8.55 - i * 0.75
    ax.text(11.65, y + 0.08, name, fontsize=11, fontweight="bold",
            color=C_DV, ha="left", va="center", zorder=3)
    ax.text(11.65, y - 0.20, desc, fontsize=10, color="#777777",
            ha="left", va="center", zorder=3)

# ====================================================================
# FIXED PARAMETER (bottom)
# ====================================================================
draw_box(ax, -0.2, 1.2, 15.3, 2.0, C_HC_BG, C_HC)
ax.text(7.5, 3.0, "Fixed Parameter", fontsize=12, fontweight="bold",
        color=C_HC, ha="center", va="top")

ax.text(7.5, 2.25, "WAN Latency = 200 ms  \u2022  CLIENTS=48  \u2022  RANDOM_SEED=42  \u2022  CURL_MAX_TIME=30  \u2022  VIP_HARD_TIMEOUT=60",
        fontsize=11, color="#333333", ha="center", va="center")

ax.text(7.5, 1.6, "Telemetry: Poll modes use AGGREGATION_WINDOW_S=10, SCALEDOWN_COMPUTE_COOLDOWN_S=180  —  all 12 runs share identical conditions",
        fontsize=10.2, color="#727171", ha="center", va="center", style="italic")

# ====================================================================
# ARROWS
# ====================================================================
draw_arrow(ax, 3.75, 7.4, 4.15, 7.4, C_ARROW, 2.0)
draw_arrow(ax, 10.85, 7.4, 11.25, 7.4, C_ARROW, 2.0)

# ====================================================================
# RQ STATEMENT (between FC and SUT)
# ====================================================================
rq_text = (
    "RQ1: \"How does telemetry delivery cadence affect elasticity reaction latency,\n"
    "information staleness, and service quality in an edge storage platform?\""
)
draw_box(ax, -0.35, 3.65, 15.5, 0.80, "#FAFAFA", "#CCCCCC", linewidth=1)
ax.text(7.5, 4.05, rq_text, fontsize=15, color=C_RQ, ha="center", va="center",
        style="italic", linespacing=1.45)

# ====================================================================
# FOOTER
# ====================================================================
ax.text(7.5, 0.70,
        "*Poll-12s and Poll-30s staleness bounded by 10 s aggregation window, not by polling interval",
        fontsize=7.8, color="#999999", ha="center", va="center", style="italic")
ax.text(7.5, 0.40, "n = 3 replicates per mode (12 total runs)  \u2022  July 2026  \u2022  ISCTE-IUL",
        fontsize=7.5, color="#AAAAAA", ha="center", va="top")

# --- SAVE ---
out = "docs/diagrams/rq1/rq1_experimental_design.png"
fig.savefig(out, dpi=200, facecolor="white", edgecolor="none")
print(f"Saved {out}")
plt.close(fig)
