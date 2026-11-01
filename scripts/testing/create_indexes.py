#!/usr/bin/env python3
"""
create_indexes.py — run once after seeding, creates indexes on both replica-set primaries
"""

from pymongo import MongoClient, ASCENDING, DESCENDING
import argparse

REGIONS = {
    "lan1": "mongodb://10.0.0.4:27018/",
    "lan2": "mongodb://10.0.1.4:27018/",
}


def setup(uri: str, label: str):
    client = MongoClient(uri)
    db = client["edge_platform"]

    db.content_items.create_index([("region_origin", ASCENDING)])
    db.content_items.create_index([("tags", ASCENDING)])
    db.content_items.create_index([("tags", ASCENDING), ("last_updated", DESCENDING)])
    db.content_items.create_index([("content_type", ASCENDING), ("payload.status", ASCENDING)])

    db.user_profiles.create_index([("home_region", ASCENDING)])
    db.user_profiles.create_index([("subscribed_tags", ASCENDING)])

    print(f"[{label}] Indexes created.")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-lan1", default=REGIONS["lan1"], help="MongoDB URI for LAN 1 primary")
    parser.add_argument("--mongo-lan2", default=REGIONS["lan2"], help="MongoDB URI for LAN 2 primary")
    args = parser.parse_args()
    setup(args.mongo_lan1, "lan1")
    setup(args.mongo_lan2, "lan2")
