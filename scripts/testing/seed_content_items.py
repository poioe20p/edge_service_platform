#!/usr/bin/env python3
"""
seed_content_items.py

Populates the content_items collection with heterogeneous content documents.
Each region's content set is seeded into that region's own replica-set primary.

Usage:
    python3 seed_content_items.py --mongo-lan1 <uri> --mongo-lan2 <uri> --content-items <N per region>
"""

import argparse
import math
import random
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, UpdateOne

REGIONS = {
    "lan1": "mongodb://10.0.0.4:27018/",
    "lan2": "mongodb://10.0.1.4:27018/",
}

STATUS_WEIGHTS = {
    "quiet": 0.55,
    "trending": 0.30,
    "hot": 0.15,
}

ENGAGEMENT_MULTIPLIERS = {
    "quiet": (0.35, 0.84),
    "trending": (0.85, 0.99),
    "hot": (1.00, 1.35),
}

STATUS_CONDITIONED_TAGS = {"trending", "archived"}

CONTENT_SPECS = {
    "article": {
        "unit": "views/h",
        "baseline_range": (120, 500),
        "preferred_tags": ("news", "technology", "science", "finance", "education"),
    },
    "video": {
        "unit": "streams/h",
        "baseline_range": (180, 700),
        "preferred_tags": ("entertainment", "featured", "technology", "sports"),
    },
    "podcast": {
        "unit": "listens/h",
        "baseline_range": (80, 260),
        "preferred_tags": ("education", "news", "technology", "health", "finance"),
    },
    "image_gallery": {
        "unit": "views/h",
        "baseline_range": (100, 420),
        "preferred_tags": ("featured", "entertainment", "news", "sports", "science"),
    },
    "event": {
        "unit": "rsvps/h",
        "baseline_range": (15, 90),
        "preferred_tags": ("featured", "entertainment", "education", "sports", "premium"),
    },
    "tool": {
        "unit": "sessions/h",
        "baseline_range": (40, 180),
        "preferred_tags": ("technology", "finance", "education", "featured", "premium"),
    },
    "review": {
        "unit": "interactions/h",
        "baseline_range": (30, 140),
        "preferred_tags": ("premium", "technology", "entertainment", "health", "featured"),
    },
    "discussion": {
        "unit": "replies/h",
        "baseline_range": (60, 240),
        "preferred_tags": ("news", "entertainment", "sports", "technology"),
    },
    "curated_list": {
        "unit": "saves/h",
        "baseline_range": (25, 110),
        "preferred_tags": ("featured", "premium", "education", "entertainment"),
    },
}

CONTENT_TYPES = tuple(CONTENT_SPECS.keys())

PROS_POOL = [
    "clear structure",
    "useful local angle",
    "strong visuals",
    "timely update",
    "good expert context",
]

CONS_POOL = [
    "limited detail",
    "narrow scope",
    "repetitive examples",
    "late refresh",
    "shallow sourcing",
]


def half_up_count(total: int, ratio: float) -> int:
    return int(math.floor((total * ratio) + 0.5))


def sample_bool(probability: float) -> bool:
    return random.random() < probability


def sample_metric(low: float, high: float, precision: int = 2) -> float:
    return round(random.uniform(low, high), precision)


def sample_enum(prefix: str, start: int, end: int) -> str:
    return f"{prefix}_{random.randint(start, end)}"


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


def build_body_preview(region: str, index: int) -> str:
    return (
        f"{region} article {index} briefs readers on regional developments, "
        "featured context, and current discovery highlights for the feed cycle."
    )


def build_article_payload(region: str, index: int, now: datetime) -> dict:
    del now
    word_count = random.randint(400, 2200)
    return {
        "headline": f"{region} article {index}",
        "author": sample_enum("author", 1, 20),
        "word_count": word_count,
        "reading_time_s": round(word_count / random.uniform(3.2, 4.2)),
        "body_preview": build_body_preview(region, index),
    }


def build_article_metadata(now: datetime) -> dict:
    del now
    return {
        "section": random.choice(["local", "world", "business", "science", "culture"]),
        "language": random.choice(["en", "pt"]),
        "publisher_tier": random.choice(["local", "partner", "flagship"]),
        "source": random.choice(["editorial", "syndicated", "contributor"]),
    }


def build_video_payload(region: str, index: int, now: datetime) -> dict:
    del now
    return {
        "title": f"{region} video {index}",
        "duration_s": random.randint(60, 3600),
        "resolution": random.choice(["720p", "1080p", "1440p", "4k"]),
        "creator": sample_enum("creator", 1, 30),
        "captioned": sample_bool(0.50),
    }


def build_video_metadata(now: datetime) -> dict:
    del now
    return {
        "channel": sample_enum("channel", 1, 20),
        "language": random.choice(["en", "pt"]),
        "distribution_tier": random.choice(["local", "partner", "featured"]),
        "source": random.choice(["studio", "mobile", "partner_feed"]),
    }


def build_podcast_payload(region: str, index: int, now: datetime) -> dict:
    del now
    return {
        "title": f"{region} podcast {index}",
        "duration_s": random.randint(300, 5400),
        "host": sample_enum("host", 1, 15),
        "episode_number": random.randint(1, 300),
        "transcript_available": sample_bool(0.50),
    }


def build_podcast_metadata(now: datetime) -> dict:
    del now
    return {
        "series": sample_enum("series", 1, 20),
        "language": random.choice(["en", "pt"]),
        "release_cadence": random.choice(["daily", "weekly", "biweekly"]),
        "source": random.choice(["recording", "live", "partner_audio"]),
    }


def build_image_gallery_payload(region: str, index: int, now: datetime) -> dict:
    del now
    return {
        "title": f"{region} gallery {index}",
        "image_count": random.randint(4, 24),
        "cover_caption": f"{region} gallery {index} highlights the current regional visual collection.",
        "dominant_format": random.choice(["photo", "illustration", "mixed"]),
    }


def build_image_gallery_metadata(now: datetime) -> dict:
    del now
    return {
        "collection": sample_enum("collection", 1, 15),
        "license": random.choice(["standard", "cc-by", "editorial"]),
        "photographer_credit": sample_enum("photographer", 1, 25),
        "source": random.choice(["upload", "agency_feed", "editorial"]),
    }


def build_event_payload(region: str, index: int, now: datetime) -> dict:
    start_time = now + timedelta(
        days=random.randint(1, 30),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )
    end_time = start_time + timedelta(hours=random.randint(1, 6))
    return {
        "title": f"{region} event {index}",
        "start_time": start_time,
        "end_time": end_time,
        "venue": sample_enum("venue", 1, 30),
        "capacity": random.randint(30, 1500),
    }


def build_event_metadata(now: datetime) -> dict:
    del now
    return {
        "organizer_type": random.choice(["community", "commercial", "institutional"]),
        "access_tier": random.choice(["free", "ticketed", "invite"]),
        "venue_region": sample_enum("region_zone", 1, 8),
        "source": random.choice(["organizer", "partner_api"]),
    }


def build_tool_payload(region: str, index: int, now: datetime) -> dict:
    del now
    return {
        "title": f"{region} tool {index}",
        "tool_type": random.choice(["calculator", "visualizer", "converter", "planner"]),
        "platform": random.choice(["web", "mobile", "desktop"]),
        "interaction_mode": random.choice(["form", "canvas", "chat"]),
        "estimated_runtime_ms": random.randint(50, 1500),
    }


def build_tool_metadata(now: datetime) -> dict:
    del now
    return {
        "provider": sample_enum("provider", 1, 15),
        "supported_audiences": random.sample(
            ["consumer", "student", "analyst", "operator"],
            k=random.randint(1, 3),
        ),
        "stability_tier": random.choice(["beta", "stable", "featured"]),
        "source": random.choice(["sdk", "custom_build", "partner_tool"]),
    }


def build_review_payload(region: str, index: int, now: datetime) -> dict:
    del now
    pros_count = random.randint(1, 3)
    cons_count = random.randint(0, 2)
    return {
        "title": f"{region} review {index}",
        "rating": round(random.uniform(2.0, 5.0), 1),
        "reviewed_item": sample_enum("item", 1, 200),
        "pros": random.sample(PROS_POOL, k=pros_count),
        "cons": random.sample(CONS_POOL, k=cons_count),
    }


def build_review_metadata(now: datetime) -> dict:
    del now
    return {
        "reviewer_kind": random.choice(["staff", "verified_user", "guest"]),
        "reviewed_category": random.choice(["video", "tool", "event", "article", "podcast"]),
        "spoiler_level": random.choice(["none", "light", "heavy"]),
        "source": random.choice(["user_submitted", "curator_pick"]),
    }


def build_discussion_payload(region: str, index: int, now: datetime) -> dict:
    del now
    return {
        "title": f"{region} discussion {index}",
        "reply_count": random.randint(0, 400),
        "participant_count": random.randint(2, 120),
        "is_pinned": sample_bool(0.10),
        "is_locked": sample_bool(0.05),
    }


def build_discussion_metadata(now: datetime) -> dict:
    del now
    return {
        "forum": random.choice(["general", "support", "strategy", "fan"]),
        "moderation_state": random.choice(["open", "reviewed", "restricted"]),
        "language": random.choice(["en", "pt"]),
        "source": random.choice(["forum", "live_chat", "partner_board"]),
    }


def build_curated_list_payload(region: str, index: int, now: datetime) -> dict:
    del now
    return {
        "title": f"{region} list {index}",
        "item_count": random.randint(3, 40),
        "curator": sample_enum("curator", 1, 20),
        "theme": random.choice(["starter_pack", "weekly_digest", "local_highlights", "expert_picks"]),
        "update_frequency": random.choice(["daily", "weekly", "monthly"]),
    }


def build_curated_list_metadata(now: datetime) -> dict:
    del now
    return {
        "curator_kind": random.choice(["editorial", "community", "algorithmic"]),
        "list_scope": random.choice(["local", "cross-region", "global"]),
        "freshness_policy": random.choice(["rolling", "manual", "seasonal"]),
        "source": random.choice(["editorial", "automated", "community"]),
    }


PAYLOAD_BUILDERS = {
    "article": build_article_payload,
    "video": build_video_payload,
    "podcast": build_podcast_payload,
    "image_gallery": build_image_gallery_payload,
    "event": build_event_payload,
    "tool": build_tool_payload,
    "review": build_review_payload,
    "discussion": build_discussion_payload,
    "curated_list": build_curated_list_payload,
}

METADATA_BUILDERS = {
    "article": build_article_metadata,
    "video": build_video_metadata,
    "podcast": build_podcast_metadata,
    "image_gallery": build_image_gallery_metadata,
    "event": build_event_metadata,
    "tool": build_tool_metadata,
    "review": build_review_metadata,
    "discussion": build_discussion_metadata,
    "curated_list": build_curated_list_metadata,
}


def build_content_type_sequence(content_items_per_region: int) -> dict[str, list[str]]:
    sequence_by_region: dict[str, list[str]] = {}
    for region in REGIONS:
        content_types: list[str] = []
        while len(content_types) < content_items_per_region:
            block = list(CONTENT_TYPES)
            random.shuffle(block)
            content_types.extend(block)
        sequence_by_region[region] = content_types[:content_items_per_region]
    return sequence_by_region


def append_status_tag(tags: list[str], tag: str) -> list[str]:
    if tag in tags:
        return tags
    if len(tags) < 3:
        return tags + [tag]

    removable = [value for value in tags if value != "premium"]
    if not removable:
        return tags

    replacement = random.choice(removable)
    updated_tags = [value for value in tags if value != replacement]
    updated_tags.append(tag)
    return updated_tags


def build_tags(content_type: str, status: str, include_premium: bool, include_archived: bool) -> list[str]:
    preferred_pool = [
        tag for tag in CONTENT_SPECS[content_type]["preferred_tags"] if tag not in STATUS_CONDITIONED_TAGS
    ]
    available_pool = [tag for tag in preferred_pool if tag != "premium"]
    base_count = random.randint(1, min(3, len(preferred_pool)))

    tags: list[str] = []
    if include_premium:
        tags.append("premium")
        base_count = max(0, base_count - 1)

    if base_count > 0 and available_pool:
        tags.extend(random.sample(available_pool, k=min(base_count, len(available_pool))))

    if status in {"trending", "hot"}:
        tags = append_status_tag(tags, "trending")
    if include_archived:
        tags = append_status_tag(tags, "archived")
    return tags


def sample_last_updated(status: str, archived: bool, now: datetime) -> datetime:
    if status == "hot":
        age_seconds = random.uniform(0, 15 * 60)
    elif status == "trending":
        age_seconds = random.uniform(15 * 60, 6 * 60 * 60)
    elif archived:
        age_seconds = random.uniform(30 * 24 * 60 * 60, 180 * 24 * 60 * 60)
    else:
        age_seconds = random.uniform(6 * 60 * 60, 7 * 24 * 60 * 60)
    return now - timedelta(seconds=age_seconds)


def select_premium_indices(blueprints: list[dict], target_count: int) -> set[int]:
    indices_by_type: dict[str, list[int]] = {}
    for blueprint_index, blueprint in enumerate(blueprints):
        content_type = blueprint["content_type"]
        if "premium" not in CONTENT_SPECS[content_type]["preferred_tags"]:
            continue
        indices_by_type.setdefault(content_type, []).append(blueprint_index)

    for indices in indices_by_type.values():
        random.shuffle(indices)

    ordered_types = list(indices_by_type.keys())
    random.shuffle(ordered_types)

    selected: list[int] = []
    selected_by_type = {content_type: 0 for content_type in ordered_types}
    progress = True
    while len(selected) < target_count and progress:
        progress = False
        for content_type in ordered_types:
            indices = indices_by_type[content_type]
            type_total = len(indices)
            if not indices:
                continue

            limit = type_total if type_total == 1 else type_total - 1
            if selected_by_type[content_type] >= limit:
                continue

            selected.append(indices.pop())
            selected_by_type[content_type] += 1
            progress = True
            if len(selected) == target_count:
                break

    if len(selected) < target_count:
        remaining = []
        for indices in indices_by_type.values():
            remaining.extend(indices)
        random.shuffle(remaining)
        selected.extend(remaining[: target_count - len(selected)])

    return set(selected)


def build_blueprints(content_items_per_region: int) -> list[dict]:
    sequence_by_region = build_content_type_sequence(content_items_per_region)
    blueprints: list[dict] = []
    for region in REGIONS:
        for index, content_type in enumerate(sequence_by_region[region], start=1):
            blueprints.append(
                {
                    "region": region,
                    "index": index,
                    "content_type": content_type,
                    "status": pick_weighted_key(STATUS_WEIGHTS),
                }
            )
    return blueprints


def make_content_item(
    region: str,
    index: int,
    content_type: str,
    status: str,
    include_premium: bool,
    include_archived: bool,
    now: datetime,
) -> dict:
    spec = CONTENT_SPECS[content_type]
    baseline = sample_metric(*spec["baseline_range"])
    multiplier = random.uniform(*ENGAGEMENT_MULTIPLIERS[status])
    payload = PAYLOAD_BUILDERS[content_type](region, index, now)
    payload["engagement"] = round(baseline * multiplier, 2)
    payload["status"] = status

    metadata = METADATA_BUILDERS[content_type](now)
    metadata["relevance_baseline"] = baseline

    return {
        "_id": f"{region}::content::{index:03d}",
        "region_origin": region,
        "content_type": content_type,
        "payload": payload,
        "metadata": metadata,
        "tags": build_tags(content_type, status, include_premium, include_archived),
        "unit": spec["unit"],
        "last_updated": sample_last_updated(status, include_archived, now),
    }


def generate_documents(content_items_per_region: int) -> dict[str, list[dict]]:
    blueprints = build_blueprints(content_items_per_region)
    quiet_indices = [index for index, blueprint in enumerate(blueprints) if blueprint["status"] == "quiet"]
    archived_count = half_up_count(len(quiet_indices), 0.10)
    archived_indices = set(random.sample(quiet_indices, k=min(archived_count, len(quiet_indices))))

    premium_count = half_up_count(len(blueprints), 0.15)
    premium_indices = select_premium_indices(blueprints, premium_count)

    now = datetime.now(timezone.utc)
    docs_by_region = {"lan1": [], "lan2": []}
    for blueprint_index, blueprint in enumerate(blueprints):
        doc = make_content_item(
            region=blueprint["region"],
            index=blueprint["index"],
            content_type=blueprint["content_type"],
            status=blueprint["status"],
            include_premium=blueprint_index in premium_indices,
            include_archived=blueprint_index in archived_indices,
            now=now,
        )
        docs_by_region[blueprint["region"]].append(doc)

    return docs_by_region


def seed(uri_lan1: str, uri_lan2: str, content_items_per_region: int):
    uris = {"lan1": uri_lan1, "lan2": uri_lan2}
    docs_by_region = generate_documents(content_items_per_region)

    for region, uri in uris.items():
        client = MongoClient(uri, timeoutMS=0, connectTimeoutMS=60000, serverSelectionTimeoutMS=60000)
        collection = client["edge_platform"]["content_items"]
        ops = [
            UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
            for doc in docs_by_region[region]
        ]
        result = collection.bulk_write(ops, ordered=False)
        print(
            f"[{region}] Upserted content items: {result.upserted_count}, "
            f"Modified: {result.modified_count}"
        )
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-lan1", default=REGIONS["lan1"], help="MongoDB URI for LAN 1 primary")
    parser.add_argument("--mongo-lan2", default=REGIONS["lan2"], help="MongoDB URI for LAN 2 primary")
    parser.add_argument("--content-items", type=int, default=50, help="Content items per region")
    parser.add_argument("--data-seed", type=int, default=None, help="Fixed seed for reproducible content generation")
    args = parser.parse_args()
    if args.data_seed is not None:
        random.seed(args.data_seed)
    seed(args.mongo_lan1, args.mongo_lan2, args.content_items)