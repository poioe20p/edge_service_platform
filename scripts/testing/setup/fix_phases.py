"""Replace phases.json with the canonical content-discovery workload.

Writes the canonical Phase C profile captured in source/scripts/testing/phases.json.
The helper exists only to restore that single canonical file; validation and
diagnostic overrides live under source/scripts/testing/phases_override/.

Usage:
    python fix_phases.py [--dry-run]
"""
import json
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
PHASES_PATH = REPO_ROOT / "source" / "scripts" / "testing" / "phases.json"

PHASES_CANONICAL = {
    "phases": [
        {
            "name": "baseline",
            "duration_s": 60,
            "rate_per_client": 1.0,
            "cross_region_ratio": 0.0,
            "client_fraction": 0.5,
            "mix": {"content_lookup": 0.60, "feed_ranking": 0.25, "service_pressure": 0.15},
        },
        {
            "name": "storage_storm",
            "duration_s": 240,
            "rate_per_client": 4.0,
            "cross_region_ratio": 0.90,
            "hotspot_direction": "",
            "client_fraction": 1.0,
            "mix": {
                "content_lookup": 0.35, "feed_ranking": 0.10, "service_pressure": 0.05,
                "content_update": 0.30, "content_aggregate": 0.20,
            },
        },
        {
            "name": "tier1_hotspot",
            "duration_s": 180,
            "rate_per_client": 5.0,
            "cross_region_ratio": 0.95,
            "hotspot_direction": "",
            "client_fraction": 1.0,
            "mix": {
                "content_lookup": 0.80, "feed_ranking": 0.05, "service_pressure": 0.05,
                "content_update": 0.05, "content_aggregate": 0.05,
            },
        },
        {
            "name": "inter_hotspot_cooldown",
            "duration_s": 300,
            "rate_per_client": 1.0,
            "cross_region_ratio": 0.0,
            "client_fraction": 0.10,
            "mix": {"content_lookup": 0.60, "feed_ranking": 0.25, "service_pressure": 0.15},
        },
        {
            "name": "compute_spike",
            "duration_s": 180,
            "rate_per_client": 4.0,
            "cross_region_ratio": 0.05,
            "hotspot_direction": "",
            "client_fraction": 1.0,
            "mix": {"content_lookup": 0.20, "feed_ranking": 0.65, "service_pressure": 0.15},
        },
        {
            "name": "cooldown",
            "duration_s": 120,
            "rate_per_client": 1.0,
            "cross_region_ratio": 0.0,
            "client_fraction": 0.10,
            "mix": {"content_lookup": 0.60, "feed_ranking": 0.25, "service_pressure": 0.15},
        },
    ]
}


def fix(dry_run: bool = False) -> None:
    if not PHASES_PATH.exists():
        print(f"ERROR: {PHASES_PATH} not found")
        sys.exit(1)

    current = json.loads(PHASES_PATH.read_text())
    current_names = [p["name"] for p in current.get("phases", [])]
    expected_names = [p["name"] for p in PHASES_CANONICAL["phases"]]
    if current == PHASES_CANONICAL:
        print("phases.json already has the canonical content-discovery workload — no changes needed.")
        return

    if dry_run:
        print(f"Would replace {len(current_names)} phases ({current_names})")
        print(f"With {len(expected_names)} phases: {expected_names}")
    else:
        shutil.copy(PHASES_PATH, str(PHASES_PATH) + ".bak")
        PHASES_PATH.write_text(json.dumps(PHASES_CANONICAL, indent=2) + "\n")
        print(f"Written canonical phases to {PHASES_PATH}:")
        for p in PHASES_CANONICAL["phases"]:
            print(f"  {p['name']}: {p['duration_s']}s")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    fix(dry_run=dry_run)
