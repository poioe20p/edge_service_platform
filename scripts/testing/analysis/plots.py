"""Shared matplotlib helpers: phase shading and elasticity-event overlays."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import matplotlib.axes

_PHASE_COLOURS = [
    "#e8f4f8", "#f8ece8", "#e8f8ec", "#f8f4e8", "#f0e8f8",
    "#e8f0f8", "#f8e8f4", "#eef8e8",
]

_SPAWN_COLOR   = "#1a7abf"
_ARMED_COLOR   = "#bf1a1a"
_COOLDOWN_COLOR = "#bf8c1a"


def shade_phases(ax: "matplotlib.axes.Axes",
                 boundaries: list[tuple[str, float, float]],
                 t0: float = 0.0) -> None:
    """Draw translucent background bands for each phase."""
    for i, (name, start, end) in enumerate(boundaries):
        colour = _PHASE_COLOURS[i % len(_PHASE_COLOURS)]
        ax.axvspan(start - t0, end - t0, alpha=0.25, color=colour, label=name)


def overlay_events(ax: "matplotlib.axes.Axes",
                   events,
                   t0: float = 0.0,
                   tier: str | None = None) -> None:
    """Draw vertical lines for spawn and armed elasticity events."""
    from .events import ElasticityEvent  # local import to avoid circular
    seen_labels: set[str] = set()

    for ev in events:
        if tier and ev.tier not in ("", tier):
            continue
        t = ev.ts - t0

        if ev.kind == "spawn_done":
            label = "spawn" if "spawn" not in seen_labels else None
            ax.axvline(t, color=_SPAWN_COLOR, linestyle="--", linewidth=0.8,
                       label=label, alpha=0.7)
            if label:
                seen_labels.add("spawn")

        elif ev.kind == "armed":
            label = "armed" if "armed" not in seen_labels else None
            ax.axvline(t, color=_ARMED_COLOR, linestyle=":", linewidth=0.8,
                       label=label, alpha=0.7)
            if label:
                seen_labels.add("armed")

        elif ev.kind == "cooldown":
            label = "cooldown" if "cooldown" not in seen_labels else None
            ax.axvline(t, color=_COOLDOWN_COLOR, linestyle="-.", linewidth=0.5,
                       label=label, alpha=0.5)
            if label:
                seen_labels.add("cooldown")
