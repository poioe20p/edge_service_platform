"""cli_recovery_validation — targeted recovery validation summary for a run.

Produces under <run_dir>/analysis/:
  - recovery_validation_summary.md
  - recovery_validation_fault_windows.csv
  - recovery_validation_request_lease_outcomes.csv

The CLI works for both explicit fault-injection runs and ordinary observation
runs. When no experiment_fault_events.csv rows exist, the summary falls back to
aggregate request-lease and controller-marker counts.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .loader import load_run
from .client_status import is_failure


REQUEST_LEASE_RE = re.compile(
    r"request lease outcome request_id=(?P<request_id>\S+) lan=(?P<lan>\S+) "
    r"lifecycle=(?P<lifecycle>\S+) outcome=(?P<outcome>\S+) epoch_id=(?P<epoch_id>\S+) "
    r"epoch_mode=(?P<epoch_mode>\S+) rebinds_used=(?P<rebinds_used>\S+) "
    r"replay_safe=(?P<replay_safe>\S+) terminal_reason=(?P<terminal_reason>\S+)"
)
AVOID_RE = re.compile(
    r"select_storage\((?P<domain>n[12])\): recovery avoiding last normal backend "
    r"mac=(?P<backend_mac>\S+) client=(?P<client_mac>\S+)"
)
FALLBACK_RE = re.compile(
    r"select_storage\((?P<domain>n[12])\): recovery fallback to full pool after avoidance would empty candidates "
    r"client=(?P<client_mac>\S+) mac=(?P<backend_mac>\S+)"
)
TIMESTAMP_PREFIX_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


@dataclass
class RequestLeaseOutcomeRow:
    timestamp: datetime
    edge_server: str
    request_id: str
    lan: str
    lifecycle: str
    outcome: str
    epoch_id: str
    epoch_mode: str
    rebinds_used: str
    replay_safe: str
    terminal_reason: str


@dataclass
class ControllerRecoveryMarker:
    timestamp: datetime
    domain: str
    kind: str
    backend_mac: str
    client_mac: str


def _parse_iso_ts(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if "." in value:
        prefix, suffix = value.split(".", 1)
        fraction, offset = suffix.split("+", 1)
        value = f"{prefix}.{fraction[:6]:0<6}+{offset}"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _parse_leading_timestamp(line: str) -> datetime | None:
    match = TIMESTAMP_PREFIX_RE.match(line)
    if not match:
        return None
    return _parse_iso_ts(match.group("ts"))


def _iter_service_log_paths(run_dir: Path) -> list[Path]:
    service_dir = run_dir / "service_logs"
    if not service_dir.exists():
        return []
    return sorted(service_dir.glob("edge_server_*.log"))


def parse_request_lease_outcomes(run_dir: Path) -> list[RequestLeaseOutcomeRow]:
    rows: list[RequestLeaseOutcomeRow] = []
    for path in _iter_service_log_paths(run_dir):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = REQUEST_LEASE_RE.search(line)
                if not match:
                    continue
                timestamp = _parse_leading_timestamp(line)
                if timestamp is None:
                    continue
                rows.append(
                    RequestLeaseOutcomeRow(
                        timestamp=timestamp,
                        edge_server=path.name,
                        request_id=match.group("request_id"),
                        lan=match.group("lan"),
                        lifecycle=match.group("lifecycle"),
                        outcome=match.group("outcome"),
                        epoch_id=match.group("epoch_id"),
                        epoch_mode=match.group("epoch_mode"),
                        rebinds_used=match.group("rebinds_used"),
                        replay_safe=match.group("replay_safe"),
                        terminal_reason=match.group("terminal_reason"),
                    )
                )
    rows.sort(key=lambda row: row.timestamp)
    return rows


def parse_controller_markers(run_dir: Path) -> list[ControllerRecoveryMarker]:
    rows: list[ControllerRecoveryMarker] = []
    for path in (run_dir / "controller_lan1.log", run_dir / "controller_lan2.log"):
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                timestamp = None
                avoid = AVOID_RE.search(line)
                if avoid:
                    timestamp = _parse_controller_ts(line)
                    if timestamp is not None:
                        rows.append(
                            ControllerRecoveryMarker(
                                timestamp=timestamp,
                                domain=avoid.group("domain"),
                                kind="avoidance",
                                backend_mac=avoid.group("backend_mac"),
                                client_mac=avoid.group("client_mac"),
                            )
                        )
                    continue
                fallback = FALLBACK_RE.search(line)
                if fallback:
                    timestamp = _parse_controller_ts(line)
                    if timestamp is not None:
                        rows.append(
                            ControllerRecoveryMarker(
                                timestamp=timestamp,
                                domain=fallback.group("domain"),
                                kind="fallback",
                                backend_mac=fallback.group("backend_mac"),
                                client_mac=fallback.group("client_mac"),
                            )
                        )
    rows.sort(key=lambda row: row.timestamp)
    return rows


def _parse_controller_ts(line: str) -> datetime | None:
    prefix = line[:23]
    try:
        parsed = datetime.strptime(prefix, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _parse_client_ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _write_request_lease_csv(path: Path, rows: list[RequestLeaseOutcomeRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "edge_server",
                "request_id",
                "lan",
                "lifecycle",
                "outcome",
                "epoch_id",
                "epoch_mode",
                "rebinds_used",
                "replay_safe",
                "terminal_reason",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp": row.timestamp.isoformat(),
                    "edge_server": row.edge_server,
                    "request_id": row.request_id,
                    "lan": row.lan,
                    "lifecycle": row.lifecycle,
                    "outcome": row.outcome,
                    "epoch_id": row.epoch_id,
                    "epoch_mode": row.epoch_mode,
                    "rebinds_used": row.rebinds_used,
                    "replay_safe": row.replay_safe,
                    "terminal_reason": row.terminal_reason,
                }
            )


def _window_filter[T](rows: list[T], *, start: datetime, end: datetime, key) -> list[T]:
    return [row for row in rows if start <= key(row) <= end]


def _summarize_fault_windows(
    run,
    outcomes: list[RequestLeaseOutcomeRow],
    markers: list[ControllerRecoveryMarker],
    *,
    window_before_s: float,
    window_after_s: float,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for fault in run.fault_event_rows:
        if fault.get("status") != "executed":
            continue
        fault_ts = _parse_iso_ts(str(fault["timestamp"]))
        start = fault_ts - timedelta(seconds=window_before_s)
        end = fault_ts + timedelta(seconds=window_after_s)
        window_client_rows = _window_filter(
            run.all_client_rows,
            start=start,
            end=end,
            key=lambda row: _parse_client_ts(str(row["timestamp"])),
        )
        window_outcomes = _window_filter(
            outcomes,
            start=start,
            end=end,
            key=lambda row: row.timestamp,
        )
        window_markers = _window_filter(
            markers,
            start=start,
            end=end,
            key=lambda row: row.timestamp,
        )
        outcome_counts = Counter(row.outcome for row in window_outcomes)
        failure_count = sum(1 for row in window_client_rows if is_failure(row))
        request_count = len(window_client_rows)
        summaries.append(
            {
                "fault_timestamp": fault_ts.isoformat(),
                "action_name": fault.get("action_name", ""),
                "phase": fault.get("phase", ""),
                "domain": fault.get("domain", ""),
                "backend_ip": fault.get("backend_ip", ""),
                "container_name": fault.get("container_name", ""),
                "request_count": request_count,
                "failure_count": failure_count,
                "failure_rate_pct": (100.0 * failure_count / request_count) if request_count else 0.0,
                "success_normal_count": outcome_counts.get("success_normal", 0),
                "success_after_rebind_count": outcome_counts.get("success_after_rebind", 0),
                "failure_terminal_count": outcome_counts.get("failure_terminal", 0),
                "avoidance_count": sum(1 for row in window_markers if row.kind == "avoidance"),
                "fallback_count": sum(1 for row in window_markers if row.kind == "fallback"),
            }
        )
    return summaries


def _write_fault_windows_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "fault_timestamp",
            "action_name",
            "phase",
            "domain",
            "backend_ip",
            "container_name",
            "request_count",
            "failure_count",
            "failure_rate_pct",
            "success_normal_count",
            "success_after_rebind_count",
            "failure_terminal_count",
            "avoidance_count",
            "fallback_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_summary(
    path: Path,
    *,
    outcomes: list[RequestLeaseOutcomeRow],
    markers: list[ControllerRecoveryMarker],
    fault_window_rows: list[dict[str, object]],
    fault_event_rows: list[dict],
) -> None:
    overall_outcomes = Counter((row.lan, row.outcome) for row in outcomes)
    per_lan: dict[str, Counter[str]] = defaultdict(Counter)
    for lan, outcome in overall_outcomes:
        per_lan[lan][outcome] = overall_outcomes[(lan, outcome)]

    overall_markers = Counter(row.kind for row in markers)

    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Recovery Validation Summary\n\n")
        if fault_event_rows:
            handle.write("## Fault Events\n\n")
            executed = [row for row in fault_event_rows if row.get("status") == "executed"]
            handle.write(f"- Total fault rows: {len(fault_event_rows)}\n")
            handle.write(f"- Executed fault rows: {len(executed)}\n")
            if not executed:
                handle.write("- No executed fault rows were recorded.\n")
            handle.write("\n")
        else:
            handle.write("## Fault Events\n\n")
            handle.write("- No explicit fault events were recorded for this run.\n\n")

        handle.write("## Request Lease Outcomes\n\n")
        if not outcomes:
            handle.write("- No request lease outcome rows were found in service logs.\n\n")
        else:
            for lan in sorted(per_lan):
                counts = per_lan[lan]
                handle.write(
                    f"- {lan}: success_normal={counts.get('success_normal', 0)}, "
                    f"success_after_rebind={counts.get('success_after_rebind', 0)}, "
                    f"failure_terminal={counts.get('failure_terminal', 0)}\n"
                )
            handle.write("\n")

        handle.write("## Controller Recovery Markers\n\n")
        handle.write(
            f"- avoidance markers: {overall_markers.get('avoidance', 0)}\n"
        )
        handle.write(
            f"- fallback markers: {overall_markers.get('fallback', 0)}\n\n"
        )

        handle.write("## Fault Windows\n\n")
        if not fault_window_rows:
            handle.write("- No executed fault windows to summarize.\n")
        else:
            for row in fault_window_rows:
                handle.write(
                    f"- {row['action_name']} ({row['phase']}, {row['domain']}): "
                    f"failure_rate_pct={float(row['failure_rate_pct']):.2f}, "
                    f"success_after_rebind={row['success_after_rebind_count']}, "
                    f"failure_terminal={row['failure_terminal_count']}, "
                    f"avoidance={row['avoidance_count']}, fallback={row['fallback_count']}\n"
                )


def run(run_dir: Path, *, window_before_s: float, window_after_s: float) -> None:
    run_data = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    outcome_rows = parse_request_lease_outcomes(run_dir)
    controller_markers = parse_controller_markers(run_dir)
    fault_windows = _summarize_fault_windows(
        run_data,
        outcome_rows,
        controller_markers,
        window_before_s=window_before_s,
        window_after_s=window_after_s,
    )

    lease_csv = out_dir / "recovery_validation_request_lease_outcomes.csv"
    window_csv = out_dir / "recovery_validation_fault_windows.csv"
    summary_md = out_dir / "recovery_validation_summary.md"

    _write_request_lease_csv(lease_csv, outcome_rows)
    _write_fault_windows_csv(window_csv, fault_windows)
    _write_summary(
        summary_md,
        outcomes=outcome_rows,
        markers=controller_markers,
        fault_window_rows=fault_windows,
        fault_event_rows=run_data.fault_event_rows,
    )

    print(f"[cli_recovery_validation] wrote {summary_md}")
    print(f"[cli_recovery_validation] wrote {window_csv}")
    print(f"[cli_recovery_validation] wrote {lease_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    parser.add_argument("--window-before-s", type=float, default=15.0, metavar="SECONDS")
    parser.add_argument("--window-after-s", type=float, default=60.0, metavar="SECONDS")
    args = parser.parse_args()
    run(
        Path(args.run_dir),
        window_before_s=args.window_before_s,
        window_after_s=args.window_after_s,
    )


if __name__ == "__main__":
    main()