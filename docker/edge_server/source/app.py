import logging
import os
import threading
import time

from flask import Flask, request

from db_timing_listener import register as _register_db_timing_listener

# Register the pymongo CommandListener before any MongoClient is created.
_register_db_timing_listener()

from control_plane_routes import register_control_plane_routes
from edge_server_config import CONFIG
from edge_server_process_state import EdgeServerProcessState, SKIP_COUNTING_PATHS
from edge_request_lifecycle import (
    register_post_telemetry_request_hooks,
    register_pre_telemetry_request_hooks,
)
from workload_routes import register_workload_routes
from telemetry import init_telemetry, _get_server_mac
from vip_data_mongo_runtime import (
    snapshot_normal_vip_config,
    start_epoch_housekeeping,
    _get_write_client,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
process_state = EdgeServerProcessState(CONFIG)

# Flask runs after_request hooks in reverse registration order, so the
# post-telemetry hooks registered last run before telemetry emission.
register_pre_telemetry_request_hooks(app, CONFIG, process_state)
register_control_plane_routes(app, process_state)
register_workload_routes(app, CONFIG, process_state)
start_epoch_housekeeping()
init_telemetry(
    app,
    sender=process_state.metric_sender,
    get_drain_state=process_state.get_drain_state,
)
register_post_telemetry_request_hooks(app, process_state)


@app.after_request
def _add_backend_identity(response):
    """Stamp every response with the container identity so the traffic
    generator can log which backend served each request.

    Uses the Docker container hostname (e.g. edge_server_lan1_dyn2) as the
    stable identifier.  The controller also knows container names from spawn
    events, enabling cross-referencing for TFR (Time-to-First-Response)
    computation.

    RQ3 flow isolation: when EDGE_FLOW_ISOLATION=1 and the request is a real
    workload request (not /health, /drain, /ready), emit a ``request_complete``
    control event from a background thread (after the response is flushed) so
    the controller deletes this client's VIP_SERVER flow → one fresh
    backend-selection event per request. client_ip is captured in-context; the
    thread only calls metric_sender.send()."""
    backend_id = os.environ.get("CONTAINER_NAME", os.environ.get("HOSTNAME", "unknown"))
    response.headers["X-Backend-ID"] = backend_id
    if os.environ.get("EDGE_FLOW_ISOLATION", "0") == "1":
        if request.path not in SKIP_COUNTING_PATHS:
            client_ip = request.remote_addr
            # DNAT rewrites only the destination, so REMOTE_PORT is the client's ephemeral
            # source port VIP_SERVER_PER_CONNECTION_FLOWS=1 scopes the flow delete to; 0 = unknown.
            client_port = int(request.environ.get("REMOTE_PORT") or 0)
            server_mac = _get_server_mac()
            threading.Thread(
                target=_emit_request_complete,
                args=(process_state, client_ip, client_port, server_mac),
                daemon=True,
            ).start()
    return response


def _emit_request_complete(process_state, client_ip: str, client_port: int,
                           server_mac: str) -> None:
    """Emit a request_complete control event (RQ3 flow isolation).

    Runs in a background thread after the response is flushed; never touches
    the (torn-down) request context. ``client_port`` is the connection's
    source port (per-connection flow delete); 0 if unavailable.
    """
    try:
        process_state.metric_sender.send({
            "event_type": "request_complete",
            "server_id": server_mac,
            "client_ip": client_ip,
            "client_port": client_port,
            "ts": time.time(),
        })
    except Exception:
        log.exception("[flow-isolation] request_complete emission failed")


def _run_app_ready_probe(process_state, config) -> None:
    """RQ3 readiness: mark the app ready after a real MongoDB round-trip.

    The readiness predicate: /ready returns 200 iff
    ``process_state.app_ready`` is True, set here after a successful
    ``ping`` against the primary within ``READINESS_APP_MAX_S``.
    """
    # > controller READINESS_PROBE_MAX_S (120) so the edge never gives up
    # before the controller would abandon the backend.
    max_wait_s = float(os.environ.get("READINESS_APP_MAX_S", "180"))
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            _get_write_client(config.lan_id).admin.command("ping")
            process_state.mark_app_ready()
            log.info("app ready: MongoDB ping OK (lan=%s)", config.lan_id)
            return
        except Exception:
            time.sleep(1.0)
    log.error("app NOT ready: MongoDB ping failed within %.0fs", max_wait_s)


def _apply_request_concurrency_bound(server) -> None:
    """Bound the number of concurrent request threads.

    werkzeug's threaded server spawns one handler thread per connection with
    NO cap. Under an overload or retry-storm episode live threads grew into
    the thousands, filling the container's memory limit until the edge was
    killed and service was lost.

    The gate acquires a slot in the ACCEPT loop before a handler thread is
    spawned, so excess connections wait in the kernel accept backlog instead
    of consuming thread/memory. Replaces ThreadingMixIn.process_request with
    a bounded, faithful reimplementation. ``EDGE_MAX_CONCURRENCY`` (default
    256; 0 disables).

    A 1024 cap still allowed ~1024 live threads with 8 MB stacks (~300 MB
    resident) to fill a 512 MB limit, so the default was lowered to 256 and
    worker stacks shrunk via ``EDGE_THREAD_STACK_SIZE_KB`` (default 1024;
    0 = leave process default).
    """
    limit = int(os.environ.get("EDGE_MAX_CONCURRENCY", "256"))
    stack_kb = int(os.environ.get("EDGE_THREAD_STACK_SIZE_KB", "1024"))
    if stack_kb > 0:
        try:
            threading.stack_size(stack_kb * 1024)
            log.info("request-thread stack size set to %d KB", stack_kb)
        except (ValueError, RuntimeError) as exc:
            log.warning("request-thread stack size %d KB rejected: %s", stack_kb, exc)
    if limit <= 0:
        log.info("request-concurrency bound disabled (EDGE_MAX_CONCURRENCY=%d)", limit)
        return
    gate = threading.BoundedSemaphore(limit)
    state = {"blocked": 0, "logged": False}
    worker = server.process_request_thread

    def process_request(request, client_address):
        if not gate.acquire(blocking=False):
            state["blocked"] += 1
            if not state["logged"]:
                state["logged"] = True
                log.warning(
                    "request-concurrency gate engaged: waiting for a free request slot (limit=%d)",
                    limit,
                )
            gate.acquire()
        try:
            t = threading.Thread(
                target=process_request_thread,
                args=(request, client_address),
                daemon=getattr(server, "daemon_threads", True),
            )
            t.start()
        except BaseException:
            gate.release()
            try:
                server.shutdown_request(request)
            except Exception:
                pass
            raise

    def process_request_thread(request, client_address):
        try:
            worker(request, client_address)
        finally:
            gate.release()

    server.process_request = process_request
    server.process_request_thread = process_request_thread
    log.info("request-concurrency bound active: EDGE_MAX_CONCURRENCY=%d", limit)


if __name__ == "__main__":
    log.info(
        "Starting edge-server on %s:%d  lan=%s  db_name=%s  vip_data=%s"
        "  maxIdleTimeMS=%d  tau_dados=%.0fms",
        CONFIG.bind_host,
        CONFIG.bind_port,
        CONFIG.lan_id,
        CONFIG.db_name,
        snapshot_normal_vip_config(),
        CONFIG.max_idle_ms,
        CONFIG.tau_dados_ms,
    )
    # Bind before starting the readiness probe: app.run()'s dev-server path could
    # bind ~10 s after "Serving Flask app", so app_ready preceded servability.
    from werkzeug.serving import make_server
    # EDGE_READY_PREMATURE_S=N > 0 inverts that: the readiness claim precedes
    # servability by N s and the bind is deferred. 0 = off.
    _premature_s = 0
    try:
        _premature_s = max(
            0, int(os.environ.get("EDGE_READY_PREMATURE_S", "0") or 0)
        )
    except ValueError:
        log.warning("EDGE_READY_PREMATURE_S is not an int — treating as 0")
    if _premature_s > 0:
        log.info(
            "RQ3 premature claim: EDGE_READY_PREMATURE_S=%d (claim precedes bind)",
            _premature_s,
        )
        threading.Thread(
            target=_run_app_ready_probe, args=(process_state, CONFIG),
            daemon=True,
        ).start()
        try:
            _claim_max_s = float(os.environ.get("READINESS_APP_MAX_S", "180"))
        except ValueError:
            _claim_max_s = 180.0
            log.warning("READINESS_APP_MAX_S is not a float — using 180 s")
        _claim_deadline = time.monotonic() + _claim_max_s
        while not process_state.app_ready and time.monotonic() < _claim_deadline:
            time.sleep(0.05)
        _t_claim = time.time()
        if not process_state.app_ready:
            log.warning(
                "RQ3 premature claim: no claim within %.0fs (Mongo ping did not "
                "succeed) — binding without a premature claim",
                _claim_max_s,
            )
        log.info(
            "READINESS_CLAIM lan=%s premature_s=%d claimed=%d ts=%.3f",
            CONFIG.lan_id, _premature_s, int(process_state.app_ready), _t_claim,
        )
        time.sleep(_premature_s)
    import socket as _socket
    _t0 = time.perf_counter()
    _ai = _socket.getaddrinfo(
        CONFIG.bind_host, CONFIG.bind_port, _socket.AF_INET,
        _socket.SOCK_STREAM, _socket.IPPROTO_TCP,
    )
    _t1 = time.perf_counter()
    import importlib.metadata as _im
    _im.version("werkzeug")
    _t2 = time.perf_counter()
    # werkzeug's server_bind resolves a display name via getfqdn("0.0.0.0"), which
    # falls back to a gethostbyaddr DNS query — measured 5.3-10.3 s here.
    _saved_getfqdn = _socket.getfqdn

    def _fast_getfqdn(_name=""):
        return _socket.gethostname()

    _socket.getfqdn = _fast_getfqdn
    try:
        server = make_server(
            CONFIG.bind_host, CONFIG.bind_port, app, threaded=True,
        )
    finally:
        _socket.getfqdn = _saved_getfqdn
    _t_bind = time.time()
    _t3 = time.perf_counter()
    log.info(
        "bind-timing getaddrinfo=%.3fs importlib.metadata=%.3fs make_server(bind+listen)=%.3fs total=%.3fs",
        _t1 - _t0, _t2 - _t1, _t3 - _t2, _t3 - _t0,
    )
    _apply_request_concurrency_bound(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("edge-server listening: http://%s:%d", CONFIG.bind_host, CONFIG.bind_port)
    if _premature_s > 0:
        log.info(
            "READINESS_BIND lan=%s premature_s=%d bind_ts=%.3f deferral_s=%.3f",
            CONFIG.lan_id, _premature_s, _t_bind, _t_bind - _t_claim,
        )
    if _premature_s <= 0:
        # Started only after the socket is bound so app_ready cannot precede
        # servability (the default, fixed ordering).
        threading.Thread(
            target=_run_app_ready_probe, args=(process_state, CONFIG),
            daemon=True,
        ).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()