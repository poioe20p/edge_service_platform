"""release_log.py — research_q3 release-event CSV logger.

Appends one row per release event (``begin`` / ``end`` /
``quarantine_begin`` / ``recall``) for every ``RELEASE_MECHANISM`` except
``off`` — see ``scaling_config._RELEASE_MECHANISM``. Written from both
Thread 2 (greenthread housekeeping in ``main_n*.py``) and Thread 3 (native
``ElasticityManager``), so NO locking is used here: every append opens the
file with ``O_APPEND`` and emits the whole row with a single ``os.write``,
which Linux guarantees is atomic for small writes. Never raises — a failed
append is logged at debug and dropped.
"""

from __future__ import annotations

import logging
import os
import time

try:
    from .scaling_config import _RELEASE_LOG_PATH, _RELEASE_MECHANISM
except ImportError:  # imported as a top-level module (standalone smoke tests)
    from scaling_config import _RELEASE_LOG_PATH, _RELEASE_MECHANISM

logger = logging.getLogger("os_ken.release_log")

RELEASE_LOG_HEADER = ("ts,network_id,mechanism,tier,container,mac,trigger,"
                      "event,success,reason,eviction_overlap\n")


def append_row(*, network_id: str, mechanism: str, tier: str, container: str,
               mac: str, trigger: str, event: str, success: str, reason: str,
               eviction_overlap: str = "") -> None:
    """Append one release-event row.

    No-op when ``_RELEASE_MECHANISM == "off"``. Opens the log with
    ``O_APPEND|O_CREAT|O_WRONLY`` (0o644) if missing, writes the header first
    if the file is empty, then one row and closes the fd. No ``fsync`` is
    performed (greenthread-blocking hazard): each row is emitted with a
    single ``os.write`` on an ``O_APPEND`` fd, which Linux guarantees is
    append-atomic for small writes. A same-instant double-header race (two
    writers both seeing an empty file) is benign and rare. Any error is
    logged at debug and swallowed (never raises).
    """
    if _RELEASE_MECHANISM == "off":
        return
    row = (f"{time.time():.3f},{network_id},{mechanism},{tier},{container},"
           f"{mac},{trigger},{event},{success},{reason},{eviction_overlap}\n")
    fd = -1
    try:
        fd = os.open(_RELEASE_LOG_PATH,
                     os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        if os.fstat(fd).st_size == 0:
            os.write(fd, RELEASE_LOG_HEADER.encode("utf-8"))
        os.write(fd, row.encode("utf-8"))
    except Exception:
        logger.debug("[release_log] failed to append release log row",
                     exc_info=True)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
