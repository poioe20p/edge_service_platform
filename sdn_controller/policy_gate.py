"""policy_gate.py — RQ2 bottleneck-aware scaling-action selection.

A thin, pure (no-I/O) decision layer that turns two per-window
``ScaleUpVerdict`` objects into a set of selected actions, applying the
configured policy and the per-tier action budget. Called exclusively from
Thread 2 (the mediator in ``main_n*.py``).

Modes (``SCALEUP_POLICY``):
- ``dual`` (default) — pre-RQ2 behavior: every fired tier is selected, no
  budget. NOT an RQ2 comparison arm.
- ``fixed_compute_first`` — RQ2 arm: compute when it fires, never storage.
- ``fixed_storage_first`` — RQ2 arm: storage when it fires, never compute.
- ``bottleneck_aware`` — RQ2 arm: classify the bottleneck (D3) and select the
  matching tier.

See ``docs/telemetry-and-scaling.md``.
"""

from __future__ import annotations

import logging
from typing import Literal

from .scaling_config import (
    _SCALEUP_POLICY,
    _ACTION_BUDGET_PER_TIER,
    _BOTTLENECK_CLASSIFY_MARGIN,
)
from .scaling_policy import ScaleUpVerdict

logger = logging.getLogger("os_ken.policy_gate")

Tier = Literal["compute", "storage"]

_VALID_MODES = ("dual", "fixed_compute_first", "fixed_storage_first", "bottleneck_aware")


class PolicyGate:
    """Selects which tier(s) scale up from per-window verdicts, within budget."""

    def __init__(self,
                 mode: str | None = None,
                 budget_per_tier: int | None = None,
                 margin: float | None = None) -> None:
        self.mode = mode if mode is not None else _SCALEUP_POLICY
        if self.mode not in _VALID_MODES:
            logger.error(
                "unknown SCALEUP_POLICY=%r — falling back to 'dual'", self.mode)
            self.mode = "dual"
        # The action budget applies only to the three RQ2 arms; dual has it
        # disabled (0). Per-tier, per controller (per LAN).
        self._budget = (budget_per_tier if budget_per_tier is not None
                        else _ACTION_BUDGET_PER_TIER)
        if self.mode == "dual":
            self._budget = 0
        self._margin = (margin if margin is not None
                        else _BOTTLENECK_CLASSIFY_MARGIN)
        self._used: dict[Tier, int] = {"compute": 0, "storage": 0}
        logger.info(
            "policy_gate: mode=%s budget_per_tier=%d margin=%.3f",
            self.mode, self._budget, self._margin,
        )



    def select(self, compute_v: ScaleUpVerdict,
               storage_v: ScaleUpVerdict) -> tuple[Tier, ...]:
        """Return the selected tier(s) for this window (0..2; ≤1 in RQ2 arms).

        - dual: every fired tier.
        - fixed_*: the configured tier if it fired.
        - bottleneck_aware: both fired → the classified tier; one fired → that
          tier; none fired → ().
        """
        if self.mode == "dual":
            return tuple(
                t for t, v in (("compute", compute_v), ("storage", storage_v))
                if v.fired
            )
        if self.mode == "fixed_compute_first":
            return ("compute",) if compute_v.fired else ()
        if self.mode == "fixed_storage_first":
            return ("storage",) if storage_v.fired else ()
        if compute_v.fired and storage_v.fired:
            return (self.classify(compute_v, storage_v),)
        if compute_v.fired:
            return ("compute",)
        if storage_v.fired:
            return ("storage",)
        return ()

    def classify(self, compute_v: ScaleUpVerdict,
                 storage_v: ScaleUpVerdict) -> Tier:
        """Declared bottleneck classification over ELIGIBLE tiers (D3).

        Exactly one eligible tier → that tier. Both eligible → higher
        score_norm wins; |Δ| <= margin → storage (documented tie-break: storage
        is the higher-urgency tier). The mediator maps the neither-eligible case
        to ``"n/a"`` before calling this.
        """
        c_elig = compute_v.eligible
        s_elig = storage_v.eligible
        if c_elig and not s_elig:
            return "compute"
        if s_elig and not c_elig:
            return "storage"
        if not c_elig and not s_elig:
            # Caller guards with "n/a"; unreachable default kept for safety.
            return "storage"
        if compute_v.score_norm >= storage_v.score_norm + self._margin:
            return "compute"
        return "storage"


    def budget_available(self, tier: Tier) -> bool:
        if self._budget <= 0:
            return True
        return self._used[tier] < self._budget

    def consume_budget(self, tier: Tier) -> None:
        if self._budget > 0:
            self._used[tier] += 1

    def budget_used(self, tier: Tier) -> int:
        return self._used[tier]
