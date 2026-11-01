#!/usr/bin/env python3
"""
traffic_generator.py

Sends phased HTTP traffic from Linux network namespaces through VIP_SERVER.
Each request is spawned as `ip netns exec <ns> curl ...` so the SDN controller
sees the correct source IP and routes via the VIP mechanism.

Requires root (for ip netns exec).

Usage:
    sudo python3 traffic_generator.py \
      --config phases.json \
      --clients-lan1 test_client_1,test_client_2,test_client_3 \
      --clients-lan2 test_client_4,test_client_5,test_client_6 \
      --snapshot-dir data/workload_snapshot \
      --output metrics/client_requests.csv \
      [--vip-lan1 10.0.0.253:5000] \
      [--vip-lan2 10.0.1.253:5000] \
      [--dry-run]
"""

import argparse
import asyncio
import csv
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Snapshot:
    """Pre-loaded content/user data from exported JSON."""

    content_ids_by_region: dict = field(default_factory=dict)
    user_ids_by_region: dict = field(default_factory=dict)

    @classmethod
    def load(cls, snapshot_dir: str) -> "Snapshot":
        with open(os.path.join(snapshot_dir, "content_items.json")) as f:
            content_items = json.load(f)
        with open(os.path.join(snapshot_dir, "user_profiles.json")) as f:
            user_profiles = json.load(f)

        snap = cls()

        for item in content_items:
            region = item["region_origin"]
            snap.content_ids_by_region.setdefault(region, []).append(item["_id"])

        for profile in user_profiles:
            region = profile["home_region"]
            snap.user_ids_by_region.setdefault(region, []).append(profile["_id"])

        return snap

    @classmethod
    def mock(cls, n_content_items: int = 50, n_users: int = 20) -> "Snapshot":
        """Return synthetic snapshot data for dry-run testing without real files."""
        snap = cls()
        for region in ("lan1", "lan2"):
            snap.content_ids_by_region[region] = [
                f"{region}::content::{i:03d}" for i in range(1, n_content_items + 1)
            ]
            snap.user_ids_by_region[region] = [
                f"{region}::user::{i:03d}" for i in range(1, n_users + 1)
            ]
        return snap


@dataclass
class PhaseConfig:
    name: str
    duration_s: int
    rate_per_client: float
    cross_region_ratio: float
    hotspot_direction: str
    mix: dict
    client_fraction: float = 1.0

    @classmethod
    def from_dict(cls, d: dict) -> "PhaseConfig":
        hotspot_direction = d.get("hotspot_direction") or "bidirectional"
        if hotspot_direction not in {"bidirectional", "lan2_to_lan1", "lan1_to_lan2"}:
            raise ValueError(
                "hotspot_direction must be bidirectional, lan2_to_lan1, lan1_to_lan2, or blank"
            )
        return cls(
            name=d["name"],
            duration_s=d["duration_s"],
            rate_per_client=d["rate_per_client"],
            cross_region_ratio=d.get("cross_region_ratio", 0.0),
            hotspot_direction=hotspot_direction,
            mix=d["mix"],
            client_fraction=d.get("client_fraction", 1.0),
        )


def pick_request_type(mix: dict) -> str:
    """Weighted random selection from the mix distribution."""
    r = random.random()
    cumulative = 0.0
    for req_type, weight in mix.items():
        cumulative += weight
        if r <= cumulative:
            return req_type
    return list(mix.keys())[-1]


def pick_target(client_lan: str, phase: PhaseConfig, snap: Snapshot, request_type: str) -> dict:
    """Select content_id, user_id, and target_region for one request."""
    home = client_lan
    foreign = "lan2" if home == "lan1" else "lan1"

    if request_type == "content_lookup":
        is_cross = random.random() < phase.cross_region_ratio

        # Blank hotspot_direction = both LANs emit cross-region lookups under the
        # shared cross_region_ratio; a directional override pins one source LAN.
        if phase.hotspot_direction == "lan2_to_lan1" and home == "lan1":
            is_cross = False
        elif phase.hotspot_direction == "lan1_to_lan2" and home == "lan2":
            is_cross = False

        target_lan = foreign if is_cross else home
        content_id = random.choice(snap.content_ids_by_region[target_lan])
        user_id = random.choice(snap.user_ids_by_region[home])
        return {"content_id": content_id, "user_id": user_id, "target_region": target_lan}

    if request_type == "feed_ranking":
        user_id = random.choice(snap.user_ids_by_region[home])
        return {"content_id": "", "user_id": user_id, "target_region": home}

    if request_type == "service_pressure":
        return {"content_id": "", "user_id": "", "target_region": home}

    if request_type == "hash_challenge":
        # Request-bound nonce for the CPU hashing press (RQ2 v4); fresh per
        # request so caching cannot flatten the cost.
        return {
            "content_id": "",
            "user_id": "",
            "target_region": home,
            "nonce": "%016x" % random.getrandbits(64),
        }

    if request_type == "content_update":
        content_id = random.choice(snap.content_ids_by_region[home])
        return {"content_id": content_id, "user_id": "", "target_region": home}

    if request_type == "content_aggregate":
        # Aggregation is collection-level: no content item, and the target region is
        # always local (the client's own LAN MongoDB; the aggregator never crosses).
        return {
            "content_id": "",
            "user_id": "",
            "target_region": client_lan,
        }

    raise ValueError(f"Unsupported request type: {request_type}")


def build_url(vip: str, request_type: str, target: dict) -> str:
    """Build the full URL for a request."""
    base = f"http://{vip}"

    if request_type == "content_lookup":
        return f"{base}/content/{target['content_id']}?requester={target['user_id']}"
    if request_type == "feed_ranking":
        return f"{base}/feed/{target['user_id']}?limit=10"
    if request_type == "service_pressure":
        return f"{base}/service_pressure?window_min=1&limit=10"
    if request_type == "hash_challenge":
        return f"{base}/compute/hash?nonce={target['nonce']}"
    if request_type == "content_update":
        return f"{base}/content"
    if request_type == "content_aggregate":
        return f"{base}/content/aggregate"

    raise ValueError(f"Unsupported request type: {request_type}")


_curl_warn_shown = False


async def exec_curl(ns: str, url: str, dry_run: bool = False, body: str | None = None) -> tuple:
    """Execute curl inside a network namespace.

    Returns ``(http_status, latency_s, backend_id, source_port)``.
    *backend_id* is the value of the ``X-Backend-ID`` response header, or
    ``"unknown"`` if the header is absent (e.g. connection failure).
    *source_port* is the curl local source port (``%{local_port}``), used by
    ``rq3_flow_validation.py`` Check D to verify one fresh TCP connection per
    request (RQ3 flow isolation).

    When *body* is not None the request is sent as POST with
    ``Content-Type: application/json``.
    """
    global _curl_warn_shown
    curl_max_time = os.environ.get("CURL_MAX_TIME") or "30"

    cmd = [
        "ip", "netns", "exec", ns,
        "curl", "-s", "-o", "/dev/null", "-D", "-",
        "-w", "\n%{http_code} %{time_total} %{local_port}",
        "--max-time", curl_max_time,
    ]
    if body is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json", "-d", body]
    cmd.append(url)

    if dry_run:
        print(f"[DRY-RUN] {' '.join(cmd)}")
        return 200, 0.0, "dry_run", 0

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    output = stdout.decode().strip()

    # Parse -D - headers (X-Backend-ID) before the blank-line separator; the -w
    # output is the last line, prefixed with \n so it never merges into headers.
    backend_id = "unknown"
    header_lines: list[str] = []
    in_headers = True
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped == "":
            in_headers = False
            continue
        if in_headers:
            header_lines.append(stripped)
    for hdr in header_lines:
        if hdr.lower().startswith("x-backend-id:"):
            backend_id = hdr.split(":", 1)[1].strip()
            break

    last_line = output.split("\n")[-1].strip() if output else ""
    parts = last_line.split()

    if len(parts) == 3:
        try:
            return int(parts[0]), float(parts[1]), backend_id, int(parts[2])
        except ValueError:
            pass

    # Diagnostic: show why parsing failed (only first occurrence per client)
    if not _curl_warn_shown:
        _curl_warn_shown = True
        err = stderr.decode().strip()[:200] if stderr else "(empty)"
        print(f"  [DIAG] curl parse failed in {ns} (rc={proc.returncode})")
        print(f"         stdout={output[:200]!r}")
        print(f"         stderr={err!r}")

    return 0, 0.0, backend_id, 0


async def client_loop(
    ns: str,
    client_lan: str,
    phase: PhaseConfig,
    snap: Snapshot,
    vip: str,
    csv_targets,
    csv_lock: asyncio.Lock,
    dry_run: bool,
):
    """One async task per client namespace for a single phase."""
    if dry_run:
        # Bounded preview mode: emit each active request type once so validation
        # can confirm the renamed surface without replaying full phase timing.
        for req_type, weight in phase.mix.items():
            if weight <= 0:
                continue

            target = pick_target(client_lan, phase, snap, req_type)
            url = build_url(vip, req_type, target)

            body = None
            if req_type == "content_update":
                update_padding = "x" * 1024  # 1KB of padding to inflate oplog entries
                body = (
                    f'{{"content_id":"{target["content_id"]}",'
                    f'"engagement":{random.randint(0,100)},'
                    f'"lan":"{client_lan}",'
                    f'"update_padding":"{update_padding}"}}'
                )
            if req_type == "content_aggregate":
                body = (
                    f'{{"lan":"{client_lan}",'
                    f'"engagement_threshold":{random.randint(30,70)}}}'
                )

            sent_at = datetime.now(timezone.utc).isoformat()
            phase_name = phase.name

            http_status, latency_s, backend_id, source_port = await exec_curl(
                ns, url, dry_run, body)

            row = [
                sent_at,
                phase_name,
                ns,
                client_lan,
                req_type,
                target.get("content_id", ""),
                target.get("user_id", ""),
                target.get("target_region", ""),
                http_status,
                round(latency_s, 4),
                datetime.now(timezone.utc).isoformat(),
                backend_id,
                source_port,
            ]
            async with csv_lock:
                for csv_writer, csv_file in csv_targets:
                    csv_writer.writerow(row)
                for _, csv_file in csv_targets:
                    csv_file.flush()
        return

    phase_end = time.monotonic() + phase.duration_s
    interval = 1.0 / phase.rate_per_client
    request_count = 0
    last_log = time.monotonic()
    log_interval = 10  # seconds between progress logs

    while time.monotonic() < phase_end:
        t0 = time.monotonic()

        req_type = pick_request_type(phase.mix)
        target = pick_target(client_lan, phase, snap, req_type)
        url = build_url(vip, req_type, target)

        body = None
        if req_type == "content_update":
            update_padding = "x" * 1024  # 1KB of padding to inflate oplog entries
            body = (
                f'{{"content_id":"{target["content_id"]}",'
                f'"engagement":{random.randint(0,100)},'
                f'"lan":"{client_lan}",'
                f'"update_padding":"{update_padding}"}}'
            )
        if req_type == "content_aggregate":
            body = (
                f'{{"lan":"{client_lan}",'
                f'"engagement_threshold":{random.randint(30,70)}}}'
            )
        sent_at = datetime.now(timezone.utc).isoformat()
        phase_name = phase.name
        http_status, latency_s, backend_id, source_port = await exec_curl(
            ns, url, dry_run, body)
        request_count += 1

        row = [
            sent_at,
            phase_name,
            ns,
            client_lan,
            req_type,
            target.get("content_id", ""),
            target.get("user_id", ""),
            target.get("target_region", ""),
            http_status,
            round(latency_s, 4),
            datetime.now(timezone.utc).isoformat(),
            backend_id,
            source_port,
        ]
        async with csv_lock:
            for csv_writer, csv_file in csv_targets:
                csv_writer.writerow(row)
            for _, csv_file in csv_targets:
                csv_file.flush()

        now = time.monotonic()
        remaining = max(0, phase_end - now)
        if now - last_log >= log_interval:
            print(f"  [{ns}] {request_count} reqs sent, "
                  f"{int(remaining)}s remaining, last status={http_status}")
            last_log = now

        elapsed = time.monotonic() - t0
        sleep_time = max(0.0, interval - elapsed + random.uniform(-0.05, 0.05))
        await asyncio.sleep(sleep_time)


async def run(args):
    if args.driver_mode == "open_loop":
        return await run_open_loop(args)

    if args.random_seed is not None:
        random.seed(args.random_seed)
        print(f"Random seed fixed: {args.random_seed}")

    with open(args.config) as f:
        raw = json.load(f)
    phases = [PhaseConfig.from_dict(p) for p in raw["phases"]]

    if args.dry_run:
        try:
            snap = Snapshot.load(args.snapshot_dir)
        except FileNotFoundError:
            snap = Snapshot.mock()
            print("[DRY-RUN] Snapshot files not found — using synthetic data")
    else:
        snap = Snapshot.load(args.snapshot_dir)
    n_content_items = sum(len(v) for v in snap.content_ids_by_region.values())
    n_users = sum(len(v) for v in snap.user_ids_by_region.values())
    print(f"Snapshot: {n_content_items} content items, {n_users} user profiles")

    lan1_clients = [c for c in args.clients_lan1.split(",") if c] if args.clients_lan1 else []
    lan2_clients = [c for c in args.clients_lan2.split(",") if c] if args.clients_lan2 else []
    all_clients = [(ns, "lan1") for ns in lan1_clients] + [(ns, "lan2") for ns in lan2_clients]

    if not all_clients:
        print("ERROR: no clients specified (use --clients-lan1 and/or --clients-lan2)")
        return

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    csv_lock = asyncio.Lock()

    total_s = sum(p.duration_s for p in phases)
    print(f"{len(phases)} phases, {total_s}s total, {len(all_clients)} clients")
    for p in phases:
        print(f"  {p.name}: {p.duration_s}s @ {p.rate_per_client} req/s/client, "
              f"cross_region={p.cross_region_ratio}")

    # Phase file: signals the current phase to sibling processes (e.g. resource stats collector)
    phase_state_file = os.path.join(output_dir, "current_phase.txt") if output_dir else "current_phase.txt"
    header = [
        "sent_at", "phase", "client_ns", "client_lan", "endpoint",
        "content_id", "user_id", "target_region", "http_status", "latency_s",
        "completed_at", "backend_id", "source_port",
    ]

    aggregate_file = open(args.output, "w", newline="")
    aggregate_writer = csv.writer(aggregate_file)
    aggregate_writer.writerow(header)
    csv_targets = [(aggregate_writer, aggregate_file)]

    try:
        for i, phase in enumerate(phases):
            with open(phase_state_file, "w") as pf:
                pf.write(phase.name)

            # client_fraction < 1.0 idles some clients; sampling is per-LAN so the
            # active set stays balanced instead of skewing toward one LAN.
            fraction = getattr(phase, 'client_fraction', 1.0)
            if fraction < 1.0:
                n_lan1 = max(1, int(len(lan1_clients) * fraction))
                n_lan2 = max(1, int(len(lan2_clients) * fraction))
                lan1_active = [(ns, lan) for ns, lan in random.sample(
                    [(ns, "lan1") for ns in lan1_clients], n_lan1)]
                lan2_active = [(ns, lan) for ns, lan in random.sample(
                    [(ns, "lan2") for ns in lan2_clients], n_lan2)]
                phase_clients = lan1_active + lan2_active
            else:
                phase_clients = all_clients

            print(f"\n{'='*60}")
            print(f"Phase {i + 1}/{len(phases)}: {phase.name} ({phase.duration_s}s)")
            print(f"  Output: {args.output}")
            if fraction < 1.0:
                print(f"  Clients: {len(phase_clients)}/{len(all_clients)} active (fraction={fraction})")
            print(f"{'='*60}")

            tasks = [
                asyncio.create_task(
                    client_loop(ns, lan, phase, snap,
                                args.vip_lan1 if lan == "lan1" else args.vip_lan2,
                                csv_targets, csv_lock, args.dry_run)
                )
                for ns, lan in phase_clients
            ]
            await asyncio.gather(*tasks)
    finally:
        aggregate_file.close()

    # Signal that all phases are complete
    with open(phase_state_file, "w") as pf:
        pf.write("idle")

    print("\nDone. Results written to:")
    print(f"  {args.output}")


# open_loop: the supervisor writes a per-phase mask + schedule and merges worker
# CSVs; workers append a 14th `status` column (completed|timeout|dropped|canceled).

_OPEN_LOOP_CSV_HEADER = [
    "sent_at", "phase", "client_ns", "client_lan", "endpoint",
    "content_id", "user_id", "target_region", "http_status", "latency_s",
    "completed_at", "backend_id", "source_port", "status",
]


def _compute_active_masks(phases, all_clients, base_seed):
    """Per-phase active (ns, lan) list sampled with a dedicated RNG so the
    per-client request RNG stream is never consumed by mask sampling."""
    rng = random.Random(base_seed)
    lan1 = [(ns, lan) for ns, lan in all_clients if lan == "lan1"]
    lan2 = [(ns, lan) for ns, lan in all_clients if lan == "lan2"]
    masks = []
    for phase in phases:
        fraction = getattr(phase, "client_fraction", 1.0)
        if fraction < 1.0:
            n1 = max(1, int(len(lan1) * fraction))
            n2 = max(1, int(len(lan2) * fraction))
            masks.append(rng.sample(lan1, n1) + rng.sample(lan2, n2))
        else:
            masks.append(all_clients)
    return masks


def _build_body(request_type, target, client_lan):
    """Request body for POST endpoints (None for GET endpoints)."""
    if request_type == "content_update":
        return {
            "content_id": target["content_id"],
            "engagement": random.randint(0, 100),
            "lan": client_lan,
            "update_padding": "x" * 1024,
        }
    if request_type == "content_aggregate":
        return {
            "lan": client_lan,
            "engagement_threshold": random.randint(30, 70),
        }
    return None


async def run_open_loop(args):
    """Supervisor: schedule file + one worker subprocess per client netns,
    then merge per-worker CSVs into the final output."""
    with open(args.config) as f:
        raw = json.load(f)
    phases = [PhaseConfig.from_dict(p) for p in raw["phases"]]
    if args.dry_run:
        snap = Snapshot.mock()
    else:
        snap = Snapshot.load(args.snapshot_dir)
    n_content_items = sum(len(v) for v in snap.content_ids_by_region.values())
    n_users = sum(len(v) for v in snap.user_ids_by_region.values())
    print(f"Snapshot: {n_content_items} content items, {n_users} user profiles")

    lan1_clients = [c for c in args.clients_lan1.split(",") if c] if args.clients_lan1 else []
    lan2_clients = [c for c in args.clients_lan2.split(",") if c] if args.clients_lan2 else []
    all_clients = [(ns, "lan1") for ns in lan1_clients] + [(ns, "lan2") for ns in lan2_clients]
    if not all_clients:
        print("ERROR: no clients specified (use --clients-lan1 and/or --clients-lan2)")
        return 1

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    base_seed = args.random_seed if args.random_seed is not None else 42
    masks = _compute_active_masks(phases, all_clients, base_seed)

    sched_path = os.path.join(output_dir or ".", "open_loop_schedule.json")
    sched = {
        "base_seed": base_seed,
        "drain_s": args.drain_s,
        "in_flight_window": args.in_flight_window,
        "phases": [p.__dict__ for p in phases],
        "active_masks": [[ns for ns, _ in m] for m in masks],
    }
    with open(sched_path, "w") as f:
        json.dump(sched, f)

    phase_state_file = os.path.join(output_dir or ".", "current_phase.txt")
    print(f"{len(phases)} phases, {len(all_clients)} clients; "
          f"open-loop supervisor (window={args.in_flight_window}, drain={args.drain_s}s)")
    for i, p in enumerate(phases):
        print(f"  {p.name}: {p.duration_s}s @ {p.rate_per_client} req/s/client, "
              f"{len(masks[i])}/{len(all_clients)} active")

    procs = []
    worker_csvs = []
    for idx, (ns, lan) in enumerate(all_clients):
        worker_csv = os.path.join(output_dir or ".", f"client_requests_{ns}.csv")
        worker_csvs.append(worker_csv)
        vip = args.vip_lan1 if lan == "lan1" else args.vip_lan2
        cmd = [
            "ip", "netns", "exec", ns,
            sys.executable, os.path.abspath(__file__),
            "--worker",
            "--client-ns", ns,
            "--client-lan", lan,
            "--ns-index", str(idx),
            "--vip", vip,
            "--schedule-file", sched_path,
            "--snapshot-dir", args.snapshot_dir,
            "--output", worker_csv,
            "--driver-mode", "open_loop",
            "--in-flight-window", str(args.in_flight_window),
            "--drain-s", str(args.drain_s),
            "--phase-state-file", phase_state_file,
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        print(f"  launching worker: {' '.join(cmd)}")
        procs.append(await asyncio.create_subprocess_exec(*cmd))

    rc = 0
    for i, p in enumerate(procs):
        code = await p.wait()
        if code != 0:
            rc = code
            # A worker failed — terminate the remaining workers to bound the
            # run and surface the error promptly.
            for other in procs[i + 1:]:
                if other.returncode is None:
                    other.terminate()
            for other in procs[i + 1:]:
                try:
                    await other.wait()
                except ProcessLookupError:
                    pass
            break
    if rc != 0:
        print(f"ERROR: open-loop worker(s) failed (rc={rc})")
        return rc

    with open(args.output, "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(_OPEN_LOOP_CSV_HEADER)
        for wcsv in worker_csvs:
            if not os.path.exists(wcsv):
                print(f"  [WARN] missing worker CSV: {wcsv}")
                continue
            with open(wcsv, newline="") as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    if row:
                        writer.writerow(row)
    print(f"\nDone (open-loop). Merged {len(worker_csvs)} worker CSVs -> {args.output}")
    return 0


async def _worker_main(args):
    """Per-netns worker: schedule-preserving dispatcher with aiohttp."""
    try:
        import aiohttp
    except ImportError:
        print("ERROR: open_loop worker requires aiohttp (pip install aiohttp)")
        return 1

    with open(args.schedule_file) as f:
        sched = json.load(f)
    phases = [PhaseConfig.from_dict(p) for p in sched["phases"]]
    active_sets = sched["active_masks"]
    random.seed(int(sched["base_seed"]) + args.ns_index)

    if args.dry_run:
        snap = Snapshot.mock()
    else:
        snap = Snapshot.load(args.snapshot_dir)

    out_f = open(args.output, "w", newline="")
    writer = csv.writer(out_f)
    writer.writerow(_OPEN_LOOP_CSV_HEADER)

    curl_max_time = float(os.environ.get("CURL_MAX_TIME") or "300")
    window = max(1, args.in_flight_window)
    drain_s = max(0.0, args.drain_s)

    # aiohttp DEFAULT_TIMEOUT is ClientTimeout(total=300, sock_connect=30): a stalled
    # handshake aborts at exactly 30 s — a client-side artifact that dominated the
    # plateau failure metric (~5 %). RQ1 sets 300; unset keeps the aiohttp default.
    _sock_connect_env = os.environ.get("AIOHTTP_SOCK_CONNECT_TIMEOUT")
    _session_timeout = aiohttp.ClientTimeout(
        total=curl_max_time,
        sock_connect=float(_sock_connect_env) if _sock_connect_env else 30,
    )

    connector = aiohttp.TCPConnector(force_close=True)
    # RQ3 flow validation needs the client source port, but reading it from the
    # response often returns nothing under flow isolation (the per-request flow
    # delete can reset the connection), so record it at CONNECTION CREATION.
    _conn_ports: dict[int, int] = {}

    class _PortRecordingConnector(aiohttp.TCPConnector):
        async def _create_connection(self, req, traces, timeout):
            protocol = await super()._create_connection(req, traces, timeout)
            try:
                transport = getattr(protocol, "transport", None)
                sockname = (transport.get_extra_info("sockname")
                            if transport is not None else None)
                if sockname:
                    if len(_conn_ports) > 8192:
                        _conn_ports.clear()
                    _conn_ports[id(protocol)] = int(sockname[1])
            except Exception:
                pass
            return protocol

    session = aiohttp.ClientSession(
        connector=_PortRecordingConnector(force_close=True),
        timeout=_session_timeout)
    sem = asyncio.Semaphore(window)
    pending: set = set()
    stop_dispatch = asyncio.Event()

    def write_row(parts):
        writer.writerow(parts)
        out_f.flush()

    async def dispatch_one(phase, req_type, target, body, sent_at_iso):
        if sem.locked():
            # In-flight window full -> client-side admission (reported separately).
            # No await between locked() and the drop decision, so this is atomic
            # in single-threaded asyncio.
            write_row([sent_at_iso, phase.name, args.client_ns, args.client_lan, req_type,
                       target.get("content_id", ""), target.get("user_id", ""),
                       target.get("target_region", ""), "", "", "", "", "", "dropped"])
            return
        await sem.acquire()
        try:
            t0 = time.monotonic()
            try:
                url = build_url(args.vip, req_type, target)
                if body is not None:
                    resp = await asyncio.wait_for(
                        session.post(url, json=body), timeout=curl_max_time)
                else:
                    resp = await asyncio.wait_for(
                        session.get(url), timeout=curl_max_time)
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                write_row([sent_at_iso, phase.name, args.client_ns, args.client_lan, req_type,
                           target.get("content_id", ""), target.get("user_id", ""),
                           target.get("target_region", ""), "000", round(elapsed, 4),
                           datetime.now(timezone.utc).isoformat(), "unknown", 0, "timeout"])
                return
            except Exception:
                elapsed = time.monotonic() - t0
                write_row([sent_at_iso, phase.name, args.client_ns, args.client_lan, req_type,
                           target.get("content_id", ""), target.get("user_id", ""),
                           target.get("target_region", ""), "000", round(elapsed, 4),
                           datetime.now(timezone.utc).isoformat(), "unknown", 0, "completed"])
                return
            latency = time.monotonic() - t0
            backend_id = resp.headers.get("X-Backend-ID", "unknown")
            source_port = 0
            conn = resp.connection
            if conn is not None:
                source_port = _conn_ports.pop(id(conn._protocol), 0)
            if not source_port:
                # Fallback: read the transport directly (pre-record path).
                try:
                    transport = conn._protocol.transport if conn is not None else None
                    sockname = (transport.get_extra_info("sockname")
                                if transport is not None else None)
                    if sockname:
                        source_port = int(sockname[1])
                except Exception:
                    source_port = 0
            write_row([sent_at_iso, phase.name, args.client_ns, args.client_lan, req_type,
                       target.get("content_id", ""), target.get("user_id", ""),
                       target.get("target_region", ""), str(resp.status), round(latency, 4),
                       datetime.now(timezone.utc).isoformat(), backend_id, source_port, "completed"])
        except asyncio.CancelledError:
            write_row([sent_at_iso, phase.name, args.client_ns, args.client_lan, req_type,
                       target.get("content_id", ""), target.get("user_id", ""),
                       target.get("target_region", ""), "", "", "", "", "", "canceled"])
            raise
        finally:
            sem.release()

    async def dispatch_phase(phase):
        stop_dispatch.clear()
        phase_end = time.monotonic() + phase.duration_s
        interval = 1.0 / phase.rate_per_client if phase.rate_per_client > 0 else 1.0
        while time.monotonic() < phase_end and not stop_dispatch.is_set():
            req_type = pick_request_type(phase.mix)
            target = pick_target(args.client_lan, phase, snap, req_type)
            body = _build_body(req_type, target, args.client_lan)
            sent_at_iso = datetime.now(timezone.utc).isoformat()
            task = asyncio.create_task(
                dispatch_one(phase, req_type, target, body, sent_at_iso))
            pending.add(task)
            task.add_done_callback(pending.discard)
            await asyncio.sleep(max(0.0, interval + random.uniform(-0.05, 0.05)))

    async def drain():
        stop_dispatch.set()
        deadline = time.monotonic() + drain_s
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        for t in list(pending):
            t.cancel()
        for t in list(pending):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    total_s = sum(p.duration_s for p in phases)
    print(f"[{args.client_ns}] worker start: {len(phases)} phases, {total_s}s")
    for i, phase in enumerate(phases):
        await drain()
        with open(args.phase_state_file, "w") as pf:
            pf.write(phase.name)
        if args.client_ns in active_sets[i]:
            print(f"[{args.client_ns}] ACTIVE in {phase.name} ({phase.duration_s}s)")
            if args.dry_run:
                # Match the sync driver's dry-run semantics: print, do NOT
                # dispatch (open-loop would otherwise fire real requests).
                print(f"[{args.client_ns}] dry-run: skipping dispatch for {phase.name}")
                await asyncio.sleep(phase.duration_s)
            else:
                await dispatch_phase(phase)
        else:
            print(f"[{args.client_ns}] idle in {phase.name} ({phase.duration_s}s)")
            await asyncio.sleep(phase.duration_s)
    await drain()
    await session.close()
    out_f.close()
    print(f"[{args.client_ns}] worker done -> {args.output}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Traffic generator for the edge content-discovery workload experiment"
    )
    parser.add_argument("--config", required=False, help="Path to phases.json (supervisor only)")
    parser.add_argument(
        "--clients-lan1", default="",
        help="Comma-separated LAN1 namespace names (e.g. test_client_1,test_client_2)"
    )
    parser.add_argument(
        "--clients-lan2", default="",
        help="Comma-separated LAN2 namespace names (e.g. test_client_4,test_client_5)"
    )
    parser.add_argument("--snapshot-dir", default="data/workload_snapshot", metavar="DIR")
    parser.add_argument("--output", default="metrics/client_requests.csv", metavar="FILE")
    parser.add_argument(
        "--vip-lan1", default="10.0.0.253:5000",
        help="VIP_SERVER_N1 address:port for LAN1 clients (default: 10.0.0.253:5000)"
    )
    parser.add_argument(
        "--vip-lan2", default="10.0.1.253:5000",
        help="VIP_SERVER_N2 address:port for LAN2 clients (default: 10.0.1.253:5000)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print curl commands without executing them"
    )
    parser.add_argument(
        "--random-seed", type=int, default=None,
        help="Fixed random seed for reproducible request sequences (default: system random)"
    )
    parser.add_argument(
        "--driver-mode", choices=["sync", "open_loop"], default="sync",
        help="Traffic driver mode: sync (legacy stop-and-wait) or open_loop "
             "(schedule-preserving, independent of response latency)"
    )
    parser.add_argument(
        "--worker", action="store_true",
        help="[open_loop] internal: run as a per-netns worker subprocess"
    )
    parser.add_argument(
        "--client-ns", default="",
        help="[open_loop worker] this worker's network namespace"
    )
    parser.add_argument(
        "--client-lan", default="",
        help="[open_loop worker] this worker's LAN (lan1|lan2)"
    )
    parser.add_argument(
        "--ns-index", type=int, default=0,
        help="[open_loop worker] netns index for per-client seeding (base + index)"
    )
    parser.add_argument(
        "--vip", default="",
        help="[open_loop worker] VIP address:port for this worker's LAN"
    )
    parser.add_argument(
        "--schedule-file", default="",
        help="[open_loop worker] supervisor-written schedule JSON"
    )
    parser.add_argument(
        "--phase-state-file", default="current_phase.txt",
        help="[open_loop worker] shared current-phase marker file"
    )
    parser.add_argument(
        "--in-flight-window", type=int, default=1024,
        help="[open_loop] max in-flight requests per client (window)"
    )
    parser.add_argument(
        "--drain-s", type=float, default=30.0,
        help="[open_loop] seconds to drain in-flight requests at each phase boundary"
    )

    args = parser.parse_args()
    if not args.worker and not args.config:
        # --config is only required for the supervisor; workers read the schedule
        # file. Guarding here (not inside run()) keeps the error at parse time.
        parser.error("--config is required unless running as a worker (--worker)")
    if args.worker:
        asyncio.run(_worker_main(args))
    else:
        asyncio.run(run(args))
