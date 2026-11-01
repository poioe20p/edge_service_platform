#!/usr/bin/env python3
"""
export_workload_snapshot.py

Exports content and user-profile data from MongoDB to JSON files for use by
the traffic generator. Decouples experiment execution from a live database.

Usage:
        python3 export_workload_snapshot.py \
            [--mongo-lan1 mongodb://10.0.0.4:27018/] \
            [--mongo-lan2 mongodb://10.0.1.4:27018/] \
            [--output-dir data/workload_snapshot]
"""

import argparse
import json
import os

from pymongo import MongoClient

REGIONS = {
    "lan1": "mongodb://10.0.0.4:27018/",
    "lan2": "mongodb://10.0.1.4:27018/",
}


def export(uri_lan1: str, uri_lan2: str, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)

    allowed_snapshot_files = {"content_items.json", "user_profiles.json"}
    for snapshot_name in os.listdir(output_dir):
        snapshot_path = os.path.join(output_dir, snapshot_name)
        if not os.path.isfile(snapshot_path):
            continue
        if not snapshot_name.endswith(".json"):
            continue
        if snapshot_name in allowed_snapshot_files:
            continue
        os.remove(snapshot_path)
        print(f"Removed stale snapshot -> {snapshot_path}")

    uris = {"lan1": uri_lan1, "lan2": uri_lan2}

    all_content_items = []
    for region, uri in uris.items():
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        docs = list(
            client["edge_platform"]["content_items"].find(
                {}, {"_id": 1, "region_origin": 1}
            )
        )
        all_content_items.extend(docs)
        client.close()
        print(f"  [{region}] {len(docs)} content items")

    out_content_items = os.path.join(output_dir, "content_items.json")
    if os.path.exists(out_content_items):
        os.remove(out_content_items)
    with open(out_content_items, "w") as f:
        json.dump(all_content_items, f, indent=2, default=str)
    print(f"Exported {len(all_content_items)} content items -> {out_content_items}")

    all_user_profiles = []
    for region, uri in uris.items():
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        docs = list(
            client["edge_platform"]["user_profiles"].find(
                {},
                {
                    "_id": 1,
                    "home_region": 1,
                    "subscribed_tags": 1,
                    "followed_content": 1,
                    "profile_config": 1,
                },
            )
        )
        all_user_profiles.extend(docs)
        client.close()
        print(f"  [{region}] {len(docs)} user profiles")

    out_user_profiles = os.path.join(output_dir, "user_profiles.json")
    if os.path.exists(out_user_profiles):
        os.remove(out_user_profiles)
    with open(out_user_profiles, "w") as f:
        json.dump(all_user_profiles, f, indent=2, default=str)
    print(f"Exported {len(all_user_profiles)} user profiles -> {out_user_profiles}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export seeded MongoDB data to JSON for the traffic generator"
    )
    parser.add_argument("--mongo-lan1", default=REGIONS["lan1"], metavar="URI")
    parser.add_argument("--mongo-lan2", default=REGIONS["lan2"], metavar="URI")
    parser.add_argument(
        "--output-dir", default="data/workload_snapshot", metavar="DIR"
    )
    args = parser.parse_args()
    export(args.mongo_lan1, args.mongo_lan2, args.output_dir)
