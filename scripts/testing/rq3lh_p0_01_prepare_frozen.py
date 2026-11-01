#!/usr/bin/env python3
"""Prepare and attest an isolated RQ3 low-headroom worktree."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

TAG = "rq3-sat-preflight-20260808"
TAG_COMMIT = "55c22fbf3a5ab907ce90601fb31dea82ad705e2c"
CONTROLLER_COMMIT = "d26709935c72795bcf6d744c5b3b839aeb77f559"
PHASE_SOURCE = Path("source/scripts/testing/phases_override/phases_rq3_saturation.json")
PHASE_TARGET = Path("source/scripts/testing/phases.json")
PROTECTED = (
    "source/sdn_controller",
    "source/docker/edge_server",
    "source/docker/local_state_server",
    "source/scripts/network",
    "source/scripts/Makefile",
    "source/scripts/testing/run_experiment.sh",
)
ARM_ENVS = (
    "source/scripts/testing/controller_env_overrides/rq3sat_direct.env",
    "source/scripts/testing/controller_env_overrides/rq3sat_discovery.env",
    "source/scripts/testing/controller_env_overrides/rq3lh_direct_event_absent.env",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(worktree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(worktree), *args], text=True
    ).strip()


def validate_phases(data: object) -> list[dict]:
    phases = data.get("phases", data) if isinstance(data, dict) else data
    if not isinstance(phases, list) or [p.get("name") for p in phases] != [
        "baseline", "compute_plateau", "recovery_gap", "demand_drop", "idle_tail"
    ]:
        raise ValueError("historical P4 phase names/order do not match")
    by_name = {p["name"]: p for p in phases}
    expected = {
        "baseline": (60, 1.0),
        "compute_plateau": (600, 1.5),
        "recovery_gap": (120, 0.5),
        "demand_drop": (420, 1.0),
        "idle_tail": (420, 0.05),
    }
    for name, (duration, rate) in expected.items():
        phase = by_name[name]
        if phase.get("duration_s") != duration or phase.get("rate_per_client") != rate:
            raise ValueError(f"unexpected P4 values in {name}")
    if by_name["compute_plateau"].get("mix") != {"service_pressure": 1.0}:
        raise ValueError("compute_plateau is not compute-pure service_pressure=1.0")
    return phases


def tree_hashes(worktree: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in PROTECTED:
        path = worktree / relative
        paths = [path] if path.is_file() else sorted(path.rglob("*"))
        for child in paths:
            if child.is_file():
                result[str(child.relative_to(worktree)).replace("\\", "/")] = sha256(child)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    worktree = args.worktree.resolve()
    if not worktree.is_dir():
        raise SystemExit(f"ERROR: worktree does not exist: {worktree}")
    source = worktree / PHASE_SOURCE
    target = worktree / PHASE_TARGET
    manifest_path = (args.manifest or worktree / ".rq3lh_frozen_manifest.json").resolve()
    if not source.exists():
        raise SystemExit(f"ERROR: missing phase source: {source}")
    phases = validate_phases(json.loads(source.read_text(encoding="utf-8")))
    tag_commit = run_git(worktree, "rev-parse", f"refs/tags/{TAG}")
    if tag_commit != TAG_COMMIT:
        raise SystemExit(f"ERROR: tag resolves to {tag_commit}, expected {TAG_COMMIT}")
    controller_diff = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--quiet", CONTROLLER_COMMIT, TAG_COMMIT,
         "--", "source/sdn_controller"]
    )
    if controller_diff.returncode != 0:
        raise SystemExit("ERROR: controller subtree differs from archived controller state")
    if not args.dry_run:
        target.write_text(json.dumps({"phases": phases}, indent=2) + "\n", encoding="utf-8")
    phase_hash = sha256(target) if target.exists() else "pending-materialization"
    manifest = {
        "dry_run": args.dry_run,
        "worktree_root": str(worktree),
        "tag": TAG,
        "tag_commit": TAG_COMMIT,
        "controller_commit": CONTROLLER_COMMIT,
        "phase_source": str(PHASE_SOURCE).replace("\\", "/"),
        "phase_target": str(PHASE_TARGET).replace("\\", "/"),
        "phase_sha256": phase_hash,
        "protected_sha256": tree_hashes(worktree),
        "arm_env_sha256": {
            relative: sha256(worktree / relative)
            for relative in ARM_ENVS if (worktree / relative).exists()
        },
        "p4_phases": phases,
    }
    if not args.dry_run:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
