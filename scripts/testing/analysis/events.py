"""Typed regex parsers for SDN controller log files → list[ElasticityEvent]."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ElasticityEvent:
    ts: float
    lan: str
    kind: str      # alert | spawn_start | spawn_done | scale_down | busy | cooldown | armed | down_eval
    tier: str      # compute | storage
    container: Optional[str] = None
    fields: Optional[dict] = field(default=None)


_RE_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")

_RE_ALERT = re.compile(
    r"alert submitted .*?(ComputeAlert|DataAlert)\(lan=(\d+)"
)
_RE_SPAWN_START = re.compile(
    r"\[elasticity\] (compute|data|storage): spawning (\S+) on LAN (\d+)"
)
_RE_SPAWN_DONE = re.compile(
    r"\[elasticity\] (compute|data|storage): (\S+) online"
)
_RE_COOLDOWN = re.compile(
    r"\[scale-down\] (compute|storage) within (\d+)s cooldown"
)
_RE_BUSY = re.compile(r"\[scale-down\] elasticity manager is busy")
_RE_ARMED = re.compile(
    r"\[scale-down\] (compute|storage) ARMED: hits=(\d+)/(\d+)"
)
_RE_DOWN_EVAL = re.compile(
    r"\[scale-down\] (compute|storage) eval: "
    r"(?:cpu|stCpu)=([\d.]+)/[\d.]+ "
    r"(?:proc|db)=([\d.]+)/[\d.]+ "
    r"below=(\w+) hits=(\d+)/(\d+) armed=(\w+)"
)
_RE_DOWN_CEILING = re.compile(
    r"\[scale-down\] (compute|storage) eval: "
    r"(proc|db)=([\d.]+) exceeds ceiling \(([\d.]+)\) \u2014 window skipped"
)


def _parse_ts(line: str) -> float:
    """Extract a Unix-epoch float from a log line timestamp, or 0.0.

    Controller logs use Python's ``%(asctime)s`` which produces
    comma-separated milliseconds in local time (e.g.
    ``2026-06-14 00:38:25,822``).  We parse as UTC because the
    controller and aggregator both use ``time.time()`` (UTC epoch)
    for all other timestamps.
    """
    import calendar
    import time as _time
    m = _RE_TIMESTAMP.match(line)
    if not m:
        return 0.0
    ts_str = m.group(1).replace(",", ".")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return calendar.timegm(_time.strptime(ts_str, fmt))
        except ValueError:
            continue
    return 0.0


def parse_log_file(path: Path, lan: str) -> list[ElasticityEvent]:
    """Parse a single controller log file and return all recognised events."""
    events: list[ElasticityEvent] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events

    for line in lines:
        ts = _parse_ts(line)

        m = _RE_ALERT.search(line)
        if m:
            kind_map = {"ComputeAlert": "compute", "DataAlert": "storage"}
            events.append(ElasticityEvent(
                ts=ts, lan=lan,
                kind="alert",
                tier=kind_map.get(m.group(1), "compute"),
            ))
            continue

        m = _RE_SPAWN_START.search(line)
        if m:
            tier_raw = m.group(1)
            tier = "storage" if tier_raw in ("data", "storage") else "compute"
            events.append(ElasticityEvent(
                ts=ts, lan=m.group(3),
                kind="spawn_start",
                tier=tier,
                container=m.group(2),
            ))
            continue

        m = _RE_SPAWN_DONE.search(line)
        if m:
            tier_raw = m.group(1)
            tier = "storage" if tier_raw in ("data", "storage") else "compute"
            events.append(ElasticityEvent(
                ts=ts, lan=lan,
                kind="spawn_done",
                tier=tier,
                container=m.group(2),
            ))
            continue

        m = _RE_DOWN_EVAL.search(line)
        if m:
            events.append(ElasticityEvent(
                ts=ts, lan=lan,
                kind="down_eval",
                tier=m.group(1),
                fields={
                    "metric_val": float(m.group(2)),
                    "threshold":  float(m.group(3)),
                    "below":      m.group(4).lower() == "true",
                    "hits":       int(m.group(5)),
                    "required":   int(m.group(6)),
                    "armed":      m.group(7).lower() == "true",
                },
            ))
            continue

        m = _RE_DOWN_CEILING.search(line)
        if m:
            events.append(ElasticityEvent(
                ts=ts, lan=lan,
                kind="down_eval_ceiling_skip",
                tier=m.group(1),
                fields={"metric": m.group(2), "val": float(m.group(3)),
                        "ceiling": float(m.group(4))},
            ))
            continue

        m = _RE_ARMED.search(line)
        if m:
            events.append(ElasticityEvent(
                ts=ts, lan=lan,
                kind="armed",
                tier=m.group(1),
                fields={"hits": int(m.group(2)), "required": int(m.group(3))},
            ))
            continue

        m = _RE_COOLDOWN.search(line)
        if m:
            events.append(ElasticityEvent(
                ts=ts, lan=lan,
                kind="cooldown",
                tier=m.group(1),
                fields={"remaining_s": int(m.group(2))},
            ))
            continue

        if _RE_BUSY.search(line):
            events.append(ElasticityEvent(ts=ts, lan=lan, kind="busy", tier=""))

    return events


def parse_logs(paths: list[Path]) -> list[ElasticityEvent]:
    """Parse multiple log files (one per LAN) and merge by timestamp."""
    all_events: list[ElasticityEvent] = []
    for path in paths:
        lan = ""
        # Infer LAN from filename convention: controller_lan1.log → "1"
        stem = path.stem
        if "lan" in stem:
            lan = stem.split("lan")[-1].split(".")[0]
        all_events.extend(parse_log_file(path, lan))
    all_events.sort(key=lambda e: e.ts)
    return all_events
