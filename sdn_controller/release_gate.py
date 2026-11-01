"""release_gate.py — research_q3 release-mechanism gate.

Selects how surplus dynamic capacity is released (see ``RELEASE_MECHANISM``
in ``scaling_config.py``):

  - ``off``:        current behavior (incumbent release path unchanged) —
                    not a comparison arm; the gate is inactive.
  - ``drained``:    incumbent graceful release — drain the container, await
                    rs.remove confirmation, then teardown.
  - ``immediate``:  teardown without drain / rs.remove confirmation.
  - ``stabilized``: retention with recall — surplus capacity is held for
                    ``RELEASE_STABILIZE_S`` (clocked on ``time.monotonic()``,
                    passed in by callers): compute stays registered but
                    non-serving (quarantined), storage stays syncing but out
                    of ``VIP_DATA`` (retained), and either tier can be
                    recalled by an overload-recall signal; on horizon expiry
                    both tiers follow the safe termination path.

The quarantine state machine (IDLE → ACTIVE → DORMANT → IDLE) is used only in
``stabilized`` mode — one gate instance per tier (compute + storage). All
state is guarded by a ``threading.Lock``; every method is idempotent and
never raises.

See ``docs/operation/testing/experiment/research_q3_DO_NOT_CITE/experiment_plan.md`` for
the comparison design.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

try:
    from .scaling_config import _RELEASE_RECALL_K, _RELEASE_STABILIZE_S
except ImportError:  # imported as a top-level module (standalone smoke tests)
    from scaling_config import _RELEASE_RECALL_K, _RELEASE_STABILIZE_S

logger = logging.getLogger("os_ken.release_gate")

_VALID_MODES = ("off", "drained", "immediate", "stabilized")


class ReleaseGate:
    """Selects the release mechanism and owns the stabilized quarantine."""

    def __init__(self, mode: str) -> None:
        self._mode = mode
        if self._mode not in _VALID_MODES:
            logger.error(
                "unknown RELEASE_MECHANISM=%r — falling back to 'off'", self._mode)
            self._mode = "off"
        self._lock = threading.Lock()
        # Quarantine state while ACTIVE: (mac, container, start). The tuple is kept
        # while DORMANT but only ACTIVE answers quarantine_active()/expired()/recall().
        self._quarantine: tuple[str, str, float] | None = None
        self._dormant = False
        self._overload_window: deque = deque(maxlen=5)
        if self._mode == "off":
            logger.debug("release_gate: mode=%s", self._mode)
        else:
            logger.info("release_gate: mode=%s", self._mode)


    @property
    def mode(self) -> str:
        """The validated release mechanism (unknown values fall back to 'off')."""
        return self._mode

    @property
    def active(self) -> bool:
        """True when the mechanism is anything other than 'off'."""
        return self._mode != "off"


    def quarantine_begin(self, mac: str, container: str, now: float) -> bool:
        """Start a quarantine (IDLE → ACTIVE).

        Only in stabilized mode and only from IDLE; returns False when the
        mode is not "stabilized" or a quarantine is already active/dormant
        (idempotent).
        """
        with self._lock:
            if self._mode != "stabilized":
                return False
            if self._quarantine is not None or self._dormant:
                return False
            self._quarantine = (mac, container, now)
            return True

    def quarantine_active(self) -> bool:
        """True while a quarantine is ACTIVE (not IDLE, not DORMANT)."""
        with self._lock:
            return self._quarantine is not None and not self._dormant

    def quarantine_expired(self, now: float) -> bool:
        """True when a quarantine is active and now - start >= _RELEASE_STABILIZE_S."""
        with self._lock:
            if self._quarantine is None or self._dormant:
                return False
            return now - self._quarantine[2] >= _RELEASE_STABILIZE_S

    def quarantine_mac(self) -> str | None:
        """The quarantined mac (None when no quarantine)."""
        with self._lock:
            return self._quarantine[0] if self._quarantine is not None else None

    def quarantine_container(self) -> str:
        """The quarantined container name ("" when no quarantine) for log rows."""
        with self._lock:
            return self._quarantine[1] if self._quarantine is not None else ""

    def recall(self) -> tuple[str, str] | None:
        """Cancel an active quarantine (ACTIVE → IDLE).

        Only acts while a quarantine is ACTIVE — a DORMANT quarantine is NOT
        cleared by recall(). Returns the canceled (mac, container) when a
        quarantine was actually canceled, None otherwise (dormant → None,
        idempotent). Callers must read the returned identity — the gate's
        own state is already cleared.
        """
        with self._lock:
            if self._quarantine is None or self._dormant:
                return None
            mac, container = self._quarantine[0], self._quarantine[1]
            self._quarantine = None
            return mac, container

    def mark_finalized(self) -> None:
        """Mark the quarantine finalized (ACTIVE → DORMANT).

        Called after a finalization alert is submitted. Idempotent.
        """
        with self._lock:
            if self._quarantine is not None and not self._dormant:
                self._dormant = True

    def dormant(self) -> bool:
        """True while the quarantine is DORMANT (finalized, awaiting release)."""
        with self._lock:
            return self._dormant

    def notify_compute_scale_up(self) -> None:
        """A compute scale-up decision ends the dormant suppression (DORMANT → IDLE)."""
        with self._lock:
            if self._dormant:
                self._dormant = False
                self._quarantine = None

    def clear(self, mac: str) -> None:
        """Called on removal-complete / absent-cleanup.

        When mac matches the quarantined mac, cancel the quarantine
        (ACTIVE → IDLE). DORMANT state is NOT touched by clear().
        """
        with self._lock:
            if self._dormant:
                return
            if self._quarantine is not None and self._quarantine[0] == mac:
                self._quarantine = None


    def feed_window(self, overload: bool) -> bool:
        """Push an overload flag into the recall window (maxlen=5).

        Returns True when sum(window) >= _RELEASE_RECALL_K (the caller then
        calls recall()).
        """
        with self._lock:
            self._overload_window.append(overload)
            return sum(self._overload_window) >= _RELEASE_RECALL_K
