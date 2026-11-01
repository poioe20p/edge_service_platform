"""Shared visual style for the Chapter 5 thesis figures.

One palette, one rcParams block and one ordered scheme keep the RQ1, RQ2 and
RQ3 graph-like figures visually consistent. The palette is seaborn-deep, and
the configuration colours follow a single order shared by all three RQs:
grey for position 1 (the reference or control), blue for position 2, orange
for position 3, and green for position 4. Red is reserved for annotations and
is never a configuration colour.
"""
from __future__ import annotations

import matplotlib.pyplot as plt

PALETTE = {
    "grey": "#8C8C8C",
    "blue": "#4C72B0",
    "orange": "#DD8452",
    "green": "#55A868",
    "red": "#C44E52",
    "purple": "#8172B2",
    "light_blue": "#64B5CD",
}
INK = "#333333"
GRID = "#E0E0E0"

ORDER = ("grey", "blue", "orange", "green")


def series_colours(n: int) -> list[str]:
    """Return the fixed cross-RQ colour order, truncated to *n* series."""
    if n > len(ORDER):
        raise ValueError(
            f"the fixed scheme has {len(ORDER)} colours, {n} requested"
        )
    return [PALETTE[name] for name in ORDER[:n]]


def apply_house_style() -> None:
    """Apply the rcParams shared by every Chapter 5 figure."""
    plt.rcParams.update(
        {
            "font.size": 11,
            "axes.labelsize": 11,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 9.5,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "grid.linestyle": ":",
            "axes.axisbelow": True,
            "figure.dpi": 150,
            "savefig.dpi": 200,
            "savefig.bbox": "tight",
        }
    )


def panel_title(ax, text: str) -> None:
    """Bold, left-aligned panel title."""
    ax.set_title(text, loc="left", fontsize=10.5, fontweight="bold", pad=8)


def style_axes(ax, ylabel: str | None = None, title: str | None = None) -> None:
    """Apply the shared axis treatment."""
    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        panel_title(ax, title)
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle=":")
    ax.set_axisbelow(True)
