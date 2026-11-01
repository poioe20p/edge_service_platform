"""Demand-to-Capacity Chain -- thesis-grade diagram (matches diagram_rq*_design.py style).
Two-row snake layout with role-specific shapes: demand shift -> ... -> usable capacity,
with the three studied interfaces bracketed below the transitions.
Output: tese/images/demand_to_capacity_chain.png
"""
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyBboxPatch

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

C_NODE      = "#3A3A3A"
C_NODE_BG   = "#F0F0F0"
C_DEMAND    = "#8B3A3A"
C_DEMAND_BG = "#F5E6E6"
C_GOAL      = "#5A7D5A"
C_GOAL_BG   = "#E0ECE0"
C_IFACE     = "#2C5F8A"

fig, ax = plt.subplots(figsize=(14.6, 10.8))
ax.set_xlim(-0.2, 14.4)
ax.set_ylim(-0.2, 10.6)
ax.axis("off")
ax.set_facecolor("white")
fig.patch.set_facecolor("white")


def draw_shape(kind, cx, cy, w, h, fc, ec):
    if kind == "para":  # parallelogram: data / input-output
        s = 0.28
        pts = [(cx - w / 2 + s, cy + h / 2), (cx + w / 2 + s, cy + h / 2),
               (cx + w / 2 - s, cy - h / 2), (cx - w / 2 - s, cy - h / 2)]
        ax.add_patch(plt.Polygon(pts, closed=True, facecolor=fc,
                                 edgecolor=ec, linewidth=1.5, zorder=3))
    elif kind == "dia":  # diamond: decision
        pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
        ax.add_patch(plt.Polygon(pts, closed=True, facecolor=fc,
                                 edgecolor=ec, linewidth=1.5, zorder=3))
    elif kind == "rect":  # rounded rectangle: process
        ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                    boxstyle="round,pad=0.10",
                                    facecolor=fc, edgecolor=ec,
                                    linewidth=1.5, zorder=3))
    elif kind == "hex":  # hexagon: state
        pts = []
        for k in range(6):
            ang = math.pi / 6 + k * math.pi / 3
            pts.append((cx + (w / 2) * math.cos(ang), cy + (h / 2) * math.sin(ang)))
        ax.add_patch(plt.Polygon(pts, closed=True, facecolor=fc,
                                 edgecolor=ec, linewidth=1.5, zorder=3))
    elif kind == "oval":  # ellipse: terminal / outcome
        ax.add_patch(Ellipse((cx, cy), w, h, facecolor=fc, edgecolor=ec,
                             linewidth=1.5, zorder=3))


def node_label(cx, cy, text, color, size=10.5):
    ax.text(cx, cy, text, fontsize=size, fontweight="bold", color=color,
            ha="center", va="center", zorder=4)


def arrow(x1, y1, x2, y2, color="#555555", lw=1.8):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="->", color=color, lw=lw), zorder=2)


def bracket(x1, x2, y, label_text, top_y):
    xc = (x1 + x2) / 2
    ax.plot([x1, x2], [y, y], color=C_IFACE, lw=1.8, zorder=3)
    for xb in (x1, x2):
        ax.plot([xb, xb], [y, y + 0.2], color=C_IFACE, lw=1.8, zorder=3)
    ax.plot([xc, xc], [y, top_y], color=C_IFACE, lw=1.1, ls="--", alpha=0.6, zorder=1)
    ax.text(xc, y - 0.12, label_text, fontsize=10, color=C_IFACE,
            ha="center", va="top", zorder=4)


TOP_Y, BOT_Y, GAP = 8.4, 3.9, 1.15

# ── Top row: demand → telemetry (aggregated) → decision → action ──
top = [
    ("para", 1.9,  1.9,  "Demand\nshift",          C_DEMAND, C_DEMAND_BG),
    ("para", 2.5,  2.5,  "Telemetry\nobservation\n& delivery", C_NODE, C_NODE_BG),
    ("dia",  2.05, 2.1,  "Scaling\ndecision",      C_NODE, C_NODE_BG),
    ("rect", 1.9,  1.9,  "Capacity\naction",       C_NODE, C_NODE_BG),
]
EXTRA = 0.55  # extra spacing before and after the scaling decision
cxs_top = []
cx = top[0][1] / 2 + 0.4
for i, (kind, w, h, text, ec, bg) in enumerate(top):
    draw_shape(kind, cx, TOP_Y, w, h, bg, ec)
    node_label(cx, TOP_Y, text, ec)
    cxs_top.append(cx)
    cx += w / 2 + GAP
    if i in (1, 2):  # after the telemetry node and after the decision
        cx += EXTRA

for i in range(3):
    x1 = cxs_top[i] + top[i][1] / 2 + 0.05
    x2 = cxs_top[i + 1] - top[i + 1][1] / 2 - 0.05
    arrow(x1, TOP_Y, x2, TOP_Y)

# ── Bottom row (right → left on screen): readiness → admission → usable ──
c5 = cxs_top[3]
hex_w, hex_h = 2.0, 2.0
c6 = c5 - hex_w / 2 - GAP - 0.95
c7 = c6 - 0.95 - GAP - 1.05
draw_shape("hex", c5, BOT_Y, hex_w, hex_h, C_NODE_BG, C_NODE)
node_label(c5, BOT_Y, "Backend\nreadiness", C_NODE)
draw_shape("rect", c6, BOT_Y, 1.9, 1.9, C_NODE_BG, C_NODE)
node_label(c6, BOT_Y, "Routing\nadmission", C_NODE)
draw_shape("oval", c7, BOT_Y, 2.1, 1.9, C_GOAL_BG, C_GOAL)
node_label(c7, BOT_Y, "Usable\ncapacity", C_GOAL)

arrow(c5, TOP_Y - 0.95, c5, BOT_Y + hex_h / 2 + 0.05)           # down connector
arrow(c5 - hex_w / 2 - 0.05, BOT_Y, c6 + 0.95 + 0.05, BOT_Y)    # readiness → admission
arrow(c6 - 0.95 - 0.05, BOT_Y, c7 + 1.05 + 0.05, BOT_Y)         # admission → usable

ax.text(c7, BOT_Y - 0.95 - 0.4, "first successful request",
        fontsize=9.5, color=C_GOAL, ha="center", va="top", style="italic")

# ── Interface brackets ──
bracket(cxs_top[1] + 1.25 + 0.05, cxs_top[2] - 1.025 - 0.05, 6.8,
        "Interface 1 (RQ1)\nhow demand evidence\nreaches the decision", 7.15)
bracket(cxs_top[2] + 1.025 + 0.05, cxs_top[3] - 0.95 - 0.05, 6.8,
        "Interface 2 (RQ2)\nwhich capacity action\nthe decision hands over\nto the infrastructure", 7.35)
bracket(c6 + 0.95 + 0.05, c5 - hex_w / 2 - 0.05, 2.35,
        "Interface 3 (RQ3)\nwhen a ready backend\nis admitted to traffic", 2.95)

# ── Time cue (right margin) ──
arrow(13.9, 1.8, 13.9, 9.3, color="#999999", lw=1.2)
ax.text(13.9, 5.55, "time", fontsize=9.5, color="#999999",
        ha="center", va="center", style="italic", rotation=90)

REPO_ROOT = Path(__file__).resolve().parents[4]
for name in ("demand_to_capacity_chain_v2.png",
             "demand_to_capacity_chain.png"):
    OUT = REPO_ROOT / "tese" / "images" / name
    fig.savefig(OUT, dpi=200, facecolor="white", edgecolor="none")
    print("saved", OUT)
