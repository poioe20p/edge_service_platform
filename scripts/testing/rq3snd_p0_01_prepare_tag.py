#!/usr/bin/env python3
"""Prepare and attest the isolated RQ3 soundness worktree/tag.

The frozen base is the final robustness attestation commit 852a8a3; the
soundness family adds the two pre-specified app fault knobs
(EDGE_READY_PREMATURE_S / EDGE_READY_SEMANTIC_LIE_S, both default-off) plus
the optional controller wake_verify admission mode. All reused env deltas stay
byte-identical to Family 4; only the new rq3snd_* files are added. This script
hashes the protected surface of plan §5 P0(4) — including the post-restore
phases.json — with the same manifest structure and hash algorithm as the
robustness/qoe/timing prepare_tag so the attestation is directly comparable.
Before hashing it verifies plan §5 P0(2): HEAD == tag and `git status
--porcelain` shows exactly the declared phases.json restore (--no-git-checks
is a debugging escape hatch). Protected paths that are absent are recorded in
the manifest `missing` list (a non-empty list fails the preflight) instead of
being silently skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

PROTECTED = (
    "source/docker/edge_server/source/app.py",
    "source/docker/edge_server/source/edge_server_process_state.py",
    "source/sdn_controller/readiness_gate.py",
    "source/sdn_controller/control_events.py",
    "source/sdn_controller/scaling_config.py",
    "source/sdn_controller/main_n1.py",
    "source/sdn_controller/main_n2.py",
    "source/sdn_controller/elasticity/compute_node_manager.py",
    "source/scripts/testing/phases.json",
    "source/scripts/testing/rq3snd_p0_02_analyzer_selftest.py",
    "source/scripts/testing/rq3snd_p0_03_preflight.sh",
    "source/scripts/testing/rq3snd_p1_01_launch_run.sh",
    "source/scripts/testing/analysis/rq3/readiness_robustness.py",
)
ARM_ENVS = (
    "source/scripts/testing/controller_env_overrides/rq3snd_premature_lead2.env",
    "source/scripts/testing/controller_env_overrides/rq3snd_premature_lead5.env",
    "source/scripts/testing/controller_env_overrides/rq3snd_premature_lead10.env",
    "source/scripts/testing/controller_env_overrides/rq3snd_semantic.env",
    "source/scripts/testing/controller_env_overrides/rq3snd_wake_verify.env",
    "source/scripts/testing/controller_env_overrides/rq3rob_event_only.env",
    "source/scripts/testing/controller_env_overrides/rq3rob_loss_all.env",
    "source/scripts/testing/controller_env_overrides/rq3sat_direct.env",
    "source/scripts/testing/controller_env_overrides/rq3sat_discovery.env",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--no-git-checks",
        action="store_true",
        help="skip the HEAD==tag and git-status checks (debugging only)",
    )
    args = parser.parse_args()
    worktree = args.worktree.resolve()
    if not worktree.is_dir():
        raise SystemExit(f"ERROR: worktree does not exist: {worktree}")
    commit = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", f"{args.tag}^{{commit}}"], text=True
    ).strip()
    if not args.no_git_checks:
        head = subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True
        ).strip()
        if head != commit:
            raise SystemExit(
                f"ERROR: worktree HEAD {head} does not match {args.tag}^{{commit}} "
                f"{commit} (plan §5 P0(2); --no-git-checks only for debugging)"
            )
        porcelain = subprocess.check_output(
            ["git", "-C", str(worktree), "status", "--porcelain"], text=True
        ).splitlines()
        changes = [line for line in porcelain if line.strip()]
        expected = "source/scripts/testing/phases.json"
        if len(changes) != 1:
            raise SystemExit(
                "ERROR: git status --porcelain must show exactly one change "
                f"({expected}); saw {len(changes)}: {changes}"
            )
        status_code = changes[0][:2]
        changed_path = changes[0][3:].strip().strip('"')
        if "M" not in status_code or changed_path != expected:
            raise SystemExit(
                "ERROR: git status must show exactly one modification of "
                f"{expected}; saw: {changes[0]!r}"
            )
    protected = {}
    missing = []
    for relative in PROTECTED:
        path = worktree / relative
        paths = [path] if path.is_file() else sorted(path.rglob("*"))
        files = [child for child in paths if child.is_file()]
        if not files:
            missing.append(relative)
            continue
        for child in files:
            protected[str(child.relative_to(worktree)).replace("\\", "/")] = sha256(child)
    arm_env_sha256 = {}
    for relative in ARM_ENVS:
        path = worktree / relative
        if path.is_file():
            arm_env_sha256[relative] = sha256(path)
        else:
            missing.append(relative)
    manifest = {
        "worktree_root": str(worktree),
        "tag": args.tag,
        "tag_commit": commit,
        "frozen_base_commit": "852a8a3",
        "protected_sha256": protected,
        "arm_env_sha256": arm_env_sha256,
        "missing": missing,
    }
    manifest_path = (args.manifest or worktree / "rq3snd_protected_surface.json").resolve()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    if missing:
        print(f"WARN: {len(missing)} protected path(s) missing "
              "(recorded in the manifest `missing` list):", file=sys.stderr)
        for relative in missing:
            print(f"WARN:   {relative}", file=sys.stderr)
    print(json.dumps({"tag": args.tag, "commit": commit, "manifest": str(manifest_path)},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
