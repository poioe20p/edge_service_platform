#!/usr/bin/env python3
"""RQ3 admission-modes P0 — platform-identity gate (plan §6, §12.1).

Captures, on the campaign host (`cloud-vm-rq3`), the deployed platform
identity and admission-path surface, then compares them against the recorded
reference exactly per plan §6:

- the per-group EXECUTION trees — the trees each group's runs launch from:
  timing group `--timing-tree` (default `~/rq3tim_frozen` @ `852a8a3`, tag
  `rq3tim-preflight-20260908`), soundness group `--soundness-tree` (default
  `~/rq3snd_frozen2` @ `541fb22`, tag `rq3snd-preflight-20261003`), and the
  supporting/C3 group `--support-tree` (default this campaign tree — the C3
  launcher derives its REPO_ROOT from its own location; validated against the
  C3 record: the v2 env files `rq3_direct.env` / `rq3_discovery.env` /
  `rq3_discovery_15.env` and the per-set archived profile hashes the launcher
  asserts; the declared drop knob is not part of this group's surface) — and
  `edge_server:latest` image IDs (recorded `207b1f9e8f64` for the soundness
  era);
- the operator tree (`--deployed-root`, default this repo): reported
  informationally only — it is not an execution surface (2026-10-11 (e)) and
  never causes DRIFT in the verdict;
- the admission-path surface: `readiness_gate.py`, the
  `compute_node_manager.py` admission functions, `edge_server/source/app.py`
  readiness/bind ordering, `control_events.py`, the arm/fault env files and
  the `phases.json` profile — each group's execution tree against its own
  recorded manifest, with the operator tree compared informationally;
- the declared default-off knob rule (app.py: `EDGE_READY_PREMATURE_S`,
  `EDGE_READY_SEMANTIC_LIE_S`; readiness_gate.py:
  `READINESS_WAKE_VERIFY_MODE`): the hash may be taken over the off-path
  surface (declared knob lines excluded), or the recorded off-path
  byte-identity proof (soundness manifest hash + the recorded
  `make_server`-bind-before-emission ordering facts) substitutes on that file;
  a difference outside a declared knob block, or an unverifiable knob-off
  claim, fails the comparison.

Verdict (every required comparison item must be defined on both sides and
equal; a required item missing from the record is UNDEFINED and blocks):
Verdict, stage-scoped (2026-10-11 (g)): the report carries `c1_c2_gate`
(timing + soundness groups: the matrix) and `c3_gate` (supporting/campaign
group: the five supporting runs); the overall `verdict` is their conjunction.
A `c3_gate` block stops only the supporting batch (C3) and never the matrix;
a `c1_c2_gate` block stops C1/C2. Every required comparison item must be
defined on both sides and equal; a required item missing from the record is
UNDEFINED and blocks its own stage.
  MATCH -> proceed; DRIFT or blocking UNDEFINED -> do not extend; the §6.4
corrective full re-run matrix applies on a c1_c2 stage DRIFT. The timing-image
item is the one recorded waiver: with no timing-era ID in the record it is
reported WAIVED (non-blocking, provenance-carrying; plan §6) — its search is
scoped to the timing execution tree, and operator-tree candidates are
informational only. Items with no recorded reference are reported as
NOT_RECORDED and the verdict output prints a summary line counting them
(`NOT_RECORDED (informational): N items`), so a reader can see what was not
checked; all other required items keep the UNDEFINED-blocks rule.

Exit codes (plan §12.1): 0 = MATCH; 3 = DRIFT, blocking UNDEFINED, or a
capture/compare error (STOP — never a silent pass); argparse usage errors exit
2. Output: `analysis/platform_identity.json` under the plan folder (plus the
raw capture alongside it).

Usage:
  python3 rq3adm_p0_01_platform_identity.py                 # capture + compare
  python3 rq3adm_p0_01_platform_identity.py --no-docker     # offline capture
  python3 rq3adm_p0_01_platform_identity.py --compare-only capture.json
  python3 rq3adm_p0_01_platform_identity.py --selftest      # fixture checks
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PLAN_RELDIR = Path("docs/operation/testing/experiment/v3/rq3_admission_modes")
DEFAULT_OUT = REPO_ROOT / PLAN_RELDIR / "analysis/platform_identity.json"
DEFAULT_CAPTURE = REPO_ROOT / PLAN_RELDIR / "analysis/platform_identity_capture.json"

# Recorded reference (plan §6.1-§6.3). Tag dates come from the family
# preflight logs; the timing tag is accepted as any `rq3tim-preflight-*` tag
# resolving to the recorded commit.
TIMING_TREE_NAME = "rq3tim_frozen"
TIMING_COMMIT = "852a8a3"
# The plan leaves the timing tag date as `<date>`; the recorded name is
# rq3tim-preflight-20260908 (timing preflight log). Any
# `rq3tim-preflight-*` tag resolving to the recorded commit satisfies the
# item; the preferred name is reported for provenance only.
TIMING_TAG_PREFERRED = "rq3tim-preflight-20260908"
TIMING_TAG_GLOB = "rq3tim-preflight-*"
TIMING_MANIFEST_NAME = ".rq3tim_frozen_manifest.json"

SOUNDNESS_TREE_NAME = "rq3snd_frozen2"
SOUNDNESS_COMMIT = "541fb22"
SOUNDNESS_TAG = "rq3snd-preflight-20261003"
SOUNDNESS_MANIFEST_NAME = "rq3snd_protected_surface.json"
SOUNDNESS_IMAGE_ID = "207b1f9e8f64"

# Supporting/C3 group (plan §5.2, 2026-10-11 (f)): the five supporting runs
# launch from the operator/campaign tree — the C3 launcher
# (`rq3adm_p1_01_launch_run.sh`) derives REPO_ROOT from its own location — so
# that tree is this group's execution surface. Recorded reference:
# - the v2 env files (CR-stripped sha256: line endings are not content; the VM
#   audit proved CRLF variance across tree copies), recorded from the campaign
#   copies in this repo;
# - the per-set archived profile hashes the launcher asserts when it restores
#   each set's snapshot (first available run folder of the set, launcher glob
#   order). camp and disc15 restore from the v2 primary-set / sensitivity
#   snapshots recorded in this repo (702bc0…); cell12 restores from the
#   rate-12 rendering provisioned 2026-10-12 (plan §12.7; raw sha256
#   37bc54…). No set lacks a recorded reference; the NOT_RECORDED class
#   remains in the compare path and reports 0 items.
# The declared default-off drop knob is NOT part of this group's surface (the
# operator tree legitimately lacks it; the fault mechanism lives in the frozen
# execution trees).
SUPPORT_TREE_NAME = "efficient-storage-in-edge-scenarios"
SUPPORT_V2_ENV_CR_SHA256 = {
    "rq3_direct.env":
        "79a5ce3a50b1baa986e31ac79e560ccfd937ff142a8742045bed118bacd950d1",
    "rq3_discovery.env":
        "2110b499814014ad00526c7fedce5e7ab56a4644101e07b413d59fad3a8206af",
    "rq3_discovery_15.env":
        "7e5a3b4177b5f9efe0a01c9f46803a480c71de3bee044f78a97c535d3594ce74",
}
SUPPORT_SET_SPECS = {
    "camp": {
        "rate": 3.0, "window": 1024,
        "snapshot_globs": ("*_rq3_direct_*", "*_rq3_disc_*"),
        "recorded_snapshot_sha256":
            "702bc05196ae74c4e8ca4efa3ca150dcb3197391e55e1e17eb8a34580fbc4e8d",
    },
    "cell12": {
        "rate": 12.0, "window": 4096,
        "snapshot_globs": ("*_rq3_probe_cell12_direct_*",
                           "*_rq3_probe_cell12_disc_*"),
        "recorded_snapshot_sha256":
            # provisioned 2026-10-12 per plan §12.7; rate-12 instantiation
            # of the same template
            "37bc546de326c6ea2e81597db616544e74dae4aa0efe3ec3f68ce096e6b771fa",
    },
    "disc15": {
        "rate": 3.0, "window": 1024,
        "snapshot_globs": ("*_rq3_disc15_*",),
        "recorded_snapshot_sha256":
            "702bc05196ae74c4e8ca4efa3ca150dcb3197391e55e1e17eb8a34580fbc4e8d",
    },
}

# Declared default-off knob blocks (the contiguous declared-off regions; a
# difference confined to these regions is off-path). Each block is an
# inclusive line range from the start anchor to the first following line
# containing the end anchor; a missing anchor makes the block unverifiable
# (fail closed — never a substring-wide strip).
KNOB_BLOCKS = {
    "source/docker/edge_server/source/app.py": (
        ("RQ3 soundness knob: premature readiness claim", "time.sleep(_premature_s)"),
        ("RQ3 soundness knob: semantic lie window (default off)",
         "_lie_s, _RQ3_SEMANTIC_LIE_UNTIL,"),
    ),
    "source/sdn_controller/readiness_gate.py": (
        ("READINESS_WAKE_VERIFY_MODE", "READINESS_WAKE_VERIFY_MODE"),
    ),
}
KNOB_FILES = {key: tuple(start for start, _ in blocks)
              for key, blocks in KNOB_BLOCKS.items()}

# Recorded off-path byte-identity proof (soundness plan §5 P0(4), preflight
# log 1.2/1.3): the manifest hash of the rebuilt soundness tree plus the live
# make_server-bind-before-emission ordering facts recorded on the
# 207b1f9e8f64 image. The substitution condition verifies this artifact
# exists (never a hardcoded True).
OFF_PATH_PROOF = {
    "artifact": "docs/operation/testing/experiment/v3/rq3_soundness/preflight_log.md",
    "required_patterns": (
        r"make_server\(bind\+listen\)=0\.000 s",
        SOUNDNESS_IMAGE_ID,
    ),
    "files": ("source/docker/edge_server/source/app.py",
              "source/sdn_controller/readiness_gate.py"),
    "ordering_facts": (
        "make_server(bind+listen) logged before 'edge-server listening'",
        "'edge-server listening' logged before the readiness emission",
    ),
}

# Admission-path surface (plan §6.3). Paths are tree-relative.
SURFACE_FILES = (
    "source/sdn_controller/readiness_gate.py",
    "source/sdn_controller/control_events.py",
    "source/sdn_controller/scaling_config.py",
    "source/sdn_controller/main_n1.py",
    "source/sdn_controller/main_n2.py",
    "source/sdn_controller/elasticity/compute_node_manager.py",
    "source/docker/edge_server/source/app.py",
    "source/docker/edge_server/source/edge_server_process_state.py",
    "source/scripts/testing/phases.json",
    "source/scripts/testing/controller_env_overrides/rq3sat_direct.env",
    "source/scripts/testing/controller_env_overrides/rq3sat_discovery.env",
    "source/scripts/testing/controller_env_overrides/rq3rob_event_only.env",
    "source/scripts/testing/controller_env_overrides/rq3rob_loss_all.env",
    "source/scripts/testing/controller_env_overrides/rq3snd_premature_lead10.env",
    "source/scripts/testing/controller_env_overrides/rq3_direct.env",
    "source/scripts/testing/controller_env_overrides/rq3_discovery.env",
    "source/scripts/testing/controller_env_overrides/rq3_discovery_15.env",
)
ADMISSION_FUNCTION_NAMES = ("add_edge_server", "_docker_run_server")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cr_stripped_sha256(path: Path) -> str | None:
    """sha256 over the CR-stripped bytes (line endings are not content)."""
    if not path.is_file():
        return None
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def strip_knob_blocks(text: str, blocks) -> str | None:
    """Remove only the declared knob blocks (inclusive start-anchor to
    end-anchor line ranges); None when a declared anchor is missing (the
    knob-off claim is then unverifiable, never silently accepted)."""
    lines = text.splitlines()
    remove: set[int] = set()
    for start_anchor, end_anchor in blocks:
        start = next((index for index, line in enumerate(lines)
                      if start_anchor in line), None)
        if start is None:
            return None
        end = next((index for index in range(start, len(lines))
                    if end_anchor in lines[index]), None)
        if end is None:
            return None
        remove.update(range(start, end + 1))
    return "\n".join(line for index, line in enumerate(lines)
                     if index not in remove) + "\n"


def verify_off_path_proof(root: Path) -> tuple[bool, str]:
    """Verify the recorded off-path byte-identity proof artifact exists with
    its recorded facts (soundness preflight log 1.2/1.3)."""
    path = root / OFF_PATH_PROOF["artifact"]
    if not path.is_file():
        return False, f"recorded proof artifact missing: {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    for pattern in OFF_PATH_PROOF["required_patterns"]:
        if re.search(pattern, text) is None:
            return False, f"recorded proof artifact lacks pattern: {pattern}"
    return True, "soundness preflight log 1.2/1.3 facts present"


def admission_functions_text(text: str) -> str:
    """Concatenated source of the declared admission functions."""
    lines = text.splitlines()
    blocks: list[str] = []
    current: list[str] | None = None
    current_indent = 0
    for line in lines:
        match = re.match(r"^(\s*)def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if match:
            indent = len(match.group(1))
            if match.group(2) in ADMISSION_FUNCTION_NAMES:
                current = [line]
                current_indent = indent
                continue
            if current is not None and indent <= current_indent:
                blocks.append("\n".join(current))
                current = None
            continue
        if current is not None:
            if line.strip() and len(line) - len(line.lstrip()) <= current_indent \
                    and not line.startswith(" " * (current_indent + 1)):
                blocks.append("\n".join(current))
                current = None
                continue
            current.append(line)
    if current is not None:
        blocks.append("\n".join(current))
    return "\n\n".join(blocks) + "\n"


def tree_file_record(root: Path, relative: str) -> dict:
    path = root / relative
    record: dict = {"present": path.is_file(), "sha256": None,
                    "stripped_sha256": None, "knob_block_error": None,
                    "admission_functions_sha256": None}
    if not path.is_file():
        return record
    data = path.read_bytes()
    record["sha256"] = sha256_bytes(data)
    text = data.decode("utf-8", errors="replace")
    if relative in KNOB_BLOCKS:
        stripped = strip_knob_blocks(text, KNOB_BLOCKS[relative])
        if stripped is None:
            record["knob_block_error"] = "declared knob-block anchor missing"
        else:
            record["stripped_sha256"] = sha256_bytes(stripped.encode("utf-8"))
    if relative.endswith("compute_node_manager.py"):
        record["admission_functions_sha256"] = sha256_bytes(
            admission_functions_text(text).encode("utf-8"))
    return record


def capture_tree_files(root: Path) -> dict:
    return {relative: tree_file_record(root, relative) for relative in SURFACE_FILES}


def git_output(root: Path, *args: str) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            check=False)
    except OSError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    return result.stdout.strip(), None


def resolve_tag(root: Path, exact: str, glob_pattern: str,
                target_commit: str) -> tuple[str | None, str | None]:
    """Recorded tag resolution: exact name first, then any matching tag that
    resolves to the recorded commit."""
    commit, error = git_output(root, "rev-parse", f"refs/tags/{exact}^{{commit}}")
    if commit is not None and commit.startswith(target_commit):
        return exact, commit
    listing, list_error = git_output(root, "tag", "--list", glob_pattern)
    if listing:
        for name in sorted(listing.splitlines()):
            candidate, _ = git_output(root, "rev-parse", f"refs/tags/{name}^{{commit}}")
            if candidate is not None and candidate.startswith(target_commit):
                return name, candidate
    if commit is not None and not commit.startswith(target_commit):
        return exact, commit  # tag exists but points elsewhere -> DRIFT evidence
    return None, error or list_error


def capture_tree(root: Path, name: str, commit: str, tag: str,
                 tag_glob: str, manifest_name: str,
                 tree_default: Path) -> dict:
    exists = root.is_dir()
    record: dict = {
        "name": name, "root": str(root), "present": exists,
        "default_root": str(tree_default),
        "head": None, "head_error": None,
        "tag": None, "tag_commit": None, "tag_error": None,
        "manifest_path": None, "manifest": None, "manifest_error": None,
        "manifest_sha256": None,
        "files": capture_tree_files(root) if exists else {},
    }
    if not exists:
        return record
    record["head"], record["head_error"] = git_output(root, "rev-parse", "HEAD")
    record["tag"], record["tag_commit"] = resolve_tag(root, tag, tag_glob, commit)
    if record["tag"] is None:
        record["tag_error"] = "no matching tag resolves to the recorded commit"
    manifest_path = root / manifest_name
    if manifest_path.is_file():
        record["manifest_path"] = str(manifest_path)
        record["manifest_sha256"] = sha256_file(manifest_path)
        try:
            record["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            record["manifest_error"] = str(exc)
    else:
        record["manifest_error"] = f"missing {manifest_name}"
    return record


def docker_image_id(image: str = "edge_server:latest") -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, check=False)
    except OSError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    value = result.stdout.strip().replace("sha256:", "")
    return (value[:12] if value else None), None


IMAGE_ID_RE = re.compile(r"\b([0-9a-f]{12,64})\b")


def _search_image_ids_in_text(text: str) -> list[str]:
    """12-hex docker image IDs on recorded lines mentioning an image."""
    ids: list[str] = []
    for line in text.splitlines():
        if "image" not in line.lower():
            continue
        for value in IMAGE_ID_RE.findall(line):
            if len(value) >= 12 and value[:12] not in ids:
                ids.append(value[:12])
    return ids


def _search_image_ids_in_json(node, out: list[tuple[str, str]], path: str = "") -> None:
    """Image/digest-keyed values of a recorded JSON state file."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_path = f"{path}.{key}" if path else key
            if isinstance(value, str) and ("image" in key.lower()
                                           or "digest" in key.lower()):
                for match in IMAGE_ID_RE.findall(value):
                    out.append((key_path, match[:12]))
            else:
                _search_image_ids_in_json(value, out, key_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _search_image_ids_in_json(item, out, f"{path}[{index}]")


def capture_identity(deployed_root: Path, timing_root: Path, soundness_root: Path,
                     no_docker: bool, support_root: Path | None = None) -> dict:
    deployment: dict = {
        "root": str(deployed_root),
        "role": ("operator tree — not an execution surface (informational "
                 "only; the execution surfaces are the per-group frozen "
                 "worktrees)"),
        "head": None, "head_error": None,
        "docker_image": None, "docker_error": None,
        "files": capture_tree_files(deployed_root) if deployed_root.is_dir() else {},
    }
    if deployed_root.is_dir():
        deployment["head"], deployment["head_error"] = git_output(
            deployed_root, "rev-parse", "HEAD")
    if no_docker:
        deployment["docker_error"] = "docker capture disabled (--no-docker)"
    else:
        deployment["docker_image"], deployment["docker_error"] = docker_image_id()
    timing = capture_tree(timing_root, TIMING_TREE_NAME, TIMING_COMMIT,
                          TIMING_TAG_PREFERRED, TIMING_TAG_GLOB,
                          TIMING_MANIFEST_NAME,
                          Path.home() / TIMING_TREE_NAME)
    timing["image_search"] = search_timing_image_record(timing_root)
    timing["operator_image_search"] = search_operator_image_record(deployed_root)
    if not timing["present"] and deployed_root.is_dir():
        # The tag record is read from the operator repo (worktrees share the
        # tag namespace); the absent timing execution tree still blocks as
        # UNDEFINED (2026-10-11 (e) — no operator-tree substitution).
        tag_name, tag_commit = resolve_tag(deployed_root, TIMING_TAG_PREFERRED,
                                           TIMING_TAG_GLOB, TIMING_COMMIT)
        timing["tag_from_deployed"] = {"tag": tag_name, "tag_commit": tag_commit}
    proof_ok, proof_detail = verify_off_path_proof(deployed_root)
    return {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "deployed": deployment,
        "timing": timing,
        "soundness": capture_tree(soundness_root, SOUNDNESS_TREE_NAME,
                                  SOUNDNESS_COMMIT, SOUNDNESS_TAG,
                                  "rq3snd-preflight-*", SOUNDNESS_MANIFEST_NAME,
                                  Path.home() / SOUNDNESS_TREE_NAME),
        "support": capture_support_surface(
            support_root if support_root is not None else REPO_ROOT),
        "off_path_proof": {"verified": proof_ok, "detail": proof_detail,
                           "artifact": OFF_PATH_PROOF["artifact"]},
    }


def _item(items: list, name: str, group: str, status: str, required: bool,
          deployed, reference, note: str = "") -> None:
    items.append({
        "item": name, "group": group, "status": status, "required": required,
        "deployed": deployed, "reference": reference, "note": note,
    })


def _scalar_item(items: list, name: str, deployed, reference, required: bool,
                 note: str = "") -> None:
    if deployed is None:
        _item(items, name, "identity", "UNDEFINED", required, deployed,
              reference, note or "not captured on the deployed side")
        return
    status = "MATCH" if str(deployed).startswith(str(reference)) else "DRIFT"
    _item(items, name, "identity", status, required, deployed, reference, note)


def manifest_hashes(manifest: dict | None) -> dict:
    hashes: dict = {}
    if not isinstance(manifest, dict):
        return hashes
    for section in ("protected_sha256", "arm_env_sha256"):
        for key, value in (manifest.get(section) or {}).items():
            hashes[key] = value
    return hashes


def compare_surface(items: list, group: str, tree_capture: dict,
                    manifest: dict | None, soundness_capture: dict,
                    capture: dict, substitute_note: str = "",
                    informational: bool = False) -> None:
    """Compare one tree's surface against a recorded manifest.

    ``informational=True`` marks the operator-tree comparison: every item it
    emits is labelled "operator tree — not an execution surface" and is
    non-blocking (``required=False``) so it can never drive DRIFT (2026-10-11
    (e)); the per-group execution-tree comparisons stay blocking.
    """
    first_item = len(items)

    def mark_informational() -> None:
        if not informational:
            return
        for item in items[first_item:]:
            item["required"] = False
            item["informational"] = True
            item["note"] = ("operator tree — not an execution surface "
                            "(informational; never gates): "
                            + (item["note"] or ""))
    if manifest is None:
        _item(items, f"{group}:manifest", group, "UNDEFINED", True,
              tree_capture.get("manifest_error"), "recorded manifest",
              "recorded reference manifest missing -> comparison not definable")
        mark_informational()
        return
    if not isinstance(manifest, dict) or not any(
            isinstance(manifest.get(section), dict)
            for section in ("protected_sha256", "arm_env_sha256")):
        _item(items, f"{group}:manifest-shape", group, "UNDEFINED", True,
              None, "protected_sha256/arm_env_sha256 sections",
              "recorded manifest has no recognized hash sections "
              "(a differently-shaped manifest must never yield a silent MATCH)")
        mark_informational()
        return
    recorded = manifest_hashes(manifest)
    soundness_hashes = manifest_hashes(soundness_capture.get("manifest"))
    proof_ok = bool((capture.get("off_path_proof") or {}).get("verified"))
    compared = 0
    for relative in SURFACE_FILES:
        recorded_hash = recorded.get(relative)
        if recorded_hash is None:
            _item(items, f"{group}:{relative}", group, "NOT_RECORDED", False,
                  None, None, "path not part of this recorded manifest")
            continue
        file_record = (tree_capture.get("files") or {}).get(relative) or {}
        deployed_hash = file_record.get("sha256")
        if deployed_hash is None:
            _item(items, f"{group}:{relative}", group, "UNDEFINED", True,
                  None, recorded_hash, "path missing in the compared tree")
            continue
        compared += 1
        item_note = substitute_note
        if deployed_hash == recorded_hash:
            _item(items, f"{group}:{relative}", group, "MATCH", True,
                  deployed_hash, recorded_hash, item_note)
            continue
        # Declared default-off knob block rule: knob-block-excluded hash, or
        # (on the proof files) the verified recorded off-path byte-identity
        # proof. A difference outside a declared block, or an unverifiable
        # knob-off claim that the proof does not cover, fails.
        if relative in KNOB_BLOCKS and not file_record.get("knob_block_error"):
            stripped = file_record.get("stripped_sha256")
            reference_stripped = ((soundness_capture.get("files") or {})
                                  .get(relative, {}).get("stripped_sha256"))
            if stripped is not None and reference_stripped is not None \
                    and stripped == reference_stripped \
                    and soundness_hashes.get(relative) == recorded_hash:
                _item(items, f"{group}:{relative}", group, "MATCH", True,
                      stripped, reference_stripped,
                      "difference confined to the declared knob block "
                      "(knob-block-excluded hash; off-path)")
                continue
        if relative in OFF_PATH_PROOF["files"] and proof_ok \
                and soundness_hashes.get(relative) == deployed_hash:
            _item(items, f"{group}:{relative}", group, "MATCH", True,
                  deployed_hash, soundness_hashes.get(relative),
                  "off-path byte-identity proof verified "
                  f"({OFF_PATH_PROOF['artifact']}: manifest hash + recorded "
                  "make_server-bind-before-emission ordering facts)")
            continue
        if relative in KNOB_BLOCKS:
            note = ("declared knob-block anchor missing — the knob-off claim "
                    "is unverifiable and the recorded off-path proof does not "
                    "cover the difference")
            if not proof_ok:
                note = ("knob-carrying file differs and the recorded off-path "
                        "proof is not verified — substitution denied")
            _item(items, f"{group}:{relative}", group, "DRIFT", True,
                  deployed_hash, recorded_hash, note)
            continue
        _item(items, f"{group}:{relative}", group, "DRIFT", True,
              deployed_hash, recorded_hash,
              "admission-path difference outside any declared knob block")
    if compared == 0:
        _item(items, f"{group}:coverage", group, "UNDEFINED", True, compared,
              ">=1 recorded surface hash actually compared",
              "recorded manifest present but no surface hash was compared "
              "— fail closed")
    # Supplementary: the admission functions of compute_node_manager.py.
    node_path = "source/sdn_controller/elasticity/compute_node_manager.py"
    deployed_funcs = ((tree_capture.get("files") or {}).get(node_path) or {}) \
        .get("admission_functions_sha256")
    reference_funcs = ((soundness_capture.get("files") or {}).get(node_path) or {}) \
        .get("admission_functions_sha256")
    if deployed_funcs is not None and reference_funcs is not None:
        status = "MATCH" if deployed_funcs == reference_funcs else "DRIFT"
        _item(items, f"{group}:{node_path}#admission-functions",
              group, status, False, deployed_funcs, reference_funcs,
              "supplementary admission-function region hash (add_edge_server, "
              "_docker_run_server) vs the soundness tree; the whole-file hash "
              "above is the gating item")
        items[-1]["supplementary"] = True
    mark_informational()


def search_timing_image_record(timing_root: Path) -> dict:
    """Search the recorded timing-era artifacts for an image ID/digest, scoped
    to the timing EXECUTION tree (2026-10-11 (f)): only evidence from the tree
    the timing group launches from may resolve the timing-image item — the
    operator tree is not an identity surface. Deterministic and bounded; the
    provenance of every candidate is recorded."""
    candidates: list[dict] = []
    if timing_root.is_dir():
        for name in ("rq3lh_image_state.json", "rq3rob_image_state.json"):
            path = timing_root / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            found: list[tuple[str, str]] = []
            _search_image_ids_in_json(data, found)
            for key_path, value in found:
                candidates.append({"source": f"timing_tree:{name}:{key_path}",
                                   "image_id": value})
        docs_dir = timing_root / "docs/operation/testing/experiment/v3/rq3_timing"
        if docs_dir.is_dir():
            for path in sorted(docs_dir.glob("*.md")):
                for value in _search_image_ids_in_text(
                        path.read_text(encoding="utf-8", errors="replace")):
                    candidates.append({"source": f"docs:rq3_timing/{path.name}",
                                       "image_id": value})
        metrics_dir = timing_root / "source/scripts/testing/metrics"
        if metrics_dir.is_dir():
            for folder in sorted(metrics_dir.glob("*_rq3tim_*"))[:50]:
                for name in ("controller_env_snapshot.env", "quota_snapshot.json",
                             "run_status.json", "rq3snd_fault_attestation.txt"):
                    path = folder / name
                    if path.is_file():
                        for value in _search_image_ids_in_text(
                                path.read_text(encoding="utf-8", errors="replace")):
                            candidates.append(
                                {"source": f"run:{folder.name}/{name}",
                                 "image_id": value})
    resolved = candidates[0]["image_id"] if candidates else None
    return {"resolved": resolved, "candidates": candidates[:20],
            "scope": "timing execution tree"}


def search_operator_image_record(deployed_root: Path) -> dict:
    """Informational-only operator-tree search for timing-era image evidence:
    recorded for provenance, never used to upgrade the timing-image item
    (plan §6 (e)/(f) — the operator tree is not an identity surface)."""
    candidates: list[dict] = []
    if deployed_root.is_dir():
        for name in ("rq3lh_image_state.json", "rq3rob_image_state.json"):
            path = deployed_root / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            found: list[tuple[str, str]] = []
            _search_image_ids_in_json(data, found)
            for key_path, value in found:
                candidates.append({"source": f"operator_tree:{name}:{key_path}",
                                   "image_id": value})
        docs_dir = deployed_root / "docs/operation/testing/experiment/v3/rq3_timing"
        if docs_dir.is_dir():
            for path in sorted(docs_dir.glob("*.md")):
                for value in _search_image_ids_in_text(
                        path.read_text(encoding="utf-8", errors="replace")):
                    candidates.append({"source": f"docs:rq3_timing/{path.name}",
                                       "image_id": value})
    resolved = candidates[0]["image_id"] if candidates else None
    return {"resolved": resolved, "candidates": candidates[:20],
            "scope": "operator tree (informational only)"}


def family_profile_status(path: Path) -> dict:
    """The family-profile content assertion (plan §5.2)."""
    try:
        phases = json.loads(path.read_text(encoding="utf-8")).get("phases") or []
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "detail": f"unreadable: {exc}"}
    plateau = next((phase for phase in phases
                    if phase.get("name") == "compute_plateau"), None)
    if plateau is None:
        return {"ok": False, "detail": "missing compute_plateau"}
    if float(plateau.get("duration_s") or 0) != 600.0:
        return {"ok": False, "detail": "compute_plateau duration != 600 s"}
    if float(plateau.get("rate_per_client") or 0) != 2.0:
        return {"ok": False, "detail": "compute_plateau rate != 2.0"}
    if float((plateau.get("mix") or {}).get("service_pressure") or 0) != 1.0:
        return {"ok": False,
                "detail": "compute_plateau mix is not pure service_pressure"}
    return {"ok": True,
            "detail": "family profile (600 s @2.0, pure service_pressure)"}


def support_set_profile_status(snapshot: Path, rate: float) -> dict:
    """The supporting launcher's per-set profile assertion (plan §5.2,
    mirrored from rq3adm_p1_01_launch_run.sh assert_set_profile)."""
    try:
        phases = json.loads(snapshot.read_text(encoding="utf-8")).get("phases") or []
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "detail": f"unreadable: {exc}"}
    by_name: dict = {}
    for phase in phases:
        by_name.setdefault(phase.get("name"), phase)
    baseline = by_name.get("baseline") or {}
    if float(baseline.get("duration_s") or 0) != 60.0 \
            or float(baseline.get("rate_per_client") or 0) != 1.0:
        return {"ok": False, "detail": "baseline is not 60 s @1.0"}
    spike = by_name.get("compute_spike") or {}
    if float(spike.get("duration_s") or 0) != 180.0:
        return {"ok": False, "detail": "compute_spike duration != 180 s"}
    if float(spike.get("rate_per_client") or 0) != float(rate):
        return {"ok": False, "detail": f"compute_spike rate != {rate}"}
    mix = spike.get("mix") or {}
    for key, expected in (("service_pressure", 0.9), ("feed_ranking", 0.05),
                          ("content_lookup", 0.05)):
        if abs(float(mix.get(key) or 0.0) - expected) > 1e-9:
            return {"ok": False, "detail": f"compute_spike mix {key} != {expected}"}
    gap = by_name.get("cleanup_gap") or {}
    if float(gap.get("duration_s") or 0) != 180.0 \
            or float(gap.get("rate_per_client") or 0) != 0.5:
        return {"ok": False, "detail": "cleanup_gap is not 180 s @0.5"}
    return {"ok": True, "detail": "profile semantics match the launcher assertion"}


def capture_support_surface(support_root: Path) -> dict:
    """Capture the C3/supporting-group execution surface: the v2 env files
    (CR-stripped hashes) and, per set, the first archived profile snapshot the
    supporting launcher would restore (launcher glob order, hash + profile)."""
    overrides = support_root / "source/scripts/testing/controller_env_overrides"
    envs = {}
    for name in SUPPORT_V2_ENV_CR_SHA256:
        path = overrides / name
        envs[name] = {
            "present": path.is_file(),
            "sha256": sha256_file(path),
            "cr_stripped_sha256": cr_stripped_sha256(path),
        }
    metrics = support_root / "source/scripts/testing/metrics"
    sets = {}
    for set_name, spec in SUPPORT_SET_SPECS.items():
        record: dict = {"snapshot": None, "snapshot_path": None, "sha256": None,
                        "profile": None,
                        "recorded_snapshot_sha256": spec["recorded_snapshot_sha256"]}
        if metrics.is_dir():
            # Exactly the supporting launcher's resolution (restore_snapshot):
            # per glob in order, the first matching folder (sort | head -1);
            # if that folder carries no phases_snapshot.json, continue to the
            # next glob — never scan further folders of the same glob.
            for pattern in spec["snapshot_globs"]:
                matches = sorted(metrics.glob(pattern))
                if not matches:
                    continue
                snapshot = matches[0] / "phases_snapshot.json"
                if not snapshot.is_file():
                    continue
                record["snapshot"] = matches[0].name
                record["snapshot_path"] = str(snapshot)
                record["sha256"] = sha256_file(snapshot)
                record["profile"] = support_set_profile_status(
                    snapshot, spec["rate"])
                break
        sets[set_name] = record
    phases = support_root / "source/scripts/testing/phases.json"
    return {
        "root": str(support_root),
        "name": support_root.name,
        "present": support_root.is_dir(),
        "head": (git_output(support_root, "rev-parse", "HEAD")[0]
                 if support_root.is_dir() else None),
        "envs": envs,
        "sets": sets,
        "phases_present": phases.is_file(),
        "phases_sha256": sha256_file(phases),
        "phases_family_profile": (family_profile_status(phases)
                                  if phases.is_file() else None),
    }


def compare_support_group(items: list, support: dict) -> None:
    """Compare the C3/supporting execution surface against its recorded
    reference (2026-10-11 (f)): v2 env files (CR-stripped hashes), the
    canonical family profile, and the per-set archived profile hashes the
    supporting launcher asserts. The drop knob is deliberately NOT part of
    this group's surface (the operator tree lacks it; the fault mechanism
    lives in the frozen execution trees)."""
    if not support.get("present"):
        _item(items, "support_tree", "support_surface", "UNDEFINED", True,
              support.get("root"), str(REPO_ROOT),
              "supporting/C3 execution tree absent — the five supporting runs "
              "launch from it, so its identity (v2 env files + per-set profile "
              "hashes) is required; UNDEFINED blocks")
        return
    _item(items, "support_tree", "support_surface", "RECORDED", True,
          support.get("root"), str(REPO_ROOT),
          "supporting/C3 execution tree — the five supporting runs launch from "
          "this tree (the C3 launcher derives REPO_ROOT from its own location); "
          "recorded reference = v2 env files + per-set archived profile hashes "
          "(plan §5.2)")
    _item(items, "support_head", "support_surface", "RECORDED", False,
          support.get("head"), None,
          "supporting/C3 execution-tree HEAD (recorded for provenance; the "
          "group's identity is its recorded env/profile surface, not a pinned "
          "commit)")
    for name, expected in SUPPORT_V2_ENV_CR_SHA256.items():
        record = (support.get("envs") or {}).get(name) or {}
        if not record.get("present"):
            _item(items, f"support_surface:{name}", "support_surface",
                  "UNDEFINED", True, None, expected,
                  "v2 supporting-set env file missing in the support tree")
            continue
        actual = record.get("cr_stripped_sha256")
        _item(items, f"support_surface:{name}", "support_surface",
              "MATCH" if actual == expected else "DRIFT", True,
              actual, expected,
              "v2 supporting-set env file vs the recorded canonical copy "
              "(CR-stripped compare — line endings are not content)")
    if not support.get("phases_present"):
        _item(items, "support_surface:phases.json", "support_surface",
              "UNDEFINED", True, None, "family profile",
              "canonical phases.json missing in the support tree")
    else:
        profile = support.get("phases_family_profile") or {}
        _item(items, "support_surface:phases.json", "support_surface",
              "MATCH" if profile.get("ok") else "DRIFT", True,
              support.get("phases_sha256"),
              "family profile (600 s @2.0, pure service_pressure)",
              "canonical phases.json in the support tree must be the family "
              f"profile (plan §5.2): {profile.get('detail')}")
    for set_name, spec in SUPPORT_SET_SPECS.items():
        record = (support.get("sets") or {}).get(set_name) or {}
        if record.get("snapshot") is None:
            _item(items, f"support_surface:set:{set_name}", "support_surface",
                  "UNDEFINED", True, None, list(spec["snapshot_globs"]),
                  "no archived phases_snapshot.json found for this set (the "
                  "supporting launcher resolves the first matching folder per "
                  "glob in order, <glob>/phases_snapshot.json, and would "
                  "STOP); UNDEFINED blocks the C3 stage")
            continue
        profile = record.get("profile") or {}
        _item(items, f"support_surface:set:{set_name}:profile",
              "support_surface", "MATCH" if profile.get("ok") else "DRIFT", True,
              record.get("snapshot"), f"profile semantics @ rate {spec['rate']}",
              f"archived set snapshot {record.get('snapshot')}: "
              f"{profile.get('detail')}")
        recorded_hash = record.get("recorded_snapshot_sha256")
        if recorded_hash is None:
            _item(items, f"support_surface:set:{set_name}:snapshot-hash",
                  "support_surface", "NOT_RECORDED", False,
                  record.get("sha256"), None,
                  "no recorded reference hash for this set's snapshot on the "
                  "workstation record (VM-only set): the snapshot hash is "
                  "recorded here but not compared; the profile semantics "
                  "above stay gating")
        else:
            _item(items, f"support_surface:set:{set_name}:snapshot-hash",
                  "support_surface",
                  "MATCH" if record.get("sha256") == recorded_hash else "DRIFT",
                  True, record.get("sha256"), recorded_hash,
                  "per-set archived profile hash the supporting launcher "
                  "asserts (recorded reference)")


def compare(capture: dict) -> dict:
    items: list[dict] = []
    deployed = capture.get("deployed") or {}
    timing = capture.get("timing") or {}
    soundness = capture.get("soundness") or {}

    # Identity items (plan §6.1/§6.2; per-group execution trees, 2026-10-11
    # (e)). The operator tree is reported informationally: it is not an
    # execution surface and its items never produce DRIFT in the verdict.
    _item(items, "operator_tree", "identity", "RECORDED", False,
          deployed.get("root"), None,
          "operator tree — not an execution surface (informational only; the "
          "execution surfaces are the per-group frozen worktrees)")
    if not timing.get("present"):
        _item(items, "timing_tree", "identity", "UNDEFINED", True,
              timing.get("root"), str(Path.home() / TIMING_TREE_NAME),
              "timing execution tree absent — a group's identity requires its "
              "execution tree, so this is UNDEFINED and blocks (the operator "
              "tree is not an execution surface and cannot substitute; "
              "2026-10-11 (e))")
        tag = timing.get("tag_from_deployed") or {}
        _scalar_item(items, "timing_tag",
                     tag.get("tag_commit"), TIMING_COMMIT, True,
                     f"recorded tag preference {TIMING_TAG_PREFERRED}; any "
                     f"{TIMING_TAG_GLOB} tag resolving to {TIMING_COMMIT} "
                     "satisfies the item (resolved from the operator repo; "
                     "worktrees share the tag namespace)")
    else:
        _scalar_item(items, f"timing_head@{TIMING_TREE_NAME}",
                     timing.get("head"), TIMING_COMMIT, True,
                     "timing execution-tree commit (the frozen worktree its "
                     "runs launch from)")
        _scalar_item(items, "timing_tag",
                     timing.get("tag_commit"), TIMING_COMMIT, True,
                     f"recorded tag preference {TIMING_TAG_PREFERRED}; resolved "
                     f"as {timing.get('tag')}; any {TIMING_TAG_GLOB} tag "
                     f"resolving to {TIMING_COMMIT} satisfies the item")
    if not soundness.get("present"):
        _item(items, "soundness_tree", "identity", "UNDEFINED", True,
              soundness.get("root"), str(Path.home() / SOUNDNESS_TREE_NAME),
              "soundness execution tree absent — a group's identity requires "
              "its execution tree, so this is UNDEFINED and blocks (the "
              "operator tree is not an execution surface and cannot "
              "substitute; 2026-10-11 (e))")
    else:
        _scalar_item(items, f"soundness_head@{SOUNDNESS_TREE_NAME}",
                     soundness.get("head"), SOUNDNESS_COMMIT, True,
                     "frozen soundness worktree commit")
        _scalar_item(items, "soundness_tag",
                     soundness.get("tag_commit"), SOUNDNESS_COMMIT, True,
                     f"recorded tag {soundness.get('tag') or SOUNDNESS_TAG} -> "
                     f"{SOUNDNESS_COMMIT}")
    # Image IDs: the soundness-era ID is recorded (required); the timing-era
    # ID is searched in the record first — when the record yields one it is
    # required and compared, and when it genuinely does not the item is
    # reported WAIVED: non-blocking, explicit, with the waiver text and its
    # provenance, never a silent pass (plan §6, 2026-10-11 (c) waiver,
    # semantics fixed 2026-10-11 (d)). The search is scoped to the timing
    # execution tree (2026-10-11 (f)); operator-tree candidates are recorded
    # informationally only and never upgrade the item. The waiver covers the
    # timing-image item only; every other required item keeps the
    # UNDEFINED-blocks rule.
    _scalar_item(items, "soundness_image_id", deployed.get("docker_image"),
                 SOUNDNESS_IMAGE_ID, True,
                 "deployed edge_server:latest vs the recorded soundness-era ID")
    timing_image_search = timing.get("image_search") or {}
    operator_image_search = timing.get("operator_image_search") or {}
    timing_image = timing_image_search.get("resolved")
    timing_image_waiver = None
    if timing_image:
        _scalar_item(items, "timing_image_id", deployed.get("docker_image"),
                     timing_image, True,
                     "timing-era image ID resolved from the timing execution "
                     f"tree: {timing_image_search.get('candidates')}")
    else:
        operator_candidates = operator_image_search.get("candidates") or []
        timing_image_waiver = {
            "item": "timing_image_id",
            "waiver": ("the timing-reference identity is the recorded "
                       "commit/worktree + admission-path hashes; the image "
                       "half is waived in the record because no timing-era ID "
                       "exists in the timing execution tree (plan §6) — "
                       "reported as WAIVED, non-blocking, never a silent pass"),
            "provenance": (timing_image_search.get("candidates")
                           or "timing-execution-tree search yielded no "
                              "candidates"),
            "searched": {
                "timing_root": str(timing.get("root") or ""),
                "scope": ("timing execution tree only; operator-tree "
                          "candidates are informational and never upgrade "
                          "this item (2026-10-11 (f))"),
            },
            "operator_tree_candidates": operator_candidates or None,
        }
        operator_note = ""
        if operator_candidates:
            operator_note = (" Operator-tree candidates exist but are "
                             "informational only (the operator tree is not an "
                             f"identity surface): {operator_candidates}")
        _item(items, "timing_image_id", "identity", "WAIVED", False,
              deployed.get("docker_image"), None,
              "no timing-era image ID is present in the timing execution tree "
              "(image-guard state, timing docs, its run folders): the "
              "timing-reference identity is the recorded commit/worktree + "
              "admission-path hashes, and the image half is waived for the "
              "timing group only — reported as WAIVED, non-blocking, "
              "explicitly, never a silent pass (plan §6; all other required "
              "items keep the UNDEFINED-blocks rule). Record search "
              "provenance (timing execution tree): "
              f"{timing_image_search.get('candidates') or 'no candidates'}"
              + operator_note)

    # Admission-path surface per group execution tree (plan §6.3): each group
    # is compared against its own recorded manifest; the operator tree is
    # compared informationally only and never gates (2026-10-11 (e) — the
    # operator tree is not an execution surface).
    if timing.get("present"):
        compare_surface(items, "timing_surface", timing,
                        timing.get("manifest"), soundness, capture)
    else:
        _item(items, "timing_surface", "timing_surface", "UNDEFINED", True,
              None, TIMING_MANIFEST_NAME,
              "timing execution tree absent — the group's admission-path "
              "identity cannot be established (missing required item; the "
              "operator-tree substitution path is retired 2026-10-11 (e))")
    compare_surface(items, "soundness_surface", soundness,
                    soundness.get("manifest"), soundness, capture)
    compare_surface(items, "operator_surface", deployed,
                    soundness.get("manifest"), soundness, capture,
                    informational=True)
    # C3/supporting group (2026-10-11 (f)): the supporting runs launch from
    # the operator/campaign tree, which is their recorded execution surface.
    compare_support_group(items, capture.get("support") or {})

    drifts = [item for item in items
              if item["status"] == "DRIFT" and item["required"]]
    undefined_required = [item for item in items
                          if item["status"] == "UNDEFINED" and item["required"]]
    not_recorded = [item for item in items
                    if item["status"] == "NOT_RECORDED"]

    # Stage-scoped reading (plan §6, 2026-10-11 (g)): the C1/C2 matrix gate
    # covers the timing and soundness groups; the C3 gate covers the
    # supporting/campaign group. A support-group block stops only C3; a
    # timing/soundness block stops the matrix. The overall verdict is the
    # conjunction of the two stages (an operator-surface item is informational
    # and belongs to no stage).
    stage_of_group = {"identity": "c1_c2_gate",
                      "timing_surface": "c1_c2_gate",
                      "soundness_surface": "c1_c2_gate",
                      "support_surface": "c3_gate"}

    def _stage_reading(stage: str) -> dict:
        stage_items = [item for item in items
                       if stage_of_group.get(item["group"]) == stage]
        stage_drift = [item for item in stage_items
                       if item["status"] == "DRIFT" and item["required"]]
        stage_undefined = [item for item in stage_items
                           if item["status"] == "UNDEFINED" and item["required"]]
        if stage_drift:
            stage_verdict = "DRIFT"
        elif stage_undefined:
            stage_verdict = "UNDEFINED"
        else:
            stage_verdict = "MATCH"
        return {
            "gate": stage,
            "verdict": stage_verdict,
            "blocked": stage_verdict != "MATCH",
            "drift_items": [item["item"] for item in stage_drift],
            "undefined_required_items": [item["item"] for item in stage_undefined],
            "waived_items": [item["item"] for item in stage_items
                             if item["status"] == "WAIVED"],
            "not_recorded_items": [item["item"] for item in stage_items
                                   if item["status"] == "NOT_RECORDED"],
        }

    stage_c1_c2 = _stage_reading("c1_c2_gate")
    stage_c3 = _stage_reading("c3_gate")

    if drifts:
        verdict = "DRIFT"
    elif undefined_required:
        verdict = "UNDEFINED"
    else:
        verdict = "MATCH"
    return {
        "family": "rq3_admission_modes",
        "gate": "P0-platform-identity",
        "verdict": verdict,
        "match": verdict == "MATCH",
        "drift_items": [item["item"] for item in drifts],
        "undefined_required_items": [item["item"] for item in undefined_required],
        "waived_items": [item["item"] for item in items
                         if item["status"] == "WAIVED"],
        # W2: items with no recorded reference are informational, but the
        # verdict output must make them visible (what was NOT checked).
        "not_recorded_items": [item["item"] for item in not_recorded],
        "not_recorded_summary": (f"NOT_RECORDED (informational): "
                                 f"{len(not_recorded)} items"),
        # Stage-scoped gates (2026-10-11 (g)): the runner reads these, not the
        # conjunction alone — a c3_gate block stops only the supporting batch.
        "c1_c2_gate": stage_c1_c2["verdict"],
        "c3_gate": stage_c3["verdict"],
        "stages": {"c1_c2_gate": stage_c1_c2, "c3_gate": stage_c3},
        "stage_reading_note": (
            "verdict = conjunction of c1_c2_gate and c3_gate; c1_c2_gate "
            "covers the timing + soundness groups (matrix), c3_gate covers the "
            "supporting/campaign group (C3). A c3_gate block stops only the "
            "supporting batch; a c1_c2_gate block stops the matrix."),
        "execution_trees": {
            "timing": timing.get("root") or str(Path.home() / TIMING_TREE_NAME),
            "soundness": (soundness.get("root")
                          or str(Path.home() / SOUNDNESS_TREE_NAME)),
            "support": ((capture.get("support") or {}).get("root")
                        or str(REPO_ROOT)),
        },
        "operator_tree": {
            "root": deployed.get("root"),
            "role": ("operator tree — not an execution surface "
                     "(informational only; never gates)"),
        },
        "items": items,
        "recorded_reference": {
            "timing": {"tree": TIMING_TREE_NAME, "commit": TIMING_COMMIT,
                       "tag_preference": TIMING_TAG_PREFERRED,
                       "tag_glob": TIMING_TAG_GLOB, "manifest": TIMING_MANIFEST_NAME,
                       "image_waiver": timing_image_waiver},
            "soundness": {"tree": SOUNDNESS_TREE_NAME, "commit": SOUNDNESS_COMMIT,
                          "tag": SOUNDNESS_TAG, "manifest": SOUNDNESS_MANIFEST_NAME,
                          "image_id": SOUNDNESS_IMAGE_ID},
            "knob_blocks": {key: [list(block) for block in blocks]
                            for key, blocks in KNOB_BLOCKS.items()},
            "support": {"tree": SUPPORT_TREE_NAME,
                        "v2_env_cr_sha256": dict(SUPPORT_V2_ENV_CR_SHA256),
                        "set_specs": {name: {"rate": spec["rate"],
                                             "window": spec["window"],
                                             "snapshot_globs":
                                                 list(spec["snapshot_globs"]),
                                             "recorded_snapshot_sha256":
                                                 spec["recorded_snapshot_sha256"]}
                                      for name, spec in SUPPORT_SET_SPECS.items()},
                        "drop_knob_required": False},
            "off_path_proof": {**OFF_PATH_PROOF, "verified":
                               bool((capture.get("off_path_proof") or {})
                                    .get("verified"))},
        },
        "corrective_action_on_block": (
            "plan §6.4: do not extend; record the drift evidence in "
            "campaign_log.md and execute the corrective full re-run matrix "
            "(84 matrix runs — 21 per condition: intact, lost, premature "
            "faulted and premature matched partners — plus the 5 supporting "
            "runs) on the deployed build"),
        "exit_code_convention": {"0": "MATCH",
                                 "3": "DRIFT or blocking UNDEFINED / error "
                                      "(overall verdict = conjunction of the "
                                      "stage gates; read c1_c2_gate/c3_gate "
                                      "for the per-stage decision)"},
    }


def fixture_capture() -> dict:
    """Deterministic capture used by --selftest (no git/docker needed)."""
    def files(offset: int) -> dict:
        out = {}
        for index, relative in enumerate(SURFACE_FILES):
            digest = hashlib.sha256(f"fixture:{offset}:{relative}".encode()).hexdigest()
            record = {"present": True, "sha256": digest, "stripped_sha256": None,
                      "admission_functions_sha256": None}
            if relative.endswith("compute_node_manager.py"):
                record["admission_functions_sha256"] = hashlib.sha256(
                    b"fixture:admission-functions").hexdigest()
            out[relative] = record
        return out

    def manifest(offset: int) -> dict:
        protected = {}
        for relative in SURFACE_FILES:
            if relative.endswith(".env"):
                continue
            protected[relative] = hashlib.sha256(
                f"fixture:{offset}:{relative}".encode()).hexdigest()
        arm_envs = {relative: hashlib.sha256(f"fixture:{offset}:{relative}".encode())
                    .hexdigest()
                    for relative in SURFACE_FILES if relative.endswith(".env")}
        return {"protected_sha256": protected, "arm_env_sha256": arm_envs}

    def tree(offset: int, commit: str) -> dict:
        return {
            "present": True, "root": f"/fixture/{commit}", "head": commit,
            "head_error": None, "tag": "fixture-tag", "tag_commit": commit,
            "tag_error": None, "manifest_path": f"/fixture/{commit}/manifest.json",
            "manifest": manifest(offset), "manifest_sha256": "0" * 64,
            "manifest_error": None, "files": files(offset),
        }

    timing = tree(0, TIMING_COMMIT)
    timing["image_search"] = {
        "resolved": SOUNDNESS_IMAGE_ID,
        "candidates": [{"source": "fixture:image_state", "image_id": SOUNDNESS_IMAGE_ID}],
    }
    timing["operator_image_search"] = {"resolved": None, "candidates": [],
                                       "scope": "operator tree (informational only)"}

    def support_surface() -> dict:
        envs = {name: {"present": True, "sha256": "s" * 64,
                       "cr_stripped_sha256": expected}
                for name, expected in SUPPORT_V2_ENV_CR_SHA256.items()}
        sets = {}
        for name, spec in SUPPORT_SET_SPECS.items():
            recorded = spec["recorded_snapshot_sha256"]
            sets[name] = {
                "snapshot": f"fixture_{name}_1", "snapshot_path": "/fixture",
                "sha256": recorded if recorded is not None else "c" * 64,
                "profile": {"ok": True, "detail": "fixture profile semantics"},
                "recorded_snapshot_sha256": recorded,
            }
        return {
            "root": "/fixture/support", "name": SUPPORT_TREE_NAME,
            "present": True, "head": "e" * 40, "envs": envs, "sets": sets,
            "phases_present": True, "phases_sha256": "p" * 64,
            "phases_family_profile": {"ok": True,
                                      "detail": "fixture family profile"},
        }

    return {
        "captured_at": "2026-10-11T00:00:00+00:00",
        "deployed": {"root": "/fixture/deployed", "head": "f" * 40,
                     "head_error": None, "docker_image": SOUNDNESS_IMAGE_ID,
                     "docker_error": None, "files": files(2)},
        "timing": timing,
        "soundness": tree(2, SOUNDNESS_COMMIT),
        "support": support_surface(),
        "off_path_proof": {"verified": True,
                           "detail": "fixture preflight-log facts",
                           "artifact": OFF_PATH_PROOF["artifact"]},
    }


def selftest() -> int:
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(f"  ok: {label}" if condition else f"FAIL: {label}")
        if not condition:
            failures.append(label)

    capture = fixture_capture()
    report = compare(copy.deepcopy(capture))
    check(report["verdict"] == "MATCH", "fixture MATCH verdict")
    check(not any(item["status"] == "DRIFT" for item in report["items"])
          and not any(item["status"] == "UNDEFINED" and item["required"]
                      for item in report["items"]),
          "fixture: no drift and no blocking UNDEFINED items")
    check(any(item["item"] == "timing_image_id"
              and item["status"] == "MATCH" for item in report["items"]),
          "timing image ID resolved from the record -> required and matched")
    check(report["operator_tree"]["role"].startswith("operator tree")
          and report["execution_trees"]["timing"]
          and report["execution_trees"]["soundness"]
          and report["execution_trees"]["support"]
          and all(item["required"] is False for item in report["items"]
                  if item["group"] == "operator_surface"),
          "operator tree reported informationally; execution trees recorded")
    check(report["not_recorded_summary"] == "NOT_RECORDED (informational): 0 items"
          and report["not_recorded_items"] == [],
          "NOT_RECORDED summary line counts the items with no recorded "
          "reference (0 since the cell12 snapshot provisioning, 2026-10-12)")
    check(report["c1_c2_gate"] == "MATCH" and report["c3_gate"] == "MATCH"
          and report["stages"]["c1_c2_gate"]["blocked"] is False
          and report["stages"]["c3_gate"]["blocked"] is False
          and report["stage_reading_note"].startswith("verdict = conjunction"),
          "stage gates: baseline fixture is MATCH on both stages")

    # C3/supporting group (2026-10-11 (f)): the v2 env files and the per-set
    # archived profile hashes gate; the drop knob is not part of its surface.
    support_drift = copy.deepcopy(capture)
    support_drift["support"]["envs"]["rq3_discovery.env"]["cr_stripped_sha256"] = "d" * 64
    report_support_drift = compare(support_drift)
    check(report_support_drift["verdict"] == "DRIFT"
          and "support_surface:rq3_discovery.env" in report_support_drift["drift_items"],
          "supporting v2 env file drift -> DRIFT (required and checked)")
    support_hash = copy.deepcopy(capture)
    support_hash["support"]["sets"]["camp"]["sha256"] = "b" * 64
    report_support_hash = compare(support_hash)
    check(report_support_hash["verdict"] == "DRIFT"
          and "support_surface:set:camp:snapshot-hash"
          in report_support_hash["drift_items"],
          "per-set archived profile hash drift -> DRIFT (recorded reference)")
    support_profile = copy.deepcopy(capture)
    support_profile["support"]["sets"]["disc15"]["profile"] = {
        "ok": False, "detail": "compute_spike rate != 3.0"}
    report_support_profile = compare(support_profile)
    check(report_support_profile["verdict"] == "DRIFT"
          and "support_surface:set:disc15:profile"
          in report_support_profile["drift_items"],
          "set profile semantics drift -> DRIFT (launcher assertion mirrored)")
    support_absent = copy.deepcopy(capture)
    support_absent["support"]["present"] = False
    report_support_absent = compare(support_absent)
    check(report_support_absent["verdict"] == "UNDEFINED"
          and "support_tree" in report_support_absent["undefined_required_items"]
          and report_support_absent["c1_c2_gate"] == "MATCH"
          and report_support_absent["c3_gate"] == "UNDEFINED"
          and report_support_absent["stages"]["c1_c2_gate"]["blocked"] is False
          and report_support_absent["stages"]["c3_gate"]["blocked"] is True,
          "support-stage UNDEFINED blocks only C3 (c1_c2_gate stays MATCH)")
    check(all("control_events" not in item["item"]
              for item in report["items"]
              if item["group"] == "support_surface")
          and report["recorded_reference"]["support"]["drop_knob_required"] is False,
          "the drop knob is not part of the supporting group's surface")

    # (g) The support snapshot resolver mirrors the supporting launcher
    # exactly: per glob in order, the LEXICOGRAPHICALLY FIRST matching folder
    # of that glob; the folder must carry phases_snapshot.json or the resolver
    # moves to the NEXT GLOB — never to a later folder of the same glob. With
    # no glob resolving the set is UNDEFINED and blocks (the launcher STOPs).
    def set_profile(rate: float) -> str:
        return json.dumps({"phases": [
            {"name": "baseline", "duration_s": 60, "rate_per_client": 1.0},
            {"name": "compute_spike", "duration_s": 180,
             "rate_per_client": rate,
             "mix": {"service_pressure": 0.9, "feed_ranking": 0.05,
                     "content_lookup": 0.05}},
            {"name": "cleanup_gap", "duration_s": 180,
             "rate_per_client": 0.5}]}, indent=2) + "\n"

    with tempfile.TemporaryDirectory() as resolver_tmp:
        tree = Path(resolver_tmp)
        metrics_dir = tree / "source/scripts/testing/metrics"
        # glob-1 (*_rq3_direct_*): its first folder lacks the snapshot; a later
        # folder of the SAME glob carries one (the launcher never uses it).
        (metrics_dir / "20260804_000001_rq3_direct_1").mkdir(parents=True)
        later = metrics_dir / "20260805_000001_rq3_direct_2"
        later.mkdir(parents=True)
        (later / "phases_snapshot.json").write_text(set_profile(3.0),
                                                    encoding="utf-8")
        # glob-2 (*_rq3_disc_*): its first folder carries the snapshot — the
        # launcher's resolution lands here.
        disc = metrics_dir / "20260806_000001_rq3_disc_1"
        disc.mkdir(parents=True)
        (disc / "phases_snapshot.json").write_text(set_profile(3.0),
                                                   encoding="utf-8")
        capture_partial = capture_support_surface(tree)
        camp = capture_partial["sets"]["camp"]
        check(camp["snapshot"] == "20260806_000001_rq3_disc_1"
              and (camp.get("profile") or {}).get("ok") is True,
              "resolver: glob-1 first folder without snapshot -> glob-2 first "
              "folder (a later same-glob folder is never used) and the profile "
              "check runs on the resolved snapshot")

    with tempfile.TemporaryDirectory() as resolver_tmp:
        tree = Path(resolver_tmp)
        metrics_dir = tree / "source/scripts/testing/metrics"
        # Only a snapshot-less glob-1 folder: no glob resolves.
        (metrics_dir / "20260804_000001_rq3_direct_1").mkdir(parents=True)
        capture_none = capture_support_surface(tree)
        items_none: list = []
        compare_support_group(items_none, capture_none)
        camp_item = next((item for item in items_none
                          if item["item"] == "support_surface:set:camp"), None)
        check(capture_none["sets"]["camp"]["snapshot"] is None
              and camp_item is not None
              and camp_item["status"] == "UNDEFINED"
              and camp_item["required"] is True,
              "resolver: no glob resolves -> blocking UNDEFINED for the set")

    # The recorded scoping defect: the operator tree's control_events.py
    # lacks the drop knob -> informational only, never DRIFT in the verdict.
    operator_div = copy.deepcopy(capture)
    operator_div["deployed"]["files"]["source/sdn_controller/control_events.py"]["sha256"] = "x" * 64
    report_operator = compare(operator_div)
    check(report_operator["verdict"] == "MATCH"
          and any(item["item"].endswith("control_events.py")
                  and item["status"] == "DRIFT"
                  and item["required"] is False
                  and "operator tree" in item["note"]
                  for item in report_operator["items"]),
          "operator-tree divergence -> informational, never DRIFT in the "
          "verdict (the execution trees gate)")

    # Drift: one EXECUTION-tree non-knob file differs from its recorded
    # manifest (the soundness group's manifest).
    drift = copy.deepcopy(capture)
    drift["soundness"]["files"]["source/sdn_controller/control_events.py"]["sha256"] = "x" * 64
    report_drift = compare(drift)
    check(report_drift["verdict"] == "DRIFT"
          and any("control_events.py" in name
                  for name in report_drift["drift_items"])
          and report_drift["c1_c2_gate"] == "DRIFT"
          and report_drift["c3_gate"] == "MATCH",
          "admission-path difference outside a knob block -> DRIFT "
          "(c1_c2_gate blocks; c3_gate unaffected)")

    # Knob rule: app.py raw differs, declared-block-stripped equal -> MATCH.
    knob = copy.deepcopy(capture)
    knob_path = "source/docker/edge_server/source/app.py"
    knob["soundness"]["files"][knob_path]["sha256"] = "y" * 64
    for tree in ("deployed", "soundness"):
        knob[tree]["files"][knob_path]["stripped_sha256"] = "z" * 64
    report_knob = compare(knob)
    check(report_knob["verdict"] == "MATCH"
          and any(item["item"].endswith(knob_path)
                  and "knob" in item["note"]
                  for item in report_knob["items"]),
          "difference confined to the declared knob block -> MATCH")

    # Knob block unverifiable (anchor missing + no strip) -> DRIFT, never a
    # silent pass.
    unverifiable = copy.deepcopy(knob)
    unverifiable["soundness"]["files"][knob_path]["stripped_sha256"] = None
    unverifiable["soundness"]["files"][knob_path]["knob_block_error"] = "anchor missing"
    report_unverifiable = compare(unverifiable)
    check(report_unverifiable["verdict"] == "DRIFT"
          and any(item["item"].startswith("soundness_surface:")
                  and "unverifiable" in item["note"]
                  for item in report_unverifiable["items"]),
          "missing declared knob-block anchor -> DRIFT (fail closed)")

    # Off-path proof substitution -- unit-level on the timing reference: the
    # same shape passes only while the verified proof artifact is present.
    soundness_capture = copy.deepcopy(capture["soundness"])
    soundness_hash = hashlib.sha256(f"fixture:2:{knob_path}".encode()).hexdigest()
    tree_with_proof_shape = {
        "manifest_error": None,
        "files": {knob_path: {"present": True, "sha256": soundness_hash,
                              "stripped_sha256": None,
                              "knob_block_error": "anchor missing",
                              "admission_functions_sha256": None}}}
    timing_manifest = {"protected_sha256": {knob_path: "a" * 64},
                       "arm_env_sha256": {}}
    items_proof: list = []
    compare_surface(items_proof, "unit_timing", tree_with_proof_shape,
                    timing_manifest, soundness_capture,
                    {"off_path_proof": {"verified": True}})
    check(any(item["item"].startswith("unit_timing:" + knob_path)
              and item["status"] == "MATCH"
              and "off-path byte-identity proof verified" in item["note"]
              for item in items_proof),
          "verified off-path proof substitutes on the timing reference")
    items_denied: list = []
    compare_surface(items_denied, "unit_timing", tree_with_proof_shape,
                    timing_manifest, soundness_capture,
                    {"off_path_proof": {"verified": False}})
    check(any(item["item"].startswith("unit_timing:" + knob_path)
              and item["status"] == "DRIFT"
              and "substitution denied" in item["note"]
              for item in items_denied),
          "unverified off-path proof denies the substitution -> DRIFT")

    # verify_off_path_proof reads the recorded artifact, never a constant.
    with tempfile.TemporaryDirectory() as proof_root:
        root = Path(proof_root)
        artifact = root / OFF_PATH_PROOF["artifact"]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        ok, _ = verify_off_path_proof(root)
        check(ok is False, "off-path proof artifact missing -> not verified")
        artifact.write_text(
            "edge_server rebuilt = `207b1f9e8f64`; live "
            "`bind-timing make_server(bind+listen)=0.000 s`",
            encoding="utf-8")
        ok, detail = verify_off_path_proof(root)
        check(ok is True, "off-path proof artifact with recorded facts -> verified")

    # UNDEFINED: a required recorded file missing in the compared tree.
    undefined = copy.deepcopy(capture)
    undefined["soundness"]["files"]["source/sdn_controller/control_events.py"]["sha256"] = None
    report_undefined = compare(undefined)
    check(report_undefined["verdict"] == "UNDEFINED",
          "missing required item -> UNDEFINED blocks")

    # UNDEFINED: soundness manifest absent.
    no_manifest = copy.deepcopy(capture)
    no_manifest["soundness"]["manifest"] = None
    report_no_manifest = compare(no_manifest)
    check(report_no_manifest["verdict"] == "UNDEFINED",
          "missing recorded reference manifest -> UNDEFINED blocks")
    check(all(item["required"] is False
              for item in report_no_manifest["items"]
              if item["group"] == "operator_surface")
          and "operator_surface:manifest"
          not in report_no_manifest["undefined_required_items"],
          "operator-surface manifest gap stays informational (early-return path)")

    # UNDEFINED: differently-shaped manifest never yields a silent MATCH.
    bad_shape = copy.deepcopy(capture)
    bad_shape["soundness"]["manifest"] = {"hashes": {"x": "y"}}
    report_bad_shape = compare(bad_shape)
    check(report_bad_shape["verdict"] == "UNDEFINED",
          "differently-shaped manifest -> UNDEFINED (coverage fail-closed)")

    # WAIVED: timing-era image absent from the record -> reported WAIVED,
    # non-blocking, with the waiver text and its provenance (plan §6).
    no_image = copy.deepcopy(capture)
    no_image["timing"]["image_search"] = {"resolved": None, "candidates": []}
    report_no_image = compare(no_image)
    waived_item = next((item for item in report_no_image["items"]
                        if item["item"] == "timing_image_id"), None)
    check(report_no_image["verdict"] == "MATCH"
          and waived_item is not None and waived_item["status"] == "WAIVED"
          and waived_item["required"] is False
          and "waived" in waived_item["note"].lower()
          and "timing_image_id" in report_no_image["waived_items"]
          and report_no_image["recorded_reference"]["timing"]["image_waiver"],
          "timing image absent from the record -> WAIVED (non-blocking, "
          "waiver text + provenance reported)")
    check(waived_item is not None
          and waived_item["item"] not in report_no_image["undefined_required_items"],
          "timing-image waiver never surfaces as a blocking UNDEFINED")

    # (f) The timing-image search is scoped to the timing execution tree: an
    # operator-tree candidate is recorded informationally and never upgrades
    # the item to required.
    operator_candidate = copy.deepcopy(capture)
    operator_candidate["timing"]["image_search"] = {"resolved": None,
                                                    "candidates": []}
    operator_candidate["timing"]["operator_image_search"] = {
        "resolved": SOUNDNESS_IMAGE_ID,
        "candidates": [{"source": "operator_tree:rq3lh_image_state.json",
                        "image_id": SOUNDNESS_IMAGE_ID}],
        "scope": "operator tree (informational only)"}
    report_operator_candidate = compare(operator_candidate)
    op_item = next((item for item in report_operator_candidate["items"]
                    if item["item"] == "timing_image_id"), None)
    check(report_operator_candidate["verdict"] == "MATCH"
          and op_item is not None and op_item["status"] == "WAIVED"
          and op_item["required"] is False
          and "informational only" in op_item["note"]
          and report_operator_candidate["recorded_reference"]["timing"]
          ["image_waiver"]["operator_tree_candidates"],
          "operator-tree image candidate stays informational -> WAIVED, never "
          "upgraded to required")

    # Timing image resolved but mismatching the deployed image -> DRIFT.
    image_mismatch = copy.deepcopy(capture)
    image_mismatch["deployed"]["docker_image"] = "0" * 12
    report_image_mismatch = compare(image_mismatch)
    check(report_image_mismatch["verdict"] == "DRIFT",
          "resolved timing image mismatch -> DRIFT")

    # Absent timing execution tree -> the group's identity is UNDEFINED and
    # blocks (no operator-tree substitution; 2026-10-11 (e)).
    absent_tree = copy.deepcopy(capture)
    absent_tree["timing"]["present"] = False
    absent_tree["timing"]["manifest"] = None
    absent_tree["timing"]["files"] = {}
    absent_tree["timing"]["tag_from_deployed"] = {
        "tag": TIMING_TAG_PREFERRED, "tag_commit": TIMING_COMMIT}
    report_absent = compare(absent_tree)
    check(report_absent["verdict"] == "UNDEFINED"
          and any(item["item"] == "timing_tree" and item["required"]
                  for item in report_absent["items"])
          and "timing_tree" in report_absent["undefined_required_items"]
          and any(item["item"] == "timing_surface" and item["required"]
                  for item in report_absent["items"]),
          "absent timing execution tree -> UNDEFINED blocks (no operator-tree "
          "substitution)")

    # Absent soundness execution tree -> UNDEFINED blocks as well.
    absent_snd = copy.deepcopy(capture)
    absent_snd["soundness"]["present"] = False
    absent_snd["soundness"]["manifest"] = None
    absent_snd["soundness"]["files"] = {}
    report_absent_snd = compare(absent_snd)
    check(report_absent_snd["verdict"] == "UNDEFINED"
          and any(item["item"] == "soundness_tree" and item["required"]
                  for item in report_absent_snd["items"])
          and "soundness_tree" in report_absent_snd["undefined_required_items"],
          "absent soundness execution tree -> UNDEFINED blocks")

    # End-to-end CLI path with --compare-only and a small file tree.
    with tempfile.TemporaryDirectory() as tmp:
        capture_path = Path(tmp) / "capture.json"
        capture_path.write_text(json.dumps(capture), encoding="utf-8")
        out_path = Path(tmp) / "platform_identity.json"
        code_match = main(["--compare-only", str(capture_path), "--out", str(out_path)])
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        check(code_match == 0 and payload["verdict"] == "MATCH",
              "--compare-only: exit 0 on MATCH and analysis JSON emitted")
        capture_path.write_text(json.dumps(drift), encoding="utf-8")
        code_drift = main(["--compare-only", str(capture_path), "--out",
                           str(out_path)])
        check(code_drift == 3, "--compare-only: exit 3 on DRIFT")
    if failures:
        print(f"SELFTEST FAILED ({len(failures)} failure(s))", file=sys.stderr)
        return 1
    print(json.dumps({"platform_identity_selftest": "PASS"}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployed-root", type=Path, default=REPO_ROOT,
                        help="operator tree (informational only — not an "
                             "execution surface; never gates)")
    parser.add_argument("--timing-tree", type=Path,
                        default=Path.home() / TIMING_TREE_NAME,
                        help="timing group execution tree (the frozen worktree "
                             "its runs launch from)")
    parser.add_argument("--soundness-tree", type=Path,
                        default=Path.home() / SOUNDNESS_TREE_NAME,
                        help="soundness group execution tree (the frozen "
                             "worktree its runs launch from)")
    parser.add_argument("--support-tree", type=Path, default=REPO_ROOT,
                        help="supporting/C3 execution tree (the campaign tree "
                             "the five supporting runs launch from; the C3 "
                             "launcher derives REPO_ROOT from its own location)")
    parser.add_argument("--no-docker", action="store_true")
    parser.add_argument("--compare-only", type=Path, default=None,
                        help="compare a previously captured JSON (offline)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--capture-out", type=Path, default=None)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        if args.compare_only is not None:
            capture = json.loads(args.compare_only.read_text(encoding="utf-8"))
        else:
            capture = capture_identity(args.deployed_root, args.timing_tree,
                                       args.soundness_tree, args.no_docker,
                                       support_root=args.support_tree)
            capture_out = args.capture_out or (
                DEFAULT_CAPTURE if args.out == DEFAULT_OUT
                else args.out.with_name(args.out.stem + "_capture.json"))
            capture_out.parent.mkdir(parents=True, exist_ok=True)
            capture_out.write_text(
                json.dumps(capture, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        report = compare(capture)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: platform-identity capture/compare failed: {exc}",
              file=sys.stderr)
        return 3  # STOP: never a silent pass
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    # W2: the NOT_RECORDED count is printed as a summary line so a reader can
    # see what was not checked (informational; never blocking on its own).
    print(report.get("not_recorded_summary")
          or "NOT_RECORDED (informational): 0 items")
    print(f"c1_c2_gate: {report['c1_c2_gate']} (timing + soundness groups)")
    print(f"c3_gate: {report['c3_gate']} (supporting/C3 group)")
    print(json.dumps({
        "verdict": report["verdict"],
        "c1_c2_gate": report["c1_c2_gate"],
        "c3_gate": report["c3_gate"],
        "drift_items": report["drift_items"],
        "undefined_required_items": report["undefined_required_items"],
        "waived_items": report["waived_items"],
        "not_recorded_items": report["not_recorded_items"],
        "out": str(args.out),
    }, indent=2))
    return 0 if report["verdict"] == "MATCH" else 3


if __name__ == "__main__":
    raise SystemExit(main())
