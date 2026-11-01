"""Status-aware helpers for ``client_requests.csv`` rows.

Row-value contract for the open-loop driver (14 columns, ``status`` last):
    completed  -> normal row; ``http_status`` is a real HTTP status and
                  ``latency_s`` is set.
    timeout    -> ``http_status="000"``, ``latency_s`` = elapsed to the cap.
    dropped    -> ``http_status=""``, ``latency_s=""``.
    canceled   -> ``http_status=""``, ``latency_s=""``.

Legacy ``sync`` mode writes 13 columns (no ``status``); every row is treated
as ``completed``.

Classification rules (RQ2 v2 rework plan §2.8 / Phase 1.5):
  - failure  = completed AND ``http_status`` not in ("200", "")
  - timeout  = ``status == "timeout"``  (never an HTTP failure)
  - offered  = every row
  - latency  = completed rows only — a ``timeout`` row carries ``latency_s``
    = elapsed-to-cap, so it must never enter latency percentiles (that is the
    cap artifact this module prevents).
"""
from __future__ import annotations

from typing import Iterable, Optional

COMPLETED = "completed"
TIMEOUT = "timeout"
DROPPED = "dropped"
CANCELED = "canceled"

#: ``http_status`` values on a completed row that are NOT an HTTP failure.
_OK_STATUSES = frozenset({"200", ""})

#: ``http_status`` values written by the legacy sync driver on curl
#: timeout/connection failure (curl reports 000; int("000") == 0).
_LEGACY_TIMEOUT_STATUSES = frozenset({"0", "000"})


def _has_status_column(row: dict, header: Optional[Iterable[str]]) -> bool:
    if header is not None:
        return "status" in set(header)
    return "status" in row


def row_status(row: dict, header: Optional[Iterable[str]] = None) -> str:
    """Return the outcome class of *row*: completed|timeout|dropped|canceled.

    *header* is the CSV fieldnames (used to detect a missing ``status``
    column); when omitted, the presence of the ``status`` key in *row* is
    used. Legacy 13-column CSVs (no ``status`` column) always classify as
    ``completed``.
    """
    if not _has_status_column(row, header):
        return COMPLETED
    status = str(row.get("status", "") or "").strip().lower()
    return status if status else COMPLETED


def is_completed(row: dict, header: Optional[Iterable[str]] = None) -> bool:
    return row_status(row, header) == COMPLETED


def is_timeout(row: dict, header: Optional[Iterable[str]] = None) -> bool:
    return row_status(row, header) == TIMEOUT


def is_dropped(row: dict, header: Optional[Iterable[str]] = None) -> bool:
    return row_status(row, header) == DROPPED


def is_canceled(row: dict, header: Optional[Iterable[str]] = None) -> bool:
    return row_status(row, header) == CANCELED


def is_failure(row: dict, header: Optional[Iterable[str]] = None) -> bool:
    """True only for *completed* rows whose ``http_status`` is not 200/blank.

    ``timeout``/``dropped``/``canceled`` rows are never failures. Legacy
    13-column CSVs (no ``status`` column) are all ``completed``, so a legacy
    ``http_status="0"`` (curl timeout/connection failure) is still a failure —
    preserving the pre-open-loop behavior.
    """
    if not is_completed(row, header):
        return False
    return str(row.get("http_status", "") or "").strip() not in _OK_STATUSES


def is_timeout_legacy(row: dict, header: Optional[Iterable[str]] = None) -> bool:
    """Open-loop timeout (``status="timeout"``) or legacy sync-mode timeout.

    Legacy 13-column CSVs identified timeouts/connection failures by
    ``http_status == "0"``; this helper keeps that behavior for legacy rows
    while trusting the ``status`` column when it is present.
    """
    if _has_status_column(row, header):
        return row_status(row, header) == TIMEOUT
    return str(row.get("http_status", "") or "").strip() in _LEGACY_TIMEOUT_STATUSES


def completed_rows(rows, header: Optional[Iterable[str]] = None) -> list[dict]:
    return [r for r in rows if is_completed(r, header)]


def offered_count(rows, header: Optional[Iterable[str]] = None) -> int:
    return len(rows)


def completed_count(rows, header: Optional[Iterable[str]] = None) -> int:
    return sum(1 for r in rows if is_completed(r, header))


def timeout_count(rows, header: Optional[Iterable[str]] = None) -> int:
    return sum(1 for r in rows if is_timeout(r, header))


def failure_count(rows, header: Optional[Iterable[str]] = None) -> int:
    return sum(1 for r in rows if is_failure(r, header))


def latency_values(rows, header: Optional[Iterable[str]] = None) -> list[float]:
    """Latency (s) of completed rows; empty/unparseable values are skipped.

    Timeout/dropped/canceled rows never enter latency percentiles — a
    ``timeout`` row's ``latency_s`` is the elapsed-to-cap and would otherwise
    reintroduce the cap artifact.
    """
    values: list[float] = []
    for row in rows:
        if not is_completed(row, header):
            continue
        raw = row.get("latency_s", "")
        if raw is None or str(raw).strip() == "":
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values
