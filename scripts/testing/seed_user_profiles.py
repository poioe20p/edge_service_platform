#!/usr/bin/env python3
"""
seed_user_profiles.py

Populates the user_profiles collection with edge user subscription profiles.
Each region's profiles are seeded into that region's own replica-set primary.

Usage:
    python3 seed_user_profiles.py --mongo-lan1 <uri> --mongo-lan2 <uri> --users <N per region>
"""

import argparse
import math
import random

from pymongo import MongoClient, UpdateOne

REGIONS = {
    "lan1": "mongodb://10.0.0.4:27018/",
    "lan2": "mongodb://10.0.1.4:27018/",
}

TOPIC_TAGS = [
    "news",
    "technology",
    "science",
    "finance",
    "sports",
    "entertainment",
    "health",
    "education",
    "featured",
    "trending",
    "premium",
    "archived",
]

RELEVANCE_OVERRIDE_RANGES = {
    "article": (100, 550),
    "video": (150, 750),
    "podcast": (60, 300),
    "image_gallery": (80, 450),
    "event": (15, 100),
    "tool": (30, 220),
    "review": (20, 170),
    "discussion": (40, 260),
    "curated_list": (20, 130),
}

PROFILE_SPECS = {
    "focused": {
        "weight": 0.55,
        "tag_range": (1, 2),
        "followed_content_range": (3, 6),
        "follow_mode_weights": {"local": 0.80, "mixed": 0.20, "foreign": 0.00},
        "mixed_mode_foreign_share": 0.25,
    },
    "broad": {
        "weight": 0.30,
        "tag_range": (3, 4),
        "followed_content_range": (4, 8),
        "follow_mode_weights": {"local": 0.45, "mixed": 0.45, "foreign": 0.10},
        "mixed_mode_foreign_share": 0.50,
    },
    "global": {
        "weight": 0.15,
        "tag_range": (4, 6),
        "followed_content_range": (6, 12),
        "follow_mode_weights": {"local": 0.20, "mixed": 0.55, "foreign": 0.25},
        "mixed_mode_foreign_share": 0.70,
    },
}


def half_up_count(total: int, ratio: float) -> int:
    return int(math.floor((total * ratio) + 0.5))


def sample_metric(low: float, high: float, precision: int = 2) -> float:
    return round(random.uniform(low, high), precision)


def pick_weighted_key(weights: dict[str, float]) -> str:
    roll = random.random()
    cumulative = 0.0
    last_key = next(iter(weights))
    for key, weight in weights.items():
        cumulative += weight
        last_key = key
        if roll <= cumulative:
            return key
    return last_key


def build_followed_content(
    profile_kind: str,
    local_content_ids: list[str],
    foreign_content_ids: list[str],
) -> list[str]:
    spec = PROFILE_SPECS[profile_kind]
    follow_mode = pick_weighted_key(spec["follow_mode_weights"])
    low, high = spec["followed_content_range"]
    target_count = random.randint(low, high)

    if follow_mode == "local":
        return random.sample(local_content_ids, k=min(target_count, len(local_content_ids)))

    if follow_mode == "foreign":
        return random.sample(foreign_content_ids, k=min(target_count, len(foreign_content_ids)))

    if target_count <= 1:
        combined = local_content_ids + foreign_content_ids
        return random.sample(combined, k=min(target_count, len(combined)))

    foreign_share = spec["mixed_mode_foreign_share"]
    foreign_count = max(1, min(target_count - 1, half_up_count(target_count, foreign_share)))
    local_count = max(1, target_count - foreign_count)
    followed_content = (
        random.sample(local_content_ids, k=min(local_count, len(local_content_ids)))
        + random.sample(foreign_content_ids, k=min(foreign_count, len(foreign_content_ids)))
    )
    random.shuffle(followed_content)
    return followed_content


def build_relevance_override() -> dict[str, float]:
    return {
        content_type: sample_metric(*override_range)
        for content_type, override_range in RELEVANCE_OVERRIDE_RANGES.items()
    }


def make_user_profile(
    region: str,
    index: int,
    local_content_ids: list[str],
    foreign_content_ids: list[str],
) -> dict:
    profile_kind = pick_weighted_key(
        {profile_name: spec["weight"] for profile_name, spec in PROFILE_SPECS.items()}
    )
    tag_low, tag_high = PROFILE_SPECS[profile_kind]["tag_range"]
    subscribed_tags = random.sample(TOPIC_TAGS, k=random.randint(tag_low, tag_high))

    return {
        "_id": f"{region}::user::{index:03d}",
        "home_region": region,
        "profile_kind": profile_kind,
        "subscribed_tags": subscribed_tags,
        "followed_content": build_followed_content(profile_kind, local_content_ids, foreign_content_ids),
        "profile_config": {
            "email": f"ops-{region}@example.com",
            "relevance_override": build_relevance_override(),
        },
    }


def seed(uri_lan1: str, uri_lan2: str, users_per_region: int, content_items_per_region: int):
    lan1_content_ids = [f"lan1::content::{index:03d}" for index in range(1, content_items_per_region + 1)]
    lan2_content_ids = [f"lan2::content::{index:03d}" for index in range(1, content_items_per_region + 1)]
    local_ids = {"lan1": lan1_content_ids, "lan2": lan2_content_ids}
    foreign_ids = {"lan1": lan2_content_ids, "lan2": lan1_content_ids}

    uris = {"lan1": uri_lan1, "lan2": uri_lan2}

    for region, uri in uris.items():
        client = MongoClient(uri)
        db = client["edge_platform"]

        ops = []
        for index in range(1, users_per_region + 1):
            doc = make_user_profile(region, index, local_ids[region], foreign_ids[region])
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))

        result = db["user_profiles"].bulk_write(ops, ordered=False)
        print(
            f"[{region}] Upserted user profiles: {result.upserted_count}, "
            f"Modified: {result.modified_count}"
        )
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-lan1", default=REGIONS["lan1"], help="MongoDB URI for LAN 1 primary")
    parser.add_argument("--mongo-lan2", default=REGIONS["lan2"], help="MongoDB URI for LAN 2 primary")
    parser.add_argument("--users", type=int, default=40, help="User profiles per region")
    parser.add_argument("--content-items", type=int, default=100, help="Content items per region")
    parser.add_argument("--data-seed", type=int, default=None, help="Fixed seed for reproducible user generation")
    args = parser.parse_args()
    if args.data_seed is not None:
        random.seed(args.data_seed)
    seed(args.mongo_lan1, args.mongo_lan2, args.users, args.content_items)