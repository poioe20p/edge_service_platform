#!/usr/bin/env python3
"""
collect_resource_stats.py

Subscribes to both LAN aggregator ZMQ PUB sockets and writes domain-level
CPU / RAM summaries to a CSV file, one row per LAN per aggregation window.

Designed to run as a background process launched by run_experiment.sh before
the traffic generator starts and stopped (SIGTERM) after it finishes.

Usage:
    python3 collect_resource_stats.py \
      [--lan1-pub tcp://10.0.0.5:5556] \
      [--lan2-pub tcp://10.0.1.5:5556] \
      [--output metrics/resource_stats.csv]
"""

import argparse
import csv
import json
import logging
import os
import signal
import statistics
import subprocess
import time

import zmq

from tier1_stats import TIER1_ALL_COLUMNS, build_tier1_row, peer_lan

# Trimmed main view: raw inputs plus the derived helpers elasticity reasoning
# needs, with Tier 1 status visible without opening the debug CSV.
# avg_time_proc_ms / avg_time_db_ms hold the aggregator's MEDIAN (a historical
# misnomer); storage_latency_signal_ms mirrors the controller's median signal.
MAIN_FIELDNAMES = [
    "timestamp",
    "phase",
    "network_id",
    "window_end",
    "total_requests",
    "average_cpu_percent",
    "avg_time_proc_ms",
    "avg_storage_cpu_percent",
    "avg_time_db_ms",
    "p95_time_db_ms",
    "storage_latency_signal_ms",
    "server_count",
    "storage_count",
    "avg_repl_lag_ms",
    "coord_state_owner_lan",
    "tier1_lifecycle_active_count",
    "conntrack_entries_n1",
    "conntrack_entries_n2",
]

DEBUG_FIELDNAMES = [
    "timestamp",
    "phase",
    "network_id",
    "window_end",
    "total_requests",
    "median_cpu_percent",
    "median_ram_used_mb",
    "median_storage_cpu_percent",
    "median_storage_ram_used_mb",
    "median_time_proc_ms",
    "median_time_db_ms",
    "median_time_total_ms",
    "server_count",
    "storage_count",
    "avg_repl_lag_ms",
    "avg_time_db_read_ms",
    "avg_time_db_write_ms",
    "avg_time_db_cmd_count",
    "consumed_at",
] + TIER1_ALL_COLUMNS

PER_NODE_FIELDNAMES = [
    "timestamp", "phase", "network_id", "window_end",
    "server_id", "role", "request_count",
    "cpu_percent", "ram_used_mb",
    "avg_time_proc_ms", "avg_time_db_ms",
    "avg_time_db_read_ms", "avg_time_db_write_ms", "avg_time_db_cmd_count",
    "avg_repl_lag_s", "member_state", "last_report_ts",
]

_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _mean_or_none(values):
    clean = [v for v in values if v is not None]
    return statistics.mean(clean) if clean else None


def _domain_avg_repl_lag_ms(storage: dict) -> float:
    """Compute mean replication lag in milliseconds across storage nodes."""
    lags = [s.get("avg_repl_lag_s") for s in storage.values()
            if s.get("avg_repl_lag_s") is not None]
    return (statistics.mean(lags) * 1000.0) if lags else 0.0


def _domain_avg_cpu(servers: dict) -> float | None:
    """Compute mean CPU percent across servers."""
    vals = [s.get("avg_cpu_percent") for s in servers.values()
            if s.get("avg_cpu_percent") is not None]
    return statistics.mean(vals) if vals else None


def _domain_p95(values: list[float]) -> float | None:
    """P95 via linear interpolation (same method as metrics_stats.py)."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    rank = 95.0 / 100.0 * (n - 1)
    lo = int(rank)
    hi = lo + 1
    if hi >= n:
        return s[-1]
    frac = rank - lo
    return s[lo] + frac * (s[hi] - s[lo])


def _domain_p95_time_db_ms(storage: dict) -> float | None:
    """P95 of per-storage-node avg_time_db_ms across the domain."""
    vals = []
    for s in storage.values():
        t = s.get("avg_time_db_ms")
        if t is not None:
            vals.append(float(t))
    return _domain_p95(vals) if vals else None


def _storage_latency_signal_ms(domain: dict, storage: dict) -> float | None:
    """Composite storage latency signal — mirrors the controller's decision signal.

    Median mode (control group / RQ2+): the aggregator's domain-level
    median_time_db_ms — the exact value scaling_policy.storage_latency_signal
    uses with LATENCY_SIGNAL_MODE=median. Legacy fallback (pre-median
    aggregators, RQ1 era): mean of per-node avg_time_db_ms.
    """
    med = domain.get("median_time_db_ms")
    if med is not None:
        return float(med)
    vals = [float(s.get("avg_time_db_ms", 0)) for s in storage.values()
            if s.get("avg_time_db_ms") is not None]
    return statistics.mean(vals) if vals else None


def _emit_per_node_rows(writer, summary: dict, phase: str, ts: str,
                        network_id: str, window_end) -> None:
    """Write one row per compute and storage container to the per-node CSV."""
    for sid, s in summary.get("servers", {}).items():
        writer.writerow({
            "timestamp": ts, "phase": phase, "network_id": network_id,
            "window_end": window_end, "server_id": sid, "role": "compute",
            "request_count":         s.get("request_count", 0),
            "cpu_percent":           s.get("avg_cpu_percent", ""),
            "ram_used_mb":           s.get("avg_ram_used_mb", ""),
            "avg_time_proc_ms":      s.get("avg_time_proc_ms", ""),
            "avg_time_db_ms":        s.get("avg_time_db_ms", ""),
            "avg_time_db_read_ms":   s.get("avg_time_db_read_ms", ""),
            "avg_time_db_write_ms":  s.get("avg_time_db_write_ms", ""),
            "avg_time_db_cmd_count": s.get("avg_time_db_cmd_count", ""),
            "avg_repl_lag_s":        "", "member_state": "",
            "last_report_ts":        s.get("last_report_ts", ""),
        })
    for sid, s in summary.get("storage_servers", {}).items():
        writer.writerow({
            "timestamp": ts, "phase": phase, "network_id": network_id,
            "window_end": window_end, "server_id": sid, "role": "storage",
            "request_count":         s.get("sample_count", 0),
            "cpu_percent":           s.get("avg_cpu_percent", ""),
            "ram_used_mb":           s.get("avg_ram_used_mb", ""),
            "avg_time_proc_ms":      "", "avg_time_db_ms": "",
            "avg_time_db_read_ms":   "", "avg_time_db_write_ms": "",
            "avg_time_db_cmd_count": "",
            "avg_repl_lag_s":        s.get("avg_repl_lag_s", ""),
            "member_state":          s.get("member_state", "") or "",
            "last_report_ts":        s.get("last_report_ts", ""),
        })


def _extract_domain_ram(summary: dict) -> tuple:
    """
    Compute domain-average RAM for edge servers and storage servers.
    The aggregator's published domain_summary does not include RAM, so we
    average it from the per-server dicts that are also in the payload.

    Returns (avg_ram_used_mb, avg_storage_ram_used_mb) — either may be None.
    """
    servers = summary.get("servers", {})
    storage = summary.get("storage_servers", {})

    ram_edge = _mean_or_none([s.get("avg_ram_used_mb") for s in servers.values()])
    ram_storage = _mean_or_none([s.get("avg_ram_used_mb") for s in storage.values()])
    return ram_edge, ram_storage


def _read_phase(phase_file: str) -> str:
    """Read current phase from the shared phase file. Returns 'transition' on failure."""
    try:
        with open(phase_file, "r") as f:
            phase = f.read().strip()
        return phase if phase else "transition"
    except (FileNotFoundError, OSError):
        return "transition"


_logger = logging.getLogger("collect_resource_stats")


def _collect_conntrack_stats():
    """Collect conntrack entry counts per VIP_DATA domain.

    Returns dict with keys:
      conntrack_entries_n1: int
      conntrack_entries_n2: int
      conntrack_entries_total: int
      conntrack_dump_ok: bool
    """
    result = {
        "conntrack_entries_n1": 0,
        "conntrack_entries_n2": 0,
        "conntrack_entries_total": 0,
        "conntrack_dump_ok": False,
    }
    try:
        proc = subprocess.run(
            ["docker", "exec", "ovs", "ovs-appctl", "dpctl/dump-conntrack"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return result

        for line in proc.stdout.splitlines():
            result["conntrack_entries_total"] += 1
            if "10.0.0.254" in line:
                result["conntrack_entries_n1"] += 1
            elif "10.0.1.254" in line:
                result["conntrack_entries_n2"] += 1

        result["conntrack_dump_ok"] = True

        if result["conntrack_entries_total"] == 0:
            _logger.warning(
                "conntrack: zero entries — no active VIP_DATA connections?"
            )

    except Exception:
        _logger.exception("conntrack dump failed")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Collect CPU/RAM stats from aggregator ZMQ PUB sockets"
    )
    parser.add_argument("--lan1-pub", default="tcp://10.0.0.5:5556", metavar="ADDR")
    parser.add_argument("--lan2-pub", default="tcp://10.0.1.5:5556", metavar="ADDR")
    parser.add_argument(
        "--lan1-coord-pub", default="tcp://127.0.0.1:5561", metavar="ADDR",
        help="LAN1 SDN controller coordinator-state PUB endpoint",
    )
    parser.add_argument(
        "--lan2-coord-pub", default="tcp://127.0.0.1:5562", metavar="ADDR",
        help="LAN2 SDN controller coordinator-state PUB endpoint",
    )
    parser.add_argument(
        "--output", default="metrics/resource_stats.csv", metavar="FILE"
    )
    parser.add_argument(
        "--output-debug", default=None, metavar="FILE",
        help="Path for broad debug CSV (default: <output_dir>/resource_stats_debug.csv)"
    )
    parser.add_argument(
        "--phase-file", default=None, metavar="FILE",
        help="Path to current_phase.txt written by traffic_generator (default: <output_dir>/current_phase.txt)"
    )
    args = parser.parse_args()

    if args.phase_file is None:
        output_dir = os.path.dirname(args.output)
        args.phase_file = os.path.join(output_dir, "current_phase.txt") if output_dir else "current_phase.txt"

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if args.output_debug is None:
        args.output_debug = os.path.join(output_dir or ".", "resource_stats_debug.csv")

    ctx = zmq.Context()

    sub1 = ctx.socket(zmq.SUB)
    sub1.connect(args.lan1_pub)
    sub1.setsockopt_string(zmq.SUBSCRIBE, "")

    sub2 = ctx.socket(zmq.SUB)
    sub2.connect(args.lan2_pub)
    sub2.setsockopt_string(zmq.SUBSCRIBE, "")

    # Coordinator-state subscribers: controllers publish one frame per telemetry
    # window after evaluate(); the latest frame per network_id is merged into rows.
    coord_sub1 = ctx.socket(zmq.SUB)
    coord_sub1.connect(args.lan1_coord_pub)
    coord_sub1.setsockopt_string(zmq.SUBSCRIBE, "")

    coord_sub2 = ctx.socket(zmq.SUB)
    coord_sub2.connect(args.lan2_coord_pub)
    coord_sub2.setsockopt_string(zmq.SUBSCRIBE, "")

    # Two indexes over the same frames: _coord_by_window[(network_id, window_end)]
    # feeds the exact-match consumed_at pairing (in poll mode the frame can arrive
    # seconds later, so unmatched rows are buffered), while _coord_latest_by_lan
    # serves Tier 1 ownership lookups and is never used for consumed_at.
    _coord_by_window: dict[tuple[str, float], dict] = {}
    _coord_latest_by_lan: dict[str, dict] = {}

    # Each entry: (key, main_row, debug_row, summary, phase, ts_str,
    #               network_id, window_end_raw)
    _buffered_rows: list = []

    def _try_flush_buffered(
        match_lan: str, match_we: float, _frame: dict,
    ) -> None:
        """Write any buffered row whose (network_id, window_end) matches."""
        nonlocal _buffered_rows
        key = (match_lan, match_we)
        remaining: list = []
        for entry in _buffered_rows:
            if entry[0] == key:
                (_k, m_row, d_row, summ, ph, ts, nid, we_raw) = entry
                d_row["consumed_at"] = _frame.get("consumed_at", "")
                writer.writerow(m_row)
                csv_file.flush()
                debug_writer.writerow(d_row)
                debug_file.flush()
                _emit_per_node_rows(
                    per_node_writer, summ,
                    phase=ph, ts=ts,
                    network_id=nid, window_end=we_raw,
                )
                per_node_file.flush()
            else:
                remaining.append(entry)
        _buffered_rows = remaining

    poller = zmq.Poller()
    poller.register(sub1, zmq.POLLIN)
    poller.register(sub2, zmq.POLLIN)
    poller.register(coord_sub1, zmq.POLLIN)
    poller.register(coord_sub2, zmq.POLLIN)

    print(
        f"[collect_resource_stats] Subscribing to:\n"
        f"  LAN1: {args.lan1_pub}\n"
        f"  LAN2: {args.lan2_pub}\n"
        f"  LAN1 coord: {args.lan1_coord_pub}\n"
        f"  LAN2 coord: {args.lan2_coord_pub}\n"
        f"  Output (main):  {args.output}\n"
        f"  Output (debug): {args.output_debug}\n"
        f"  Phase file: {args.phase_file}",
        flush=True,
    )

    csv_file = open(args.output, "w", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=MAIN_FIELDNAMES)
    writer.writeheader()
    csv_file.flush()

    debug_file = open(args.output_debug, "w", newline="")
    debug_writer = csv.DictWriter(debug_file, fieldnames=DEBUG_FIELDNAMES)
    debug_writer.writeheader()
    debug_file.flush()

    per_node_path = os.path.join(output_dir or ".", "per_node_stats.csv")
    per_node_file = open(per_node_path, "w", newline="")
    per_node_writer = csv.DictWriter(per_node_file, fieldnames=PER_NODE_FIELDNAMES)
    per_node_writer.writeheader()
    per_node_file.flush()

    try:
        while _running:
            # 500 ms poll timeout so SIGTERM is handled promptly
            socks = dict(poller.poll(timeout=500))

            # Drain coordinator-state frames first so the next aggregator
            # row sees the freshest snapshot.
            for coord_sock in (coord_sub1, coord_sub2):
                if socks.get(coord_sock) != zmq.POLLIN:
                    continue
                try:
                    raw = coord_sock.recv_string(flags=zmq.NOBLOCK)
                    frame = json.loads(raw)
                except (zmq.ZMQError, json.JSONDecodeError):
                    continue
                lan = frame.get("network_id")
                we = frame.get("window_end")
                if lan:
                    _coord_latest_by_lan[lan] = frame
                if lan and we is not None:
                    _coord_by_window[(lan, float(we))] = frame
                    # Flush any buffered telemetry row for this frame (poll mode can
                    # deliver the frame minutes after the telemetry row).
                    _try_flush_buffered(lan, float(we), frame)

            # Collect conntrack stats once per poll cycle (global on host).
            ct_stats = _collect_conntrack_stats()

            for sock in (sub1, sub2):
                if socks.get(sock) != zmq.POLLIN:
                    continue

                raw = sock.recv_string()
                try:
                    summary = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                domain = summary.get("domain_summary")
                if domain is None:
                    # drain_complete mini-summaries carry no domain_summary — skip
                    continue

                network_id = summary.get("network_id", "")
                phase = _read_phase(args.phase_file)
                ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                window_end = summary.get("window_end", "")
                servers = summary.get("servers", {})
                storage_servers = summary.get("storage_servers", {})
                server_count = len(servers)
                storage_count = len(storage_servers)
                avg_repl_lag = _domain_avg_repl_lag_ms(storage_servers)

                main_row = {
                    "timestamp":                 ts_str,
                    "phase":                     phase,
                    "network_id":                network_id,
                    "window_end":                window_end,
                    "total_requests":            domain.get("total_requests", 0),
                    "average_cpu_percent":       _domain_avg_cpu(servers) or domain.get("median_cpu_percent", ""),
                    "avg_time_proc_ms":          domain.get("median_time_proc_ms", ""),
                    "avg_storage_cpu_percent":   _domain_avg_cpu(storage_servers) or domain.get("median_storage_cpu_percent", ""),
                    "avg_time_db_ms":            domain.get("median_time_db_ms", ""),
                    "p95_time_db_ms":            _domain_p95_time_db_ms(storage_servers) or "",
                    "storage_latency_signal_ms": _storage_latency_signal_ms(domain, storage_servers) or "",
                    "server_count":              server_count,
                    "storage_count":             storage_count,
                    "avg_repl_lag_ms":           avg_repl_lag,
                }
                # Tier 1 fields use peer_lan (which LAN owns the copy); consumed_at
                # uses the SAME lan, matched by window_end for exact pairing.
                coord_lan = peer_lan(network_id) if network_id in ("lan1", "lan2") else network_id
                t1_coord = _coord_latest_by_lan.get(coord_lan, {})

                t1 = build_tier1_row(summary, t1_coord)
                main_row["coord_state_owner_lan"] = t1.get("coord_state_owner_lan", "NONE")
                main_row["tier1_lifecycle_active_count"] = t1.get("tier1_lifecycle_active_count", 0)

                main_row["conntrack_entries_n1"] = ct_stats.get("conntrack_entries_n1", "")
                main_row["conntrack_entries_n2"] = ct_stats.get("conntrack_entries_n2", "")

                debug_row = {
                    "timestamp":                   ts_str,
                    "phase":                       phase,
                    "network_id":                  network_id,
                    "window_end":                  window_end,
                    "total_requests":              domain.get("total_requests", 0),
                    "median_cpu_percent":           domain.get("median_cpu_percent", ""),
                    "median_ram_used_mb":           domain.get("median_ram_used_mb", ""),
                    "median_storage_cpu_percent":   domain.get("median_storage_cpu_percent", ""),
                    "median_storage_ram_used_mb":   domain.get("median_storage_ram_used_mb", ""),
                    "median_time_proc_ms":          domain.get("median_time_proc_ms", ""),
                    "median_time_db_ms":            domain.get("median_time_db_ms", ""),
                    "median_time_total_ms":         domain.get("median_time_total_ms", ""),
                    "server_count":                server_count,
                    "storage_count":               storage_count,
                    "avg_repl_lag_ms":             avg_repl_lag,
                    "avg_time_db_read_ms":         domain.get("avg_time_db_read_ms", ""),
                    "avg_time_db_write_ms":        domain.get("avg_time_db_write_ms", ""),
                    "avg_time_db_cmd_count":       domain.get("avg_time_db_cmd_count", ""),
                }

                # consumed_at is same-LAN and matched by window_end; in poll mode the
                # frame may not exist yet, so the row is buffered (see _try_flush_buffered).
                we_float = float(window_end) if window_end else 0.0
                coord_key = (network_id, we_float)
                matched_coord = _coord_by_window.get(coord_key, {})
                debug_row["consumed_at"] = matched_coord.get("consumed_at", "")

                debug_row.update(build_tier1_row(summary, t1_coord))

                if matched_coord:
                    writer.writerow(main_row)
                    csv_file.flush()
                    debug_writer.writerow(debug_row)
                    debug_file.flush()
                    _emit_per_node_rows(
                        per_node_writer, summary,
                        phase=phase, ts=ts_str,
                        network_id=network_id, window_end=window_end,
                    )
                    per_node_file.flush()
                else:
                    _buffered_rows.append(
                        (coord_key, main_row, debug_row, summary,
                         phase, ts_str, network_id, window_end)
                    )

    finally:
        # Flush any buffered rows that never received a coordinator frame
        # (e.g., telemetry windows the controller skipped in poll mode).
        if _buffered_rows:
            print(f"[collect_resource_stats] flushing {len(_buffered_rows)} "
                  f"buffered rows at shutdown", flush=True)
            for entry in _buffered_rows:
                (_k, m_row, d_row, summ, ph, ts, nid, we_raw) = entry
                writer.writerow(m_row)
                debug_writer.writerow(d_row)
                _emit_per_node_rows(
                    per_node_writer, summ,
                    phase=ph, ts=ts,
                    network_id=nid, window_end=we_raw,
                )
            csv_file.flush()
            debug_file.flush()
            per_node_file.flush()

        csv_file.close()
        debug_file.close()
        per_node_file.close()
        sub1.close()
        sub2.close()
        coord_sub1.close()
        coord_sub2.close()
        ctx.destroy(linger=0)
        print("[collect_resource_stats] Stopped. Main: " + args.output
              + "  Debug: " + args.output_debug, flush=True)


if __name__ == "__main__":
    main()
