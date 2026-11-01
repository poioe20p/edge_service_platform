#!/usr/bin/env python3
"""Prepare and attest the isolated RQ3 timing worktree/tag.

The frozen base is the final robustness attestation commit 852a8a3; the timing
family adds NO runtime source changes (fault knobs and arm env deltas are
reused byte-identical from the robustness family). The swept axis is the
compute_plateau RATE in the canonical phases.json (2.0/2.5/3.0). This script
hashes the same protected runtime surface as the robustness/qoe prepare_tag so
the attestation is directly comparable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

PROTECTED = (
    "source/sdn_controller/readiness_gate.py",
    "source/sdn_controller/control_events.py",
    "source/sdn_controller/scaling_config.py",
    "source/docker/edge_server",
    "source/docker/local_state_server",
    "source/scripts/network",
    "source/scripts/Makefile",
    "source/scripts/testing/run_experiment.sh",
    "source/scripts/testing/fault_injector.py",
)
ARM_ENVS = (
    "source/scripts/testing/controller_env_overrides/rq3sat_direct.env",
    "source/scripts/testing/controller_env_overrides/rq3sat_discovery.env",
    "source/scripts/testing/controller_env_overrides/rq3rob_event_only.env",
    "source/scripts/testing/controller_env_overrides/rq3rob_loss_all.env",
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
    args = parser.parse_args()
    worktree = args.worktree.resolve()
    if not worktree.is_dir():
        raise SystemExit(f"ERROR: worktree does not exist: {worktree}")
    commit = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", f"refs/tags/{args.tag}"], text=True
    ).strip()
    protected = {}
    for relative in PROTECTED:
        path = worktree / relative
        paths = [path] if path.is_file() else sorted(path.rglob("*"))
        for child in paths:
            if child.is_file():
                protected[str(child.relative_to(worktree)).replace("\\", "/")] = sha256(child)
    manifest = {
        "worktree_root": str(worktree),
        "tag": args.tag,
        "tag_commit": commit,
        "frozen_base_commit": "852a8a3",
        "protected_sha256": protected,
        "arm_env_sha256": {
            relative: sha256(worktree / relative)
            for relative in ARM_ENVS if (worktree / relative).exists()
        },
    }
    manifest_path = (args.manifest or worktree / ".rq3tim_frozen_manifest.json").resolve()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    print(json.dumps({"tag": args.tag, "commit": commit, "manifest": str(manifest_path)},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
