#!/usr/bin/env python3
"""metrics_stats.py

Computes descriptive statistics from experiment metric CSV (or Excel) files
produced by traffic_generator.py and resource monitoring.

Usage:
    python source/scripts/tools/metrics_stats.py path/to/run_folder/
    python source/scripts/tools/metrics_stats.py path/to/run_folder/ --by-phase --by-endpoint --by-lan
    python source/scripts/tools/metrics_stats.py -r path/to/resource_stats.csv
    python source/scripts/tools/metrics_stats.py -r path/to/resource_stats.csv --by-network

Default mode processes client_requests.csv in the given run folder.
With -r, processes a single resource_stats CSV file.
"""

import argparse
import csv
import statistics
import sys
from pathlib import Path



def percentile(sorted_data: list[float], p: float) -> float:
    """Linear interpolation percentile (same as numpy/Excel default)."""
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    rank = p / 100 * (n - 1)
    lo = int(rank)
    hi = lo + 1
    frac = rank - lo
    if hi >= n:
        return sorted_data[-1]
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def compute_stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {}
    s = sorted(values)
    return {
        "count":  n,
        "mean":   statistics.mean(s),
        "median": statistics.median(s),
        "std":    statistics.pstdev(s) if n > 1 else 0.0,
        "min":    s[0],
        "p25":    percentile(s, 25),
        "p75":    percentile(s, 75),
        "p90":    percentile(s, 90),
        "p95":    percentile(s, 95),
        "p99":    percentile(s, 99),
        "max":    s[-1],
    }


def print_stats(label: str, stats: dict, fmt_val) -> None:
    if not stats:
        print(f"  {label}: no data\n")
        return
    pad = 8
    print(f"  {label}")
    print(f"    {'count':<{pad}} {stats['count']}")
    for key in ("mean", "median", "std", "min", "p25", "p75", "p90", "p95", "p99", "max"):
        print(f"    {key:<{pad}} {fmt_val(stats[key])}")
    print()



def read_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def read_excel(path: Path) -> list[dict]:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        sys.exit(
            "openpyxl is required to read Excel files.\n"
            "Install it with:  pip install openpyxl"
        )
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h) if h is not None else "" for h in next(rows_iter)]
    rows = []
    for row in rows_iter:
        rows.append(dict(zip(headers, row)))
    wb.close()
    return rows


def load_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        return read_excel(path)
    return read_csv(path)



def detect_mode(rows: list[dict]) -> str:
    if not rows:
        sys.exit("File is empty.")
    headers = set(rows[0].keys())
    if "latency_s" in headers:
        return "latency"
    if "median_cpu_percent" in headers or "average_cpu_percent" in headers:
        return "resource"
    sys.exit(
        "Unrecognized file format.\n"
        "Expected a 'latency_s' column (client_requests) "
        "or a 'median_cpu_percent'/'average_cpu_percent' column (resource_stats)."
    )



def extract_col(rows: list[dict], col: str) -> list[float]:
    values = []
    for row in rows:
        raw = row.get(col, "")
        if raw is None or str(raw).strip() == "":
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def completed_rows(rows: list[dict]) -> list[dict]:
    """Filter client rows to completed ones (status-aware).

    Legacy 13-column CSVs (no ``status`` column) treat every row as
    completed, so legacy runs are unaffected. Open-loop rows with
    ``status`` = timeout/dropped/canceled are excluded — their ``latency_s``
    is either censored (timeout) or empty, and must never enter latency
    percentiles (RQ2 v2 plan §2.8).
    """
    if rows and "status" not in rows[0]:
        return rows
    return [r for r in rows
            if str(r.get("status", "") or "").strip().lower() == "completed"]


def group_by_key(rows: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        g = str(row.get(key, "unknown") or "unknown")
        groups.setdefault(g, []).append(row)
    return groups



STAT_KEYS = ["count", "mean", "median", "std", "min", "p25", "p75", "p90", "p95", "p99", "max"]


def detect_run_name(file_path: Path) -> str:
    """Extract the experiment run folder name (e.g. 20260406_225310) from the file path."""
    return file_path.parent.name or "unknown"


def append_summary_row(csv_path: Path, row_dict: dict, fieldnames: list[str]) -> None:
    """Append a single row to a summary CSV, creating the file with headers if needed."""
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_dict)



LATENCY_COL = "latency_s"


def fmt_latency(v: float) -> str:
    return f"{v:.4f} s  ({v * 1000:.2f} ms)"

LATENCY_SUMMARY_FIELDS = ["run_name", "scenario", "phase"] + STAT_KEYS


def _append_latency_summary(stats: dict, run_name: str, scenario: str, phase: str, summary_csv: Path) -> None:
    if not stats:
        return
    row = {"run_name": run_name, "scenario": scenario, "phase": phase}
    row.update(stats)
    append_summary_row(summary_csv, row, LATENCY_SUMMARY_FIELDS)


def run_latency(rows: list[dict], args, file_path: Path) -> None:
    completed = completed_rows(rows)
    scenario = "aggregate"
    overall_stats = compute_stats(extract_col(completed, LATENCY_COL))
    print_stats("OVERALL", overall_stats, fmt_latency)

    run_name = detect_run_name(file_path)
    summary_csv = file_path.parent / "latency_summary.csv"

    # Per-phase stats (always computed for summary CSV)
    phase_groups = sorted(group_by_key(rows, "phase").items())
    for phase, group in phase_groups:
        phase_stats = compute_stats(extract_col(completed_rows(group), LATENCY_COL))
        if args.by_phase:
            print_stats(f"phase: {phase}", phase_stats, fmt_latency)
        _append_latency_summary(phase_stats, run_name, scenario, phase, summary_csv)

    _append_latency_summary(overall_stats, run_name, scenario, "OVERALL", summary_csv)
    print(f"  [summary] Appended to {summary_csv}")

    if args.by_lan:
        for lan, group in sorted(group_by_key(rows, "client_lan").items()):
            print_stats(f"lan: {lan}", compute_stats(extract_col(completed_rows(group), LATENCY_COL)), fmt_latency)

    if args.by_endpoint:
        for ep, group in sorted(group_by_key(rows, "endpoint").items()):
            print_stats(f"endpoint: {ep}", compute_stats(extract_col(completed_rows(group), LATENCY_COL)), fmt_latency)



RESOURCE_COLS: list[tuple[str, str]] = [
    ("median_cpu_percent",         "%"),
    ("median_ram_used_mb",         "MB"),
    ("median_storage_cpu_percent", "%"),
    ("median_storage_ram_used_mb", "MB"),
    ("median_time_proc_ms",        "ms"),
    ("median_time_db_ms",          "ms"),
    ("median_time_total_ms",       "ms"),
    ("server_count",               ""),
    ("storage_count",              ""),
]


def print_resource_section(label: str, subset: list[dict]) -> None:
    print(f"  --- {label} ---\n")
    for col, unit in RESOURCE_COLS:
        vals = extract_col(subset, col)
        print_stats(col, compute_stats(vals), lambda v, u=unit: f"{v:.2f} {u}")


RESOURCE_SUMMARY_FIELDS = ["run_name", "phase", "metric"] + STAT_KEYS


def _append_resource_summary(subset: list[dict], run_name: str, phase: str, summary_csv: Path) -> None:
    for col, _unit in RESOURCE_COLS:
        stats = compute_stats(extract_col(subset, col))
        if not stats:
            continue
        row = {"run_name": run_name, "phase": phase, "metric": col}
        row.update(stats)
        append_summary_row(summary_csv, row, RESOURCE_SUMMARY_FIELDS)


def run_resource(rows: list[dict], args, file_path: Path) -> None:
    print_resource_section("OVERALL", rows)

    run_name = detect_run_name(file_path)
    summary_csv = file_path.parent / "resource_summary.csv"

    _append_resource_summary(rows, run_name, "OVERALL", summary_csv)

    has_phase = "phase" in (rows[0] if rows else {})
    if has_phase:
        phase_groups = sorted(group_by_key(rows, "phase").items())
        for phase, group in phase_groups:
            if args.by_phase:
                print_resource_section(f"phase: {phase}", group)
            _append_resource_summary(group, run_name, phase, summary_csv)

    if args.by_network:
        for net, group in sorted(group_by_key(rows, "network_id").items()):
            print_resource_section(f"network: {net}", group)

    print(f"  [summary] Appended to {summary_csv}")



def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compute statistics from experiment metric files.\n"
            "Default: processes client_requests.csv in a run folder.\n"
            "With -r: processes a single resource_stats CSV file."
        )
    )
    parser.add_argument(
        "path",
        help="Run folder (default) or resource_stats CSV file (with -r)",
    )
    parser.add_argument(
        "-r", "--resource",
        action="store_true",
        help="Treat path as a resource_stats CSV file",
    )
    parser.add_argument("--by-phase",    action="store_true", help="Break down per phase")
    parser.add_argument("--by-endpoint", action="store_true", help="[client_requests] Break down per endpoint")
    parser.add_argument("--by-lan",      action="store_true", help="[client_requests] Break down per client LAN")
    parser.add_argument("--by-network",  action="store_true", help="[resource_stats] Break down per network_id")
    args = parser.parse_args()

    path = Path(args.path)

    if args.resource:
        if not path.exists():
            sys.exit(f"File not found: {path}")
        rows = load_rows(path)
        print(f"=== {path.name} ({len(rows)} rows) ===\n")
        mode = detect_mode(rows)
        if mode != "resource":
            sys.exit(f"Expected resource_stats format (median_cpu_percent column) in {path}")
        run_resource(rows, args, path)
    else:
        if not path.is_dir():
            sys.exit(f"Expected a run folder, got: {path}")
        csv_file = path / "client_requests.csv"
        if not csv_file.exists():
            sys.exit(f"No client_requests.csv file found in {path}")
        rows = load_rows(csv_file)
        print(f"=== {csv_file.name} ({len(rows)} rows) ===\n")
        run_latency(rows, args, csv_file)


if __name__ == "__main__":
    main()
