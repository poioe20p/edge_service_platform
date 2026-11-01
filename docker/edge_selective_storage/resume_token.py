"""Per-collection resume-token persistence for the selective-sync forwarder.

Tokens live under ``TOKEN_DIR / f"{coll}.token"`` and are written atomically
(tmp + rename) so a crash mid-write cannot leave the primary file truncated.
"""

from __future__ import annotations

import json
import logging
import time

from config import TOKEN_DIR

logger = logging.getLogger("selective_sync.resume_token")


def load_resume_token(coll: str) -> dict | None:
    """Return the last persisted resume token for ``coll``, or None.

    Missing file, unreadable file, or corrupt JSON all map to None — the
    worker then opens its Change Stream at "now" and relies on ``_seed``
    to cover whatever it missed.
    """
    p = TOKEN_DIR / f"{coll}.token"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        logger.warning("Corrupt resume token for %s — starting at 'now'", coll)
        return None


def save_resume_token(coll: str, token: dict) -> None:
    """Atomically persist ``token`` for ``coll`` via tmp+rename."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_DIR / f"{coll}.token.tmp"
    tmp.write_text(json.dumps(token))
    tmp.replace(TOKEN_DIR / f"{coll}.token")


def token_file_age_s(coll: str) -> float:
    """Wall-clock age of the persisted resume token, 0.0 if absent."""
    p = TOKEN_DIR / f"{coll}.token"
    try:
        return max(0.0, time.time() - p.stat().st_mtime)
    except OSError:
        return 0.0
