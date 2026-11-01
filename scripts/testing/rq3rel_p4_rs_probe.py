#!/usr/bin/env python3
"""rq3rel_p4_rs_probe.py — research_q3 post-run replica-set status probe.

After a research_q3 run, query the replica-set status from each LAN primary
storage container (``edge_storage_server_n1`` / ``edge_storage_server_n2``,
port 27018) via ``mongosh`` and write a JSON summary::

    {"lan1": [members...], "lan2": [members...], "ts": <unix>}

Members are projected to ``{name, state, stateStr}``. DOWN / ghost members
(stateStr not PRIMARY/SECONDARY and not ARBITER) are counted and reported as
``rs_probe: lan1 members=N down=K``. Missing containers or unparseable eval
output degrade gracefully: the LAN key becomes ``null`` (missing container) or
the raw eval text (parse fallback) with a warning to stderr.

Usage:
    python3 rq3rel_p4_rs_probe.py --run-dir <RUN_DIR> [--label <label>]

Output file: <RUN_DIR>/rs_status_<label|"post">.json. Plain stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# LAN primary storage containers (replica-set port 27018).
LAN_PRIMARIES = [
    ("lan1", "edge_storage_server_n1"),
    ("lan2", "edge_storage_server_n2"),
]

_EVAL_JS = ("JSON.stringify(rs.status().members.map(m=>({name:m.name,"
            "state:m.state,stateStr:m.stateStr})))")


def _member_is_down(member: dict) -> bool:
    """True for DOWN / ghost members (not PRIMARY, SECONDARY, or ARBITER)."""
    state_str = member.get("stateStr")
    return state_str != "PRIMARY" and state_str != "SECONDARY" \
        and state_str != "ARBITER"


def _probe_lan(lan: str, container: str):
    """Return (members, n, down) for one LAN primary.

    ``members`` is the parsed member list, ``null`` when the container is
    missing, or the raw eval text when the JSON does not parse.
    """
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", container, "mongosh", "--quiet",
             "--port=27018", "--eval", _EVAL_JS],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except FileNotFoundError:
        print(f"  WARNING: docker CLI not found — {lan} probe skipped", file=sys.stderr)
        return None, 0, 0
    except subprocess.TimeoutExpired:
        print(f"  WARNING: {container} probe timed out — {lan} probe skipped", file=sys.stderr)
        return None, 0, 0

    if proc.returncode != 0:
        _err = (proc.stderr.strip().splitlines()[-1]
                if proc.stderr.strip() else "no stderr")
        print(f"  WARNING: {container} unavailable ({_err}) — "
              f"{lan} probe skipped", file=sys.stderr)
        return None, 0, 0

    raw = proc.stdout.strip()
    if not raw:
        print(f"  WARNING: {container} returned empty output — {lan} probe skipped", file=sys.stderr)
        return None, 0, 0
    try:
        members = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  WARNING: {container} output is not JSON — storing raw text for {lan}", file=sys.stderr)
        return raw, 0, 0
    if not isinstance(members, list):
        print(f"  WARNING: {container} output is not a member list — storing raw text for {lan}", file=sys.stderr)
        return raw, 0, 0

    down = sum(1 for m in members
               if isinstance(m, dict) and _member_is_down(m))
    return members, len(members), down


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post-run replica-set status probe for research_q3 runs.")
    parser.add_argument("--run-dir", required=True,
                        help="Run folder to write rs_status_<label>.json into.")
    parser.add_argument("--label", default=None,
                        help="Run label suffix (default: 'post').")
    args = parser.parse_args()

    result = {"lan1": None, "lan2": None, "ts": time.time()}
    for lan, container in LAN_PRIMARIES:
        members, n, down = _probe_lan(lan, container)
        result[lan] = members
        if members is None:
            print(f"rs_probe: {lan} members=N/A (container missing/unreachable)")
        elif isinstance(members, str):
            print(f"rs_probe: {lan} members=N/A (raw output stored)")
        else:
            print(f"rs_probe: {lan} members={n} down={down}")

    out_path = os.path.join(args.run_dir,
                            f"rs_status_{args.label or 'post'}.json")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError as exc:
        print(f"FAIL: cannot write {out_path}: {exc}", file=sys.stderr)
        return 1
    print(f"rs_probe: wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
