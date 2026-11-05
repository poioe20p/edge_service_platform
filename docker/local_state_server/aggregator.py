import json
import os
import statistics
import threading
import time
import logging
import urllib.parse
from collections import Counter, deque
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

import zmq

WINDOW_S = float(os.environ.get("WINDOW_S", "10"))
NETWORK_ID = os.environ["NETWORK_ID"]
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

logger = logging.getLogger("aggregator")

_CONTROL_EVENT_TYPES = frozenset(
    {"drain_complete", "rs_secondary_ready", "request_complete", "app_ready"}
)
_HTTP_REQUIRED_KEYS = frozenset({
    "server_id",
    "time_total_ms",
    "time_db_ms",
    "status_code",
    "cpu_percent",
    "ram_used_mb",
})
_MONGO_REQUIRED_KEYS = frozenset({
    "server_id",
    "connections_current",
    "cpu_percent",
    "ram_used_mb",
})

ctx = zmq.Context()

pull = ctx.socket(zmq.PULL)
_pull_addr = os.environ.get("PULL_ADDR", "tcp://0.0.0.0:5555")
pull.bind(_pull_addr)

# Latest summary per network_id for pollers; updated on every ZMQ publish so the
# push and poll paths always serve the identical object.
_latest_summary: dict[str, dict] = {}
logger.info("PULL socket bound to %s", _pull_addr)

pub = ctx.socket(zmq.PUB)
_pub_addr = os.environ.get("PUB_ADDR", "tcp://0.0.0.0:5556")
pub.bind(_pub_addr)
logger.info("PUB socket bound to %s", _pub_addr)

_buffer: list = []
_lock = threading.Lock()

# Every WINDOW_S interval is a window with a monotonic window_seq; windows,
# including empty ones, are published, persisted to JSONL and replayed over HTTP.
WINDOW_LOG_RETENTION = int(os.environ.get("WINDOW_LOG_RETENTION", "10000"))
WINDOW_LOG_PATH = os.environ.get("WINDOW_LOG_PATH", "/tmp/window_log.jsonl")
ACK_LOG_PATH = os.environ.get("ACK_LOG_PATH", "/tmp/ack_log.jsonl")
# Overload label (D3) — pre-registered thresholds, identical across RQ1 arms.
OVERLOAD_CPU_PCT = float(os.environ.get("OVERLOAD_CPU_PCT", "5.0"))
OVERLOAD_PEAK_LATENCY_MS = float(os.environ.get("OVERLOAD_PEAK_LATENCY_MS", "1000"))
OVERLOAD_ERROR_RATE = float(os.environ.get("OVERLOAD_ERROR_RATE", "0.05"))
# research_q3: a capped-CPU fleet crosses the CPU/latency clauses while nearly idle
# (storm ~220 req/win vs hold ~38), so also require OVERLOAD_MIN_REQUESTS requests.
OVERLOAD_MIN_REQUESTS = float(os.environ.get("OVERLOAD_MIN_REQUESTS", "0"))

_window_seq: int = 0
_window_log: deque = deque(maxlen=WINDOW_LOG_RETENTION)
_log_lock = threading.Lock()


def _load_window_log_tail() -> None:
    """Resume seq + in-memory ring from the durable JSONL on boot.

    Makes window_seq restart-continuous so consumers' last_seq stays valid
    across aggregator restarts (--restart=on-failure).
    """
    global _window_seq
    if not os.path.exists(WINDOW_LOG_PATH):
        return
    try:
        with open(WINDOW_LOG_PATH, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with _log_lock:
                    _window_log.append(rec)
                if isinstance(rec.get("window_seq"), int):
                    _window_seq = max(_window_seq, rec["window_seq"])
    except OSError as exc:
        logger.warning("could not load window log tail: %s", exc)


def _append_window_record(rec: dict) -> None:
    with _log_lock:
        _window_log.append(rec)
        try:
            with open(WINDOW_LOG_PATH, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            logger.warning("window log append failed: %s", exc)


def _append_ack_record(rec: dict) -> None:
    # Guarded by _log_lock: concurrent ThreadingHTTPServer handler threads write
    # the ack log; O_APPEND alone does not make multi-part writes atomic.
    with _log_lock:
        try:
            with open(ACK_LOG_PATH, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            logger.warning("ack log append failed: %s", exc)


def _compute_overload(summary: dict) -> bool:
    ds = summary.get("domain_summary")
    if ds is None:
        return False
    if ds.get("total_requests", 0) < OVERLOAD_MIN_REQUESTS:
        return False
    error_rates = [s.get("error_rate", 0.0) for s in summary.get("servers", {}).values()]
    error_rate = statistics.mean(error_rates) if error_rates else 0.0
    return (
        ds.get("average_cpu_percent", 0.0) >= OVERLOAD_CPU_PCT
        or ds.get("peak_time_total_ms", 0.0) >= OVERLOAD_PEAK_LATENCY_MS
        or error_rate >= OVERLOAD_ERROR_RATE
    )


def _publish_window(summary: dict, *, request_evidence: bool = True) -> None:
    global _window_seq
    _window_seq += 1
    summary["window_seq"] = _window_seq
    summary["window_id"] = f"{NETWORK_ID}:{_window_seq}"
    # False = the window carried no HTTP request samples (empty window or
    # non-HTTP-only window). Consumers must not read it as a lull signal.
    summary["request_evidence"] = request_evidence
    summary["overload"] = _compute_overload(summary)
    ds = summary.get("domain_summary") or {}
    logger.info(
        "Publishing window seq=%d network_id=%s total_requests=%d avg_cpu=%.1f%% peak_total_ms=%.1f evidence=%s overload=%s",
        _window_seq,
        summary.get("network_id"),
        ds.get("total_requests", 0),
        ds.get("average_cpu_percent", 0.0),
        ds.get("peak_time_total_ms", 0.0),
        request_evidence,
        summary["overload"],
    )
    logger.debug("Full summary: %s", summary)
    _append_window_record(summary)
    pub.send_json(summary)
    _latest_summary[NETWORK_ID] = summary


def _get_window_record(seq: int) -> dict | None:
    with _log_lock:
        for rec in reversed(_window_log):
            if rec.get("window_seq") == seq:
                return rec
    return None


def _first_available_seq() -> int | None:
    with _log_lock:
        if not _window_log:
            return None
        return _window_log[0].get("window_seq")


def _get_windows_after(after_seq: int, limit: int) -> dict:
    with _log_lock:
        recs = [
            r for r in _window_log
            if isinstance(r.get("window_seq"), int) and r["window_seq"] > after_seq
        ]
    if not recs:
        return {"windows": [], "next_after_seq": after_seq, "truncated": False}
    out = recs[:limit]
    return {
        "windows": out,
        "next_after_seq": out[-1]["window_seq"],
        "truncated": len(recs) > limit,
    }


def _is_http_event(event: dict) -> bool:
    return _HTTP_REQUIRED_KEYS.issubset(event)


def _is_mongo_event(event: dict) -> bool:
    return event.get("event_type") == "mongo_stats" and _MONGO_REQUIRED_KEYS.issubset(event)


def _is_heartbeat_event(event: dict) -> bool:
    return event.get("event_type") == "heartbeat" and bool(event.get("server_id"))


def _p95(samples: list[float]) -> float:
    """Return the inclusive p95 for a non-empty sample list."""
    if len(samples) >= 2:
        return statistics.quantiles(samples, n=20, method="inclusive")[18]
    return samples[0]


def _is_known_window_event(event: dict) -> bool:
    return (
        _is_http_event(event)
        or _is_mongo_event(event)
        or _is_heartbeat_event(event)
    )


def _extract_control_events(event: dict) -> list[dict]:
    etype = event.get("event_type")
    if etype in _CONTROL_EVENT_TYPES:
        return [event]

    controls = event.get("control_events")
    if not isinstance(controls, list):
        return []

    out = []
    for control in controls:
        if not isinstance(control, dict):
            logger.warning("Dropping non-dict control event from frame keys=%s", sorted(event.keys()))
            continue
        if control.get("event_type") not in _CONTROL_EVENT_TYPES:
            logger.warning(
                "Dropping unsupported control event type=%s keys=%s",
                control.get("event_type"), sorted(control.keys()),
            )
            continue
        out.append(control)
    return out


def _publish_control_events(events: list[dict]) -> None:
    if not events:
        return
    mini = {
        "network_id":     NETWORK_ID,
        "window_end":     time.time(),
        "servers":        {},
        "storage_servers": {},
        "control_events": events,
    }
    event_types = [event.get("event_type") for event in events]
    server_ids = [event.get("server_id") for event in events]
    logger.info(
        "Control events received types=%s server_ids=%s - publishing mini-summary",
        event_types, server_ids,
    )
    pub.send_json(mini)


def _log_malformed_events(events: list[dict]) -> None:
    for event in events:
        logger.warning(
            "Dropping malformed telemetry frame keys=%s event_type=%s server_id=%s server_mac=%s",
            sorted(event.keys()),
            event.get("event_type"),
            event.get("server_id"),
            event.get("server_mac"),
        )


def _receive_loop() -> None:
    logger.info("Receive loop started")
    while True:
        event = pull.recv_json()
        if not isinstance(event, dict):
            logger.warning("Dropping non-dict telemetry frame type=%s", type(event).__name__)
            continue
        controls = event.get("control_events")
        control_count = len(controls) if isinstance(controls, list) else 0
        logger.debug(
            "Received event from server_id=%s server_mac=%s event_type=%s control_events=%s",
            event.get("server_id"), event.get("server_mac"), event.get("event_type"),
            control_count,
        )
        control_events = _extract_control_events(event)
        if control_events:
            # Publish control events immediately as mini-summaries: the controller
            # needs them now, so they are not buffered into the window.
            _publish_control_events(control_events)
            continue
        with _lock:
            _buffer.append(event)


def _publish_loop() -> None:
    logger.info("Publish loop started: network_id=%s window=%.1fs", NETWORK_ID, WINDOW_S)
    while True:
        time.sleep(WINDOW_S)
        with _lock:
            window, _buffer[:] = list(_buffer), []

        last_seen: dict[str, float] = {}
        for event in window:
            sid = event.get("server_id")
            ts  = event.get("ts", 0.0)
            if sid and ts > last_seen.get(sid, 0.0):
                last_seen[sid] = ts

        http_events  = [e for e in window if _is_http_event(e)]
        mongo_events = [e for e in window if _is_mongo_event(e)]
        heartbeats   = [e for e in window if _is_heartbeat_event(e)]
        malformed_events = [e for e in window if not _is_known_window_event(e)]
        if malformed_events:
            _log_malformed_events(malformed_events)
        if not (http_events or mongo_events or heartbeats):
            # Always-publish (RQ1): an idle window still carries a window_seq so the
            # delivery contract stays contiguous for the analyzer.
            logger.debug("Window contained no valid telemetry events — publishing empty window")
            _publish_window({
                "network_id":      NETWORK_ID,
                "window_end":      time.time(),
                "servers":         {},
                "storage_servers": {},
                "domain_summary":  None,
                "control_events":  [],
            }, request_evidence=False)
            continue

        by_server: dict = {}
        for event in http_events:
            by_server.setdefault(event["server_id"], []).append(event)

        servers = {}
        for server_id, events in by_server.items():
            time_totals = [event["time_total_ms"] for event in events]
            time_db     = [event["time_db_ms"] for event in events]
            time_procs  = [event["time_total_ms"] - event["time_db_ms"] for event in events]
            errors      = sum(1 for event in events if event["status_code"] >= 500)
            latest_event = max(events, key=lambda event: event.get("ts", 0.0))

            servers[server_id] = {
                "avg_time_total_ms": statistics.mean(time_totals),
                "avg_time_db_ms":    statistics.mean(time_db),
                "avg_time_proc_ms":  statistics.mean(time_procs),
                "request_count":     len(events),
                "error_rate":        errors / len(events),
                "avg_cpu_percent":   statistics.mean([event["cpu_percent"] for event in events]),
                "avg_ram_used_mb":   statistics.mean([event["ram_used_mb"] for event in events]),
                "last_report_ts":    last_seen.get(server_id, 0.0),
                "state":             latest_event.get("state", "active"),
                "avg_time_db_read_ms":   statistics.mean(e.get("time_db_read_ms", 0) for e in events),
                "avg_time_db_write_ms":  statistics.mean(e.get("time_db_write_ms", 0) for e in events),
                "avg_time_db_cmd_count": statistics.mean(e.get("time_db_cmd_count", 0) for e in events),
            }
            logger.debug(
                "server_id=%s requests=%d error_rate=%.2f avg_total_ms=%.1f avg_db_ms=%.1f",
                server_id, len(events), errors / len(events),
                statistics.mean(time_totals), statistics.mean(time_db),
            )

        by_storage: dict = {}
        for event in mongo_events:
            by_storage.setdefault(event["server_id"], []).append(event)

        storage_servers = {}
        for server_id, events in by_storage.items():
            lags = [e["repl_lag_s"] for e in events if e.get("repl_lag_s") is not None]
            storage_servers[server_id] = {
                "avg_repl_lag_s":  statistics.mean(lags) if lags else None,
                "avg_connections": statistics.mean([e["connections_current"] for e in events]),
                "avg_cpu_percent": statistics.mean([e["cpu_percent"] for e in events]),
                "avg_ram_used_mb": statistics.mean([e["ram_used_mb"] for e in events]),
                "sample_count":    len(events),
                "last_report_ts":  last_seen.get(server_id, 0.0),
                "member_state":    events[-1].get("member_state"),
            }

        for hb in heartbeats:
            sid = hb.get("server_id")
            if not sid:
                continue
            if "connections_current" in hb:          # storage sidecar heartbeat
                if sid not in storage_servers:
                    storage_servers[sid] = {
                        "avg_repl_lag_s":  hb.get("repl_lag_s"),
                        "avg_connections": float(hb.get("connections_current", 0)),
                        "avg_cpu_percent": hb.get("cpu_percent", 0.0),
                        "avg_ram_used_mb": hb.get("ram_used_mb", 0.0),
                        "sample_count":    0,
                        "last_report_ts":  last_seen.get(sid, hb.get("ts", 0.0)),
                        "member_state":    hb.get("member_state"),
                    }
            else:                                     # edge server heartbeat
                if sid not in servers:
                    # Liveness-only entry: flagged so routing stats never absorb it as a
                    # zero-latency sample, while presence consumers still see it.
                    servers[sid] = {
                        "heartbeat_only":    True,
                        "avg_time_total_ms": 0.0,
                        "avg_time_db_ms":    0.0,
                        "avg_time_proc_ms":  0.0,
                        "request_count":     0,
                        "error_rate":        0.0,
                        "avg_cpu_percent":   hb.get("cpu_percent", 0.0),
                        "avg_ram_used_mb":   hb.get("ram_used_mb", 0.0),
                        "last_report_ts":    last_seen.get(sid, hb.get("ts", 0.0)),
                        "state":             hb.get("state", "active"),
                    }
                else:
                    ts = hb.get("ts", 0.0)
                    entry = servers[sid]
                    if ts > entry.get("last_report_ts", 0.0):
                        entry["last_report_ts"] = ts
                        entry["state"] = hb.get("state", entry.get("state", "active"))

        if http_events:
            time_procs_all  = [e["time_total_ms"] - e["time_db_ms"] for e in http_events]
            time_dbs_all    = [e["time_db_ms"] for e in http_events]
            cpus_all        = [e["cpu_percent"] for e in http_events]
            time_totals_all = [e["time_total_ms"] for e in http_events]
            rams_all        = [e["ram_used_mb"] for e in http_events]

            avg_time_proc   = statistics.mean(time_procs_all)
            avg_time_db     = statistics.mean(time_dbs_all)
            p95_time_proc   = _p95(time_procs_all)
            p95_time_db     = _p95(time_dbs_all)
            avg_cpu_percent = statistics.mean(cpus_all)
            peak_time_total = max(time_totals_all)
            total_requests  = len(http_events)

            median_time_proc   = statistics.median(time_procs_all)
            median_time_db     = statistics.median(time_dbs_all)
            median_cpu_percent = statistics.median(cpus_all)
            median_time_total  = statistics.median(time_totals_all)
            median_ram_used_mb = statistics.median(rams_all)

            avg_time_db_read_ms   = statistics.mean(e.get("time_db_read_ms", 0) for e in http_events)
            avg_time_db_write_ms  = statistics.mean(e.get("time_db_write_ms", 0) for e in http_events)
            avg_time_db_cmd_count = statistics.mean(e.get("time_db_cmd_count", 0) for e in http_events)
        else:
            avg_time_proc = avg_time_db = avg_cpu_percent = peak_time_total = 0.0
            p95_time_proc = p95_time_db = 0.0
            median_time_proc = median_time_db = median_cpu_percent = 0.0
            median_time_total = median_ram_used_mb = 0.0
            total_requests = 0
            avg_time_db_read_ms = avg_time_db_write_ms = avg_time_db_cmd_count = 0.0

        storage_cpu_values = [
            ss["avg_cpu_percent"]
            for ss in storage_servers.values()
            if ss.get("avg_cpu_percent") is not None
        ]
        avg_storage_cpu_percent = statistics.mean(storage_cpu_values) if storage_cpu_values else 0.0

        storage_ram_values = [
            ss["avg_ram_used_mb"]
            for ss in storage_servers.values()
            if ss.get("avg_ram_used_mb") is not None
        ]
        median_storage_cpu_percent = statistics.median(storage_cpu_values) if storage_cpu_values else 0.0
        median_storage_ram_used_mb = statistics.median(storage_ram_values) if storage_ram_values else 0.0

        # Request-lease outcomes per LAN — the recovery-distress signal.
        # Shape: {"lan": "lan1", "outcome": "success_after_rebind", ...}
        request_lease_outcomes_per_lan: dict[str, dict[str, int]] = {}
        for ev in http_events:
            for rlo in ev.get("request_lease_outcomes") or []:
                lan = rlo.get("lan", "")
                if not lan:
                    continue
                dst = request_lease_outcomes_per_lan.setdefault(lan, {})
                outcome = rlo.get("outcome", "unknown")
                dst[outcome] = dst.get(outcome, 0) + 1

        summary = {
            "network_id":      NETWORK_ID,
            "window_end":      time.time(),
            "servers":         servers,
            "storage_servers": storage_servers,
            "control_events":  [],
            "domain_summary": {
                "total_requests":          total_requests,
                "avg_time_proc_ms":        avg_time_proc,
                "avg_time_db_ms":          avg_time_db,
                "p95_time_proc_ms":        p95_time_proc,
                "p95_time_db_ms":          p95_time_db,
                "average_cpu_percent":     avg_cpu_percent,
                "peak_time_total_ms":      peak_time_total,
                "avg_storage_cpu_percent": avg_storage_cpu_percent,
                "median_cpu_percent":          median_cpu_percent,
                "median_ram_used_mb":          median_ram_used_mb,
                "median_storage_cpu_percent":  median_storage_cpu_percent,
                "median_storage_ram_used_mb":  median_storage_ram_used_mb,
                "median_time_proc_ms":         median_time_proc,
                "median_time_db_ms":           median_time_db,
                "median_time_total_ms":        median_time_total,
                "avg_time_db_read_ms":         avg_time_db_read_ms,
                "avg_time_db_write_ms":        avg_time_db_write_ms,
                "avg_time_db_cmd_count":       avg_time_db_cmd_count,
                "request_lease_outcomes_per_lan": request_lease_outcomes_per_lan,
            },
        }
        _publish_window(summary, request_evidence=bool(http_events))


threading.Thread(target=_receive_loop, daemon=True).start()

_CACHE_PORT = int(os.environ.get("SUMMARY_CACHE_PORT", "5558"))

class _SummaryHandler(BaseHTTPRequestHandler):
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/latest_summary":
            self._send_json(_latest_summary.get(NETWORK_ID, {}))
            return

        if path == "/window":
            try:
                seq = int(query.get("seq", [""])[0])
            except ValueError:
                self._send_json({"error": "bad_request", "seq": query.get("seq", [""])[0]}, status=400)
                return
            rec = _get_window_record(seq)
            if rec is None:
                first = _first_available_seq()
                if first is not None and seq < first:
                    self._send_json({"error": "aged_out", "seq": seq, "first_available_seq": first}, status=410)
                else:
                    self._send_json({"error": "not_found", "seq": seq}, status=404)
            else:
                self._send_json(rec)
            return

        if path == "/windows":
            try:
                after_seq = int(query.get("after_seq", ["0"])[0])
            except ValueError:
                self._send_json({"error": "bad_request", "after_seq": query.get("after_seq", [""])[0]}, status=400)
                return
            limit = 100
            if query.get("limit"):
                try:
                    limit = int(query["limit"][0])
                except ValueError:
                    limit = 100
            if limit <= 0:
                limit = 1
            result = _get_windows_after(after_seq, limit)
            # A consumer behind the retained range gets the windows plus an
            # ``aged_out_from`` marker (not a bare 410) so it can advance.
            first = _first_available_seq()
            if first is not None and after_seq < first:
                result["aged_out_from"] = first
            self._send_json(result)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/ack":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "bad_request"}, status=400)
                return
            _append_ack_record(data)
            self._send_json({"ok": True})
            return
        self.send_response(404)
        self.end_headers()

    # Suppress stderr logging of each request (noisy at 1 s polling).
    def log_message(self, format, *args):
        logger.debug("HTTP %s", format % args)


_load_window_log_tail()
_cache_server = ThreadingHTTPServer(("0.0.0.0", _CACHE_PORT), _SummaryHandler)
_cache_thread = threading.Thread(target=_cache_server.serve_forever, daemon=True)
_cache_thread.start()
logger.info("Summary cache HTTP server listening on port %s", _CACHE_PORT)

_publish_loop()
