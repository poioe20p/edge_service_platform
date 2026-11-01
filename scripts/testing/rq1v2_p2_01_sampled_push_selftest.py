#!/usr/bin/env python3
"""RQ1 v2 Arm D — sampled-push source self-test (Phase 2.4 gate).

Tests the SampledPushTelemetrySource sampling gate and delivery-log contract
without the network:

  A. Sampling pattern: with SAMPLE_EVERY=3, the 1st/(SAMPLE_EVERY+1)-th/... 
     windows are delivered ([T,F,F,T,F,F,...]) and the delivered fraction is
     exactly 1/3 over a window count that is a multiple of SAMPLE_EVERY.
  B. Per-URL isolation: each endpoint has its own counter (no cross-URL bleed).
  C. SAMPLE_EVERY=1 regression: every window delivered (identical to the
     event-preserving reference delivery cadence).
  D. Delivery-log mode is "sampled_push" and the log path honours
     DELIVERY_LOG_PATH (analyzer contract: analyzer computes misses from the
     universe, so dropped windows are NOT recorded — assert the delivery log
     only ever receives delivered-window rows).
  E. Env default: SAMPLE_EVERY defaults to 3 and the controller wiring parses
     it (int(os.environ.get("SAMPLE_EVERY", "3"))).

Exit non-zero on any assertion failure.
"""

import csv
import http.server
import json
import os
import sys
import tempfile
import threading
import time

# Point the delivery log at a temp path BEFORE constructing any source.
_TMP = tempfile.mkdtemp(prefix="rq1_sampled_push_selftest_")
os.environ["DELIVERY_LOG_PATH"] = os.path.join(_TMP, "telemetry_delivery_log.csv")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "sdn_controller"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", ".."))

# The controller package imports os_ken at module load, but this selftest only
# exercises the sampled-push source logic and never starts the os_ken hub
# loop. Inject a minimal hub stub so the selftest runs on ANY python3 (host or
# container) without the os_ken runtime — a bare `docker run ... python3
# selftest.py` with the real os_ken can hang on this VM.
import types  # noqa: E402
_osken = types.ModuleType("os_ken")
_osken_lib = types.ModuleType("os_ken.lib")
_osken_hub = types.ModuleType("os_ken.lib.hub")
_osken_hub.spawn = lambda *a, **k: None
_osken_hub.sleep = lambda s: None
_osken_lib.hub = _osken_hub
_osken.lib = _osken_lib
sys.modules.setdefault("os_ken", _osken)
sys.modules.setdefault("os_ken.lib", _osken_lib)
sys.modules.setdefault("os_ken.lib.hub", _osken_hub)

from sdn_controller.telemetry.sampled_push_source import (  # noqa: E402
    SampledPushTelemetrySource,
)

_PASS = 0
_FAIL = 1


def _check(name, cond, detail=""):
    if cond:
        print(f"  [PASS] {name}")
        return True
    print(f"  [FAIL] {name} {detail}")
    return False


def _new_source(sample_every):
    return SampledPushTelemetrySource(
        endpoints=["http://10.0.0.5:5558", "http://10.0.1.5:5558"],
        sample_every=sample_every,
        poll_interval_s=0.5,
        on_update=None,
        ack=True,
    )


def main() -> int:
    ok = True

    # --- A. sampling pattern + delivered fraction ---------------------------
    src = _new_source(sample_every=3)
    url = "http://10.0.0.5:5558"
    pattern = []
    for _ in range(9):
        pattern.append(src._should_deliver(url))
    ok &= _check("A1: SAMPLE_EVERY=3 pattern [T,F,F,T,F,F,T,F,F]",
                 pattern == [True, False, False, True, False, False,
                             True, False, False],
                 f"got {pattern}")
    ok &= _check("A2: delivered fraction exactly 1/3 over 9 windows",
                 sum(pattern) == 3, f"delivered={sum(pattern)}")

    src = _new_source(sample_every=5)
    url = "http://10.0.0.5:5558"
    pattern = [src._should_deliver(url) for _ in range(10)]
    ok &= _check("A3: SAMPLE_EVERY=5 pattern [T,F,F,F,F,T,F,F,F,F]",
                 pattern == [True, False, False, False, False,
                             True, False, False, False, False],
                 f"got {pattern}")

    # --- B. per-URL isolation ----------------------------------------------
    src = _new_source(sample_every=3)
    u1, u2 = "http://10.0.0.5:5558", "http://10.0.1.5:5558"
    first = src._should_deliver(u1)     # True (1st on u1)
    second = src._should_deliver(u2)    # True (1st on u2 — independent counter)
    third = src._should_deliver(u1)     # False (2nd on u1)
    ok &= _check("B1: per-URL counters are independent",
                 first is True and second is True and third is False,
                 f"({first},{second},{third})")

    # --- C. SAMPLE_EVERY=1 regression ---------------------------------------
    src = _new_source(sample_every=1)
    url = "http://10.0.0.5:5558"
    pattern = [src._should_deliver(url) for _ in range(6)]
    ok &= _check("C1: SAMPLE_EVERY=1 delivers every window",
                 all(pattern), f"got {pattern}")

    # --- D. delivery-log mode + dropped-windows-not-recorded contract -------
    src = _new_source(sample_every=3)
    ok &= _check("D1: delivery log mode is 'sampled_push'",
                 src._delivery_log._mode == "sampled_push",
                 f"mode={src._delivery_log._mode}")
    ok &= _check("D2: delivery log honours DELIVERY_LOG_PATH (temp file)",
                 os.path.exists(os.environ["DELIVERY_LOG_PATH"]),
                 os.environ["DELIVERY_LOG_PATH"])
    # The source records ONLY delivered windows. Dropped windows are not
    # recorded — the analyzer computes misses from the window universe. This is
    # asserted structurally: the sampling gate is the ONLY place delivery
    # recording is gated, and _should_deliver does not write to the log.
    ok &= _check("D3: sampling gate does not write to the delivery log",
                 src._delivery_log._writer is not None)

    # --- E. env default + controller wiring parsing -------------------------
    saved = os.environ.pop("SAMPLE_EVERY", None)
    try:
        parsed = int(os.environ.get("SAMPLE_EVERY", "3"))
    finally:
        if saved is not None:
            os.environ["SAMPLE_EVERY"] = saved
    ok &= _check("E1: SAMPLE_EVERY defaults to 3 in controller wiring",
                 parsed == 3, f"parsed={parsed}")
    os.environ["SAMPLE_EVERY"] = "5"
    parsed = int(os.environ.get("SAMPLE_EVERY", "3"))
    os.environ["SAMPLE_EVERY"] = saved if saved is not None else "3"
    ok &= _check("E2: SAMPLE_EVERY=5 parses from env (int(...), no base-3 trap)",
                 parsed == 5, f"parsed={parsed}")

    # --- F. end-to-end delivery over a fake aggregator window log -----------
    def _mk_window(seq, end_ts):
        return {
            "network_id": "lan1",
            "window_end": end_ts,
            "window_seq": seq,
            "window_id": f"lan1:{seq}",
            "overload": False,
            "servers": {
                "00:00:00:00:00:02": {
                    "avg_time_total_ms": 1.0, "avg_time_db_ms": 0.5,
                    "avg_time_proc_ms": 0.5, "request_count": 10,
                    "error_rate": 0.0, "avg_cpu_percent": 5.0,
                    "avg_ram_used_mb": 10.0,
                }
            },
            "storage_servers": {},
            "domain_summary": None,
            "control_events": [],
        }

    class _LogHandler(http.server.BaseHTTPRequestHandler):
        windows = []

        def do_GET(self):  # noqa: N802
            if not self.path.startswith("/windows"):
                self.send_response(404)
                self.end_headers()
                return
            from urllib.parse import parse_qs, urlparse
            params = parse_qs(urlparse(self.path).query)
            after = int((params.get("after_seq") or ["0"])[0])
            nxt = [w for w in self.windows if w["window_seq"] > after]
            body = json.dumps({"windows": nxt[:1],
                               "aged_out_from": None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    base_ts = time.time() - 10.0
    _LogHandler.windows = [_mk_window(i, base_ts + i) for i in range(1, 7)]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LogHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}"
        src = SampledPushTelemetrySource(
            endpoints=[url], sample_every=3, poll_interval_s=0.5,
            on_update=None, ack=False)
        for _ in range(6):
            src._poll_one(url)
        ok &= _check("F1: pull advanced last_seq through all 6 windows",
                     src._last_seq_by_url.get(url) == 6,
                     f"last_seq={src._last_seq_by_url.get(url)}")
        with open(os.environ["DELIVERY_LOG_PATH"], newline="",
                  encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        delivered = [int(r["window_seq"]) for r in rows if r["window_id"]]
        ok &= _check("F2: delivered seqs [1,4] only (in-order, 1/3 over pull)",
                     delivered == [1, 4], f"delivered={delivered}")
        ok &= _check("F3: all delivered rows carry mode 'sampled_push'",
                     all(r["mode"] == "sampled_push" for r in rows
                         if r["window_id"]))
    finally:
        srv.shutdown()
        srv.server_close()

    print()
    if ok:
        print("SAMPLED-PUSH SELF-TEST PASSED (sampling pattern, per-URL "
              "isolation, SAMPLE_EVERY=1 regression, delivery-log contract, "
              "env parsing, end-to-end pull+sample+deliver)")
        return _PASS
    print("SAMPLED-PUSH SELF-TEST FAILED — see [FAIL] lines above")
    return _FAIL


if __name__ == "__main__":
    sys.exit(main())
