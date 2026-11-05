"""readiness_gate.py — RQ3 compute-backend readiness gate.

Gates ``VIP_SERVER`` pool admission on a verified application-readiness event
(``/ready``), under two switchable propagation modes:

  - ``direct``:    RQ3 v2 (approach A, event-driven): admit on the edge's
                   ``app_ready`` control event (no probe before admission);
                   /ready is used only for the post-admission identity check,
                   the event-absence safety net (``READINESS_EVENT_FALLBACK_S``),
                   and abandonment detection.
  - ``discovery``: probe only on the ``DISCOVERY_POLL_INTERVAL_S`` cadence;
                   admit when a discovery pass observes 200 (periodic
                   discovery).

Optional RQ3 soundness sub-mode (``READINESS_WAKE_VERIFY_MODE=probe``, default
off, ``direct`` only): the ``app_ready`` event wakes an immediate probe instead
of admitting; admission happens only after a successful /ready probe
(``admit_source="wake_verify_probe"``) — the event is a wake-up, not evidence.

The gate is active only when ``READINESS_PROPAGATION != "off"`` (constructed
in ``main_n*.py``). When off, no gate exists and Thread 3 registers backends
immediately (pre-RQ3 behavior, byte-identical).

Owns a native worker daemon thread (blocking HTTP probes). See
``docs/telemetry-and-scaling.md``.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

logger = logging.getLogger(__name__)


@dataclass
class PendingBackend:
    """A spawned-but-not-yet-admitted compute backend awaiting readiness."""
    mac: str
    ip: str
    name: str
    lan: int
    network_id: str                 # controller LAN id: "lan1" / "lan2"
    ready_port: int = 5000          # EDGE_READY_PORT (must equal edge BIND_PORT)
    spawn_started_wall_s: float = 0.0     # time.time() at spawn start
    spawn_complete_wall_s: float = 0.0    # time.time() when add_edge_server returned
    spawn_started_mono_s: float = 0.0     # time.monotonic() at spawn start (timing line)
    probe_first_wall_s: float | None = None
    app_ready_wall_s: float | None = None
    admitted_wall_s: float | None = None
    # RQ3 v2 admit route: "event" (direct app_ready), "probe_fallback"
    # (event-absence safety net, direct) or "probe" (discovery scan).
    admit_source: str = "probe"
    # wake_verify: wall-clock of the app_ready event that woke the probe; admission
    # still requires a successful /ready probe (admit_source="wake_verify_probe").
    wake_verify_wall_s: float | None = None
    # Set when teardown is enqueued so a late app_ready event cannot admit a
    # backend that is being abandoned.
    abandoned: bool = False


class ReadinessGate:
    """Probe pending compute backends and admit them on verified readiness.

    Native worker daemon thread (blocking HTTP probes). ``direct`` mode wakes on
    enqueue and re-probes on the retry cadence; ``discovery`` mode wakes on the
    scan cadence and ignores notify wakes.
    """

    def __init__(
        self,
        propagation: str,
        probe_timeout_s: float,
        probe_max_s: float,
        probe_retry_s: float,
        discovery_interval_s: float,
        ready_port: int,
        admission_log_path: str,
        on_admit: Callable[[PendingBackend], None],
        on_abandon: Callable[[PendingBackend], None],
        event_fallback_s: float = 5.0,
        wake_verify_mode: str = "off",
    ) -> None:
        self._propagation = propagation            # "direct" | "discovery"
        self._probe_timeout_s = probe_timeout_s
        self._probe_max_s = probe_max_s
        self._probe_retry_s = probe_retry_s
        self._discovery_interval_s = discovery_interval_s
        self._ready_port = ready_port
        self._admission_log_path = admission_log_path
        self._on_admit = on_admit
        self._on_abandon = on_abandon
        self._event_fallback_s = event_fallback_s
        # RQ3 soundness (optional arm): "probe" defers event-driven admission
        # until a successful /ready probe.
        self._wake_verify_mode = wake_verify_mode == "probe"

        self._pending: list[PendingBackend] = []
        self._confirm: list[PendingBackend] = []   # post-admission identity check
        # app_ready events that arrived before the spawn's enqueue (startup race) —
        # MAC -> arrival wall clock; replayed by enqueue within the fallback window.
        self._late_events: dict[str, float] = {}
        self._wake = threading.Condition()
        self._last_scan_mono: float = float("-inf")
        self._csv_lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._worker, name="readiness-gate", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "[readiness] gate started propagation=%s probe_timeout=%.1fs "
            "probe_max=%.1fs probe_retry=%.1fs discovery=%.1fs event_fallback=%.1fs "
            "wake_verify=%s ready_port=%d",
            self._propagation, self._probe_timeout_s, self._probe_max_s,
            self._probe_retry_s, self._discovery_interval_s,
            self._event_fallback_s, "probe" if self._wake_verify_mode else "off",
            self._ready_port,
        )

    def enqueue(self, pb: PendingBackend) -> None:
        replay = False
        with self._wake:
            self._pending.append(pb)
            if self._propagation == "direct":
                self._wake.notify_all()
                # Replay an app_ready that arrived before this enqueue (the edge can
                # flip ready before Thread 3 finishes registration), within the window.
                late_ts = self._late_events.get(pb.mac)
                if late_ts is not None and time.time() - late_ts <= self._event_fallback_s:
                    self._late_events.pop(pb.mac, None)
                    replay = True
        if replay:
            self.admit_on_event(pb.mac)
        logger.info(
            "[readiness] enqueued pending backend name=%s mac=%s ip=%s lan=%d",
            pb.name, pb.mac, pb.ip, pb.lan,
        )

    def _prune_late_events(self) -> None:
        cutoff = time.time() - self._event_fallback_s
        stale = [mac for mac, ts in self._late_events.items() if ts < cutoff]
        for mac in stale:
            self._late_events.pop(mac, None)

    def admit_on_event(self, mac: str) -> bool:
        """Admit a pending backend on its ``app_ready`` control event (direct).

        Thread-safe (called from the Thread 2 control-event handler). Finds the
        pending backend by MAC, records event-driven admission
        (``admit_source="event"``), and enqueues a post-admission
        identity-confirmation probe for the worker thread. Returns True if a
        pending backend consumed the event (admitted in plain direct mode;
        probe woken in ``wake_verify`` mode). A ``discovery`` run ignores app_ready
        events (the cadence is the treatment). If the event arrives before the
        spawn's ``enqueue`` (a startup race), the MAC is buffered for a short
        window so ``enqueue`` can replay it. ``_on_admit`` is non-blocking
        in-memory registration; all gate state mutations serialize on
        ``_wake`` (enqueue / admit_on_event / worker scan). In
        ``wake_verify`` mode the event only wakes the probe; admission is
        deferred until the probe succeeds.
        """
        if self._propagation != "direct":
            return False
        if self._wake_verify_mode:
            with self._wake:
                for pb in self._pending:
                    if (pb.mac == mac and pb.admitted_wall_s is None
                            and not pb.abandoned):
                        if pb.wake_verify_wall_s is None:
                            pb.wake_verify_wall_s = time.time()
                        self._wake.notify_all()
                        logger.info(
                            "[readiness] wake-verify: event woke probe for "
                            "name=%s mac=%s (admission deferred to probe)",
                            pb.name, pb.mac,
                        )
                        return True
                self._late_events[mac] = time.time()
                self._prune_late_events()
                return False
        with self._wake:
            for pb in self._pending:
                if (pb.mac == mac and pb.admitted_wall_s is None
                        and not pb.abandoned):
                    now = time.time()
                    if pb.app_ready_wall_s is None:
                        pb.app_ready_wall_s = now
                    pb.admitted_wall_s = now
                    pb.admit_source = "event"
                    try:
                        self._on_admit(pb)
                    except Exception:
                        logger.exception(
                            "[readiness] on_admit failed for %s — deferring to probe",
                            pb.name,
                        )
                        pb.admitted_wall_s = None
                        return False
                    self._write_admission_row(pb, result="admitted")
                    self._pending = [p for p in self._pending if p.mac != mac]
                    self._confirm.append(pb)   # post-admission identity check
                    logger.info(
                        "[readiness] event-driven admission name=%s mac=%s",
                        pb.name, pb.mac,
                    )
                    return True
            self._late_events[mac] = time.time()
            self._prune_late_events()
            return False

    def _worker(self) -> None:
        while True:
            try:
                with self._wake:
                    if self._propagation == "direct":
                        # Wake on enqueue (notify) or re-probe on the retry cadence;
                        # idle wake-ups with an empty registry are harmless (~1/s).
                        self._wake.wait(self._probe_retry_s)
                    else:
                        # Discovery: wait on the cadence; ignore notify wakes.
                        self._wake.wait(self._discovery_interval_s)
                        while (time.monotonic() - self._last_scan_mono
                               < self._discovery_interval_s):
                            remaining = (self._discovery_interval_s
                                         - (time.monotonic()
                                            - self._last_scan_mono))
                            self._wake.wait(max(0.0, remaining))
                    pending = list(self._pending)
                self._scan(pending)
                # Stamp AFTER the scan (cadence measured from the END of the previous
                # scan) so a slow serial scan cannot collapse into back-to-back scans.
                self._last_scan_mono = time.monotonic()
            except Exception:
                logger.exception("[readiness] worker pass failed — continuing")

    def _scan(self, pending: list[PendingBackend]) -> None:
        now_wall = time.time()
        admitted: list[PendingBackend] = []
        abandoned: list[PendingBackend] = []
        for pb in pending:
            if pb.admitted_wall_s is not None or pb.abandoned:
                continue
            if now_wall - pb.spawn_complete_wall_s > self._probe_max_s:
                # Only drop from the registry once teardown was successfully enqueued
                # (on_abandon); otherwise retry next pass.
                if self._try_abandon(pb):
                    abandoned.append(pb)
                continue
            if (self._propagation == "direct"
                    and pb.wake_verify_wall_s is None
                    and now_wall - pb.spawn_complete_wall_s
                    < self._event_fallback_s):
                # Event-absence safety net: in direct mode let the app_ready event arrive
                # before probing; past the grace the probe is the fallback. wake_verify bypasses it.
                continue
            self._probe_one(pb)
            if pb.app_ready_wall_s is not None and pb.admitted_wall_s is None:
                pb.admitted_wall_s = time.time()
                if self._wake_verify_mode and pb.wake_verify_wall_s is not None:
                    pb.admit_source = "wake_verify_probe"
                    logger.info(
                        "[readiness] wake_verify=pass name=%s mac=%s",
                        pb.name, pb.mac,
                    )
                else:
                    pb.admit_source = ("probe_fallback" if self._propagation == "direct"
                                       else "probe")
                    if self._wake_verify_mode:
                        logger.info(
                            "[readiness] wake_verify=fallback name=%s mac=%s",
                            pb.name, pb.mac,
                        )
                try:
                    self._on_admit(pb)
                except Exception:
                    logger.exception(
                        "[readiness] on_admit failed for %s — retrying next pass",
                        pb.name,
                    )
                    pb.admitted_wall_s = None
                    continue
                # Write the admitted row immediately after registration so a later
                # removal failure cannot drop the backend without a row.
                self._write_admission_row(pb, result="admitted")
                admitted.append(pb)

        self._process_confirmations(now_wall)

        if admitted or abandoned:
            admitted_macs = {pb.mac for pb in admitted}
            abandoned_macs = {pb.mac for pb in abandoned}
            with self._wake:
                self._pending = [
                    p for p in self._pending
                    if p.mac not in admitted_macs and p.mac not in abandoned_macs
                ]

    def _process_confirmations(self, now_wall: float) -> None:
        """Post-admission identity check for event-driven admissions (direct).

        The worker probes /ready once per event-admitted backend and logs
        identity OK/violation; a non-200 is reported (not a gate) since the
        flag was already true when the event fired. Blocking HTTP is safe in
        the worker thread.
        """
        with self._wake:
            confirm = list(self._confirm)
            self._confirm = []
        for pb in confirm:
            url = f"http://{pb.ip}:{pb.ready_port}/ready"
            try:
                resp = requests.get(url, timeout=self._probe_timeout_s)
                ok = resp.status_code == 200
            except Exception:
                ok = False
            if ok:
                logger.info(
                    "[readiness] post-admission identity OK name=%s mac=%s",
                    pb.name, pb.mac,
                )
            else:
                logger.warning(
                    "[readiness] post-admission identity CHECK VIOLATION "
                    "name=%s mac=%s (event admitted but /ready != 200)",
                    pb.name, pb.mac,
                )

    def _try_abandon(self, pb: PendingBackend) -> bool:
        """Enqueue teardown + write the abandoned row; False → retry next pass."""
        # Mark first so a late app_ready event cannot admit this backend while
        # its teardown is being enqueued (admit_on_event checks pb.abandoned).
        pb.abandoned = True
        try:
            self._on_abandon(pb)
        except Exception:
            logger.exception("[readiness] on_abandon failed for %s — retrying", pb.name)
            return False
        # A never-admitted backend must not report app_ready (§2.4 contract).
        pb.app_ready_wall_s = None
        self._write_admission_row(pb, result="abandoned")
        logger.error("[readiness] abandoned never-ready backend name=%s mac=%s",
                     pb.name, pb.mac)
        return True

    def _probe_one(self, pb: PendingBackend) -> None:
        if pb.probe_first_wall_s is None:
            pb.probe_first_wall_s = time.time()
        url = f"http://{pb.ip}:{pb.ready_port}/ready"
        try:
            resp = requests.get(url, timeout=self._probe_timeout_s)
            if resp.status_code == 200:
                if pb.app_ready_wall_s is None:   # first observation of readiness
                    pb.app_ready_wall_s = time.time()
                logger.info("[readiness] /ready OK name=%s ip=%s", pb.name, pb.ip)
            else:
                logger.debug("[readiness] /ready %d name=%s", resp.status_code, pb.name)
        except Exception as exc:  # connection refused / timeout — not ready yet
            logger.debug("[readiness] /ready probe failed name=%s err=%s", pb.name, exc)

    _ADMISSION_COLUMNS = [
        "ts", "network_id", "lan", "container", "mac", "ip", "mode",
        "result", "spawn_started_ts", "spawn_complete_ts", "probe_first_ts",
        "app_ready_ts", "admitted_ts", "admit_source",
    ]

    def _write_admission_row(self, pb: PendingBackend, *, result: str) -> None:
        row = [
            f"{time.time():.3f}",
            pb.network_id,
            str(pb.lan),
            pb.name,
            pb.mac,
            pb.ip,
            self._propagation,
            result,
            _fmt_ts(pb.spawn_started_wall_s),
            _fmt_ts(pb.spawn_complete_wall_s),
            _fmt_ts(pb.probe_first_wall_s),
            _fmt_ts(pb.app_ready_wall_s),
            _fmt_ts(pb.admitted_wall_s),
            pb.admit_source,
        ]
        try:
            with self._csv_lock:
                write_header = (
                    not os.path.exists(self._admission_log_path)
                    or os.path.getsize(self._admission_log_path) == 0
                )
                with open(self._admission_log_path, "a", newline="") as fh:
                    writer = csv.writer(fh)
                    if write_header:
                        writer.writerow(self._ADMISSION_COLUMNS)
                    writer.writerow(row)
        except Exception:
            logger.exception("[readiness] failed to write admission log row for %s",
                             pb.name)


def _fmt_ts(v: float | None) -> str:
    return f"{v:.3f}" if v is not None else ""
