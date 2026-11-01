"""scaling_policy.py — Scaling decision engine (Strategy pattern).

Owns sliding-window deques and cooldown timestamps. Evaluates DomainSummary
metrics and returns scaling decisions. Does NOT submit alerts or access
the ElasticityManager — the mediator handles that.

Thread safety: all methods are called exclusively from Thread 2.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from .scaling_config import (
    _W_STORAGE_CPU, _W_T_DB,
    _STORAGE_CPU_FLOOR, _STORAGE_CPU_SPAN,
    _T_DB_FLOOR, _T_DB_SPAN,
    _W_CPU, _W_T_PROC,
    _CPU_FLOOR, _CPU_SPAN,
    _T_PROC_FLOOR, _T_PROC_SPAN,
    _SCALE_UP_WINDOW_SIZE, _SCALE_UP_REQUIRED,
    _SCALEUP_COMPUTE_BASE_THRESHOLD, _SCALEUP_COMPUTE_THRESHOLD_INCREMENT,
    _SCALEUP_COMPUTE_MAX_THRESHOLD, _SCALEUP_COMPUTE_COOLDOWN_S,
    _SCALEUP_COMPUTE_PEER_RELIEF, _SCALEUP_COMPUTE_PEER_HEALTH_THRESHOLD,
    _SCALEUP_STORAGE_BASE_THRESHOLD, _SCALEUP_STORAGE_THRESHOLD_INCREMENT,
    _SCALEUP_STORAGE_MIN_INCREMENT, _SCALEUP_STORAGE_MAX_THRESHOLD,
    _SCALEUP_STORAGE_WINDOW_SIZE,
    _SCALEUP_STORAGE_REQUIRED,
    _TAU_CPU_DOWN, _TAU_PROC_DOWN_MS,
    _TAU_STORAGE_CPU_DOWN, _TAU_DB_DOWN_MS,
    _SCALE_DOWN_PROC_TIMEOUT_CEILING_MS, _SCALE_DOWN_DB_TIMEOUT_CEILING_MS,
    _SCALE_DOWN_COMPUTE_WINDOW_SIZE, _SCALE_DOWN_COMPUTE_REQUIRED,
    _SCALE_DOWN_STORAGE_WINDOW_SIZE, _SCALE_DOWN_STORAGE_REQUIRED,
    _SCALEDOWN_STORAGE_COOLDOWN_S, _SCALEDOWN_COMPUTE_COOLDOWN_S,
    _SCALEUP_STORAGE_COOLDOWN_S,
    _MAX_DYNAMIC_STORAGE, _MAX_DYNAMIC_COMPUTE,
    _LATENCY_SIGNAL_MODE,
    _STORAGE_SCALE_DOWN_CPU_AWARE,
)
from .elasticity.elasticity import ComputeAlert, DataAlert
from .telemetry.models import DomainSummary

logger = logging.getLogger('os_ken.scaling_policy')


@dataclass(frozen=True)
class ScaleUpVerdict:
    """Per-window per-tier scale-up evaluation result (identical in all RQ2 arms).

    ``eligible``: not busy/blocked, not at cap, scale-up cooldown elapsed.
    ``fired``:    eligible AND the sliding window reached REQUIRED hits (the
                  scale-up window was advanced and cleared).
    ``score_norm`` / ``score`` / ``threshold`` are evidence, computed on every
    call regardless of eligibility — this is what makes the RQ2 decision log a
    continuous evidence series (see rq2_preparation.md).
    """

    tier: Literal["compute", "storage"]
    eligible: bool
    fired: bool
    score_norm: float
    score: float
    threshold: float


class ScalingPolicy:
    """Decides whether to scale up or down based on DomainSummary metrics.

    Returns scaling decisions — does NOT submit alerts directly.
    """

    def __init__(self) -> None:
        self._scale_up_compute_window: deque[bool] = deque(maxlen=_SCALE_UP_WINDOW_SIZE)
        self._scale_up_storage_window: deque[bool] = deque(maxlen=_SCALEUP_STORAGE_WINDOW_SIZE)
        self._scale_down_compute_window: deque[bool] = deque(maxlen=_SCALE_DOWN_COMPUTE_WINDOW_SIZE)
        self._scale_down_storage_window: deque[bool] = deque(maxlen=_SCALE_DOWN_STORAGE_WINDOW_SIZE)
        self._last_storage_scale_up_ts: float = float('-inf')
        self._last_compute_scale_up_ts: float = float('-inf')
        # RQ2 fire timestamps — drive the scale-DOWN cooldown (D7). A tier that
        # fires but is policy-suppressed still protects itself from scale-down.
        self._last_storage_scale_up_fired_ts: float = float('-inf')
        self._last_compute_scale_up_fired_ts: float = float('-inf')
        self._prev_scale_down_compute_armed: bool = False
        self._prev_scale_down_storage_armed: bool = False

        logger.info(
            "scale-up: compute[τ_base=%.2f incr=%.2f cap=%.2f window=%d/%d cooldown=%.0fs "
            "peer_relief=%.2f w_cpu=%.1f w_lat=%.1f max_nodes=%d]  "
            "storage[τ_base=%.2f incr=%.2f cap=%.2f window=%d/%d "
            "w_cpu=%.1f w_lat=%.1f cooldown=%.0fs max_nodes=%d]",
            _SCALEUP_COMPUTE_BASE_THRESHOLD,
            _SCALEUP_COMPUTE_THRESHOLD_INCREMENT,
            _SCALEUP_COMPUTE_MAX_THRESHOLD,
            _SCALE_UP_REQUIRED, _SCALE_UP_WINDOW_SIZE,
            _SCALEUP_COMPUTE_COOLDOWN_S,
            _SCALEUP_COMPUTE_PEER_RELIEF,
            _W_CPU, _W_T_PROC,
            _MAX_DYNAMIC_COMPUTE,
            _SCALEUP_STORAGE_BASE_THRESHOLD, _SCALEUP_STORAGE_THRESHOLD_INCREMENT,
            _SCALEUP_STORAGE_MAX_THRESHOLD, _SCALEUP_STORAGE_REQUIRED, _SCALEUP_STORAGE_WINDOW_SIZE,
            _W_STORAGE_CPU, _W_T_DB, _SCALEUP_STORAGE_COOLDOWN_S,
            _MAX_DYNAMIC_STORAGE,
        )

    @staticmethod
    def degradation_score(cpu: float, latency: float,
                          w_cpu: float, w_lat: float,
                          cpu_floor: float, cpu_span: float,
                          lat_floor: float, lat_span: float) -> float:
        """Weighted degradation score in [0, w_cpu + w_lat].

        Both components saturate at 1.0 so a single extreme latency window
        cannot dominate the sliding-window hit count; sustained badness is
        expressed via WINDOW_SIZE / REQUIRED instead of unbounded magnitude.
        """
        cpu_component = min(1.0, max(0.0, cpu - cpu_floor) / cpu_span) if cpu_span else 0.0
        lat_component = min(1.0, max(0.0, latency - lat_floor) / lat_span) if lat_span else 0.0
        return w_cpu * cpu_component + w_lat * lat_component

    @staticmethod
    def compute_latency_signal(ds: DomainSummary) -> float:
        """Compute latency signal — window MEDIAN when LATENCY_SIGNAL_MODE=median,
        else mean (legacy / RQ1). Falls back to the mean when the aggregator
        did not publish a median (pre-median aggregators)."""
        if _LATENCY_SIGNAL_MODE == "median" and ds.median_time_proc_ms is not None:
            return ds.median_time_proc_ms
        return ds.avg_time_proc_ms

    @staticmethod
    def storage_latency_signal(ds: DomainSummary) -> float:
        """Storage latency signal — window MEDIAN when LATENCY_SIGNAL_MODE=median,
        else mean (legacy / RQ1). Falls back to the mean when the aggregator
        did not publish a median (pre-median aggregators)."""
        if _LATENCY_SIGNAL_MODE == "median" and ds.median_time_db_ms is not None:
            return ds.median_time_db_ms
        return ds.avg_time_db_ms

    def compute_cooldown_remaining(self) -> float:
        """Scale-DOWN cooldown — keyed on the last compute scale-up FIRE (D7)."""
        return max(0.0, _SCALEDOWN_COMPUTE_COOLDOWN_S - (time.monotonic() - self._last_compute_scale_up_fired_ts))

    def storage_cooldown_remaining(self) -> float:
        """Scale-DOWN cooldown — keyed on the last storage scale-up FIRE (D7)."""
        return max(0.0, _SCALEDOWN_STORAGE_COOLDOWN_S - (time.monotonic() - self._last_storage_scale_up_fired_ts))

    def compute_scaleup_cooldown_remaining(self) -> float:
        return max(0.0, _SCALEUP_COMPUTE_COOLDOWN_S - (time.monotonic() - self._last_compute_scale_up_ts))

    def storage_scaleup_cooldown_remaining(self) -> float:
        return max(0.0, _SCALEUP_STORAGE_COOLDOWN_S - (time.monotonic() - self._last_storage_scale_up_ts))

    def record_storage_activation(self) -> None:
        """Bookkeeping after a reserve activation: reset cooldown and scale-down window.

        Called by the mediator after a reserved storage node is consumed
        into active service. This is the same cross-direction reset that a
        normal storage scale-up commit (``commit_storage_scale_up``) applies.
        """
        self._last_storage_scale_up_ts = time.monotonic()
        self._last_storage_scale_up_fired_ts = time.monotonic()
        self.clear_scale_down_storage_window()

    def evaluate_scale_up(self, ds: DomainSummary, lan: int, network_id: str,
                          dynamic_storage_count: int,
                          dynamic_compute_count: int,
                          peer_ds: DomainSummary | None = None,
                          *,
                          allow_compute: bool = True,
                          allow_storage: bool = True) -> list[ComputeAlert | DataAlert]:
        """Dual (pre-RQ2) path: both tiers may fire and submit independently.

        Uses the same per-tier verdict evaluation as the RQ2 arms, but every
        fire is submitted (fire == commit), so the scale-up cooldown timestamp
        and cross-direction reset advance exactly as pre-RQ2 — RQ1 behavior is
        preserved byte-for-byte. RQ1 decision-logging happens in the mediator.
        """
        alerts: list[ComputeAlert | DataAlert] = []

        if allow_compute:
            v = self.evaluate_compute_scale_up(
                ds, dynamic_compute_count, peer_ds, blocked=False)
            if v.fired:
                self.commit_compute_scale_up()
                alerts.append(ComputeAlert(lan=lan, network_id=network_id))

        if allow_storage:
            v = self.evaluate_storage_scale_up(
                ds, dynamic_storage_count, blocked=False)
            if v.fired:
                self.commit_storage_scale_up()
                alerts.append(DataAlert(
                    lan=lan,
                    network_id=network_id,
                    rs_name=f"rs_net{lan}",
                    primary_container=f"edge_storage_server_n{lan}",
                ))

        return alerts

    def _compute_peer_relief(self, peer_ds: DomainSummary | None) -> tuple[float, float | None]:
        if peer_ds is None:
            return 0.0, None

        peer_score = self.degradation_score(
            peer_ds.average_cpu_percent, self.compute_latency_signal(peer_ds),
            _W_CPU, _W_T_PROC,
            _CPU_FLOOR, _CPU_SPAN,
            _T_PROC_FLOOR, _T_PROC_SPAN,
        )
        if peer_score <= _SCALEUP_COMPUTE_PEER_HEALTH_THRESHOLD:
            return _SCALEUP_COMPUTE_PEER_RELIEF, peer_score
        return 0.0, peer_score

    def evaluate_compute_scale_up(self, ds: DomainSummary,
                                  dynamic_compute_count: int,
                                  peer_ds: DomainSummary | None = None,
                                  *,
                                  blocked: bool = False) -> ScaleUpVerdict:
        """Evaluate compute scale-up for one window → ScaleUpVerdict.

        Identical evaluation mechanics in every RQ2 arm (D2): evidence is always
        computed; the sliding window advances only when eligible (not blocked /
        at cap / in scale-up cooldown). On fire only the scale-DOWN-cooldown
        timestamp and the scale-up window clear are set here — the scale-UP
        cooldown timestamp is set by ``commit_compute_scale_up`` on submission.
        """
        compute_latency_ms = self.compute_latency_signal(ds)
        compute_score = self.degradation_score(
            ds.average_cpu_percent, compute_latency_ms,
            _W_CPU, _W_T_PROC,
            _CPU_FLOOR, _CPU_SPAN,
            _T_PROC_FLOOR, _T_PROC_SPAN,
        )
        base_threshold = min(
            _SCALEUP_COMPUTE_BASE_THRESHOLD
            + dynamic_compute_count * _SCALEUP_COMPUTE_THRESHOLD_INCREMENT,
            _SCALEUP_COMPUTE_MAX_THRESHOLD,
        )
        peer_relief, peer_score = self._compute_peer_relief(peer_ds)
        effective_threshold = min(
            base_threshold + peer_relief,
            _SCALEUP_COMPUTE_MAX_THRESHOLD,
        )
        score_norm = min(1.0, compute_score / (_W_CPU + _W_T_PROC))

        if (blocked
                or dynamic_compute_count >= _MAX_DYNAMIC_COMPUTE
                or self.compute_scaleup_cooldown_remaining() > 0):
            if dynamic_compute_count >= _MAX_DYNAMIC_COMPUTE:
                logger.debug(
                    "[scale-up] compute cap reached (%d/%d) — not eligible",
                    dynamic_compute_count, _MAX_DYNAMIC_COMPUTE,
                )
            return ScaleUpVerdict(
                tier="compute", eligible=False, fired=False,
                score_norm=score_norm, score=compute_score,
                threshold=effective_threshold,
            )

        above = compute_score >= effective_threshold
        self._scale_up_compute_window.append(above)
        peer_score_display = "n/a" if peer_score is None else f"{peer_score:.2f}"
        window_hits = sum(self._scale_up_compute_window)
        window_size = len(self._scale_up_compute_window)
        logger.debug(
            "[scale-up] compute score=%.2f (τ_eff=%.2f, τ_base=%.2f, peer_relief=%.2f, peer_score=%s, dyn=%d) "
            "cpu=%.1f%% T_proc=%.1fms T_proc_signal=%.1fms window=%d/%d",
            compute_score, effective_threshold, base_threshold, peer_relief,
            peer_score_display, dynamic_compute_count,
            ds.average_cpu_percent, ds.avg_time_proc_ms, compute_latency_ms,
            window_hits, window_size,
        )
        if window_hits >= _SCALE_UP_REQUIRED:
            logger.info(
                "[scale-up] compute fired: %d/%d windows ≥ %.2f "
                "(τ_eff=%.2f, τ_base=%.2f, peer_relief=%.2f, peer_score=%s, dyn=%d, last score=%.2f, cpu=%.1f%%, T_proc=%.1fms)",
                window_hits, window_size, effective_threshold,
                effective_threshold, base_threshold, peer_relief,
                peer_score_display, dynamic_compute_count,
                compute_score, ds.average_cpu_percent, ds.avg_time_proc_ms,
            )
            self._last_compute_scale_up_fired_ts = time.monotonic()
            self._scale_up_compute_window.clear()
            return ScaleUpVerdict(
                tier="compute", eligible=True, fired=True,
                score_norm=score_norm, score=compute_score,
                threshold=effective_threshold,
            )
        return ScaleUpVerdict(
            tier="compute", eligible=True, fired=False,
            score_norm=score_norm, score=compute_score,
            threshold=effective_threshold,
        )

    def evaluate_storage_scale_up(self, ds: DomainSummary,
                                  dynamic_storage_count: int,
                                  *,
                                  blocked: bool = False) -> ScaleUpVerdict:
        """Evaluate storage scale-up for one window → ScaleUpVerdict.

        Same contract as ``evaluate_compute_scale_up`` (D2): evidence always
        computed, window advances only when eligible, fire sets the fire
        timestamp (D7) and clears the scale-up window (commit is handled by the
        mediator / dual facade).
        """
        storage_latency_ms = self.storage_latency_signal(ds)
        storage_score = self.degradation_score(
            ds.avg_storage_cpu_percent, storage_latency_ms,
            _W_STORAGE_CPU, _W_T_DB,
            _STORAGE_CPU_FLOOR, _STORAGE_CPU_SPAN,
            _T_DB_FLOOR, _T_DB_SPAN,
        )
        # Adaptive threshold: diminishing increment per node (halves each node, floored)
        cumulative = sum(
            max(_SCALEUP_STORAGE_THRESHOLD_INCREMENT * 0.5 ** i,
                _SCALEUP_STORAGE_MIN_INCREMENT)
            for i in range(dynamic_storage_count)
        )
        effective_threshold = min(
            _SCALEUP_STORAGE_BASE_THRESHOLD + cumulative,
            _SCALEUP_STORAGE_MAX_THRESHOLD,
        )
        score_norm = min(1.0, storage_score / (_W_STORAGE_CPU + _W_T_DB))

        if (blocked
                or dynamic_storage_count >= _MAX_DYNAMIC_STORAGE
                or self.storage_scaleup_cooldown_remaining() > 0):
            if dynamic_storage_count >= _MAX_DYNAMIC_STORAGE:
                logger.debug(
                    "[scale-up] storage cap reached (%d/%d) — not eligible",
                    dynamic_storage_count, _MAX_DYNAMIC_STORAGE,
                )
            return ScaleUpVerdict(
                tier="storage", eligible=False, fired=False,
                score_norm=score_norm, score=storage_score,
                threshold=effective_threshold,
            )

        above = storage_score >= effective_threshold
        self._scale_up_storage_window.append(above)
        logger.debug(
            "[scale-up] storage score=%.2f (τ_eff=%.2f, base=%.2f +Σdim(%d)=%.3f) "
            "cpu_s=%.1f%% T_db_avg=%.1fms T_db_p95=%.1fms signal=%.1fms  window=%d/%d",
            storage_score, effective_threshold,
            _SCALEUP_STORAGE_BASE_THRESHOLD, dynamic_storage_count, cumulative,
            ds.avg_storage_cpu_percent, ds.avg_time_db_ms, ds.p95_time_db_ms, storage_latency_ms,
            sum(self._scale_up_storage_window), len(self._scale_up_storage_window),
        )
        if sum(self._scale_up_storage_window) >= _SCALEUP_STORAGE_REQUIRED:
            logger.info(
                "[scale-up] storage fired: %d/%d windows ≥ %.2f "
                "(eff_τ=%.2f, dyn_nodes=%d, Σdim=%.3f, last score=%.2f, cpu_s=%.1f%%, T_db_avg=%.1fms, T_db_p95=%.1fms)",
                sum(self._scale_up_storage_window),
                len(self._scale_up_storage_window),
                effective_threshold, effective_threshold, dynamic_storage_count,
                cumulative, storage_score,
                ds.avg_storage_cpu_percent, ds.avg_time_db_ms, ds.p95_time_db_ms,
            )
            self._last_storage_scale_up_fired_ts = time.monotonic()
            self._scale_up_storage_window.clear()
            return ScaleUpVerdict(
                tier="storage", eligible=True, fired=True,
                score_norm=score_norm, score=storage_score,
                threshold=effective_threshold,
            )
        return ScaleUpVerdict(
            tier="storage", eligible=True, fired=False,
            score_norm=score_norm, score=storage_score,
            threshold=effective_threshold,
        )

    def commit_compute_scale_up(self) -> None:
        """Commit a compute scale-up — only on actual submission.

        Advances the scale-UP cooldown timestamp and performs the
        cross-direction scale-down window reset (D2).
        """
        self._last_compute_scale_up_ts = time.monotonic()
        self.clear_scale_down_compute_window()

    def commit_storage_scale_up(self) -> None:
        """Commit a storage scale-up — only on actual submission.

        Advances the scale-UP cooldown timestamp and performs the
        cross-direction scale-down window reset (D2).
        """
        self._last_storage_scale_up_ts = time.monotonic()
        self.clear_scale_down_storage_window()

    def evaluate_scale_down_compute(self, ds: DomainSummary) -> bool:
        """Returns True if compute underutilisation threshold met.

        Uses the same latency signal as scale-up (compute_latency_signal) so
        the ceiling / below-TAU checks are consistent with the decision signal.
        """
        proc_signal = self.compute_latency_signal(ds)
        if proc_signal > _SCALE_DOWN_PROC_TIMEOUT_CEILING_MS:
            logger.debug(
                "[scale-down] compute eval: proc=%.1f exceeds ceiling (%.0f) — window skipped",
                proc_signal, _SCALE_DOWN_PROC_TIMEOUT_CEILING_MS,
            )
            return False

        below = (ds.average_cpu_percent < _TAU_CPU_DOWN
                 and proc_signal < _TAU_PROC_DOWN_MS)
        self._scale_down_compute_window.append(below)
        hits = sum(self._scale_down_compute_window)
        armed = hits >= _SCALE_DOWN_COMPUTE_REQUIRED

        logger.debug(
            "[scale-down] compute eval: cpu=%.1f/%.0f proc=%.1f/%.1f below=%s hits=%d/%d armed=%s",
            ds.average_cpu_percent, _TAU_CPU_DOWN,
            proc_signal, _TAU_PROC_DOWN_MS,
            below, hits, _SCALE_DOWN_COMPUTE_REQUIRED, armed,
        )
        if armed and not self._prev_scale_down_compute_armed:
            logger.info(
                "[scale-down] compute ARMED: hits=%d/%d cpu=%.1f proc=%.1f",
                hits, _SCALE_DOWN_COMPUTE_REQUIRED,
                ds.average_cpu_percent, proc_signal,
            )
        self._prev_scale_down_compute_armed = armed
        return armed

    def evaluate_scale_down_storage(self, ds: DomainSummary) -> bool:
        """Returns True if storage underutilisation threshold met.

        Uses the same latency signal as scale-up (storage_latency_signal) so
        the ceiling / below-TAU checks are consistent with the decision signal.
        """
        db_signal = self.storage_latency_signal(ds)
        if db_signal > _SCALE_DOWN_DB_TIMEOUT_CEILING_MS:
            logger.info(
                "[scale-down] storage eval: db=%.1f exceeds ceiling (%.0f) — window skipped",
                db_signal, _SCALE_DOWN_DB_TIMEOUT_CEILING_MS,
            )
            return False

        # CPU-aware gate (control/RQ2+): STORAGE_SCALE_DOWN_CPU_AWARE=1 also requires CPU idle
        # — median latency can't protect the plateau (mean_vs_median_signal_finding.md §6).
        cpu_ok = (not _STORAGE_SCALE_DOWN_CPU_AWARE
                  or ds.avg_storage_cpu_percent < _TAU_STORAGE_CPU_DOWN)
        below = db_signal < _TAU_DB_DOWN_MS and cpu_ok
        self._scale_down_storage_window.append(below)
        hits = sum(self._scale_down_storage_window)
        armed = hits >= _SCALE_DOWN_STORAGE_REQUIRED

        logger.info(
            "[scale-down] storage eval: stCpu=%.1f/%.0f db=%.1f/%.0f below=%s hits=%d/%d armed=%s",
            ds.avg_storage_cpu_percent, _TAU_STORAGE_CPU_DOWN,
            db_signal, _TAU_DB_DOWN_MS,
            below, hits, _SCALE_DOWN_STORAGE_REQUIRED, armed,
        )
        if armed and not self._prev_scale_down_storage_armed:
            logger.info(
                "[scale-down] storage ARMED: hits=%d/%d stCpu=%.1f db=%.1f",
                hits, _SCALE_DOWN_STORAGE_REQUIRED,
                ds.avg_storage_cpu_percent, db_signal,
            )
        self._prev_scale_down_storage_armed = armed
        return armed

    def clear_scale_down_compute_window(self) -> None:
        self._scale_down_compute_window.clear()

    def clear_scale_down_storage_window(self) -> None:
        self._scale_down_storage_window.clear()
