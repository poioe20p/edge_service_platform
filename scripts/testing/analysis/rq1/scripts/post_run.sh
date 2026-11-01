#!/bin/bash
# Post-run workflow for RQ1 experiment runs.
# Runs all analysis CLIs, verifies env snapshot, deletes controller/service logs.
#
# Usage:
#   bash post_run.sh <run_dir>
#
# Example:
#   bash post_run.sh source/scripts/testing/metrics/20260630_135024_rq1_v2_push_1
set -euo pipefail

RUN_DIR="${1:?Usage: post_run.sh <run_dir>}"
cd "$(dirname "$0")/../../../.."  # cd to repo root

echo "=== Post-run: $RUN_DIR ==="

# 1. Fix ownership (may be root-owned from sudo make)
sudo chown -R "$(whoami)" "$RUN_DIR" 2>/dev/null || true

# 2. Verify env snapshot readable
python3 -c "import os; d='$RUN_DIR/controller_env_snapshot.env'; assert os.access(d, os.R_OK), f'Cannot read {d}'; print('  env OK:', d)"

# 3. Run all analysis CLIs
echo "  timings..."
python3 -m source.scripts.testing.analysis.rq1.cli.timings --run-dir "$RUN_DIR"

echo "  overhead..."
python3 -m source.scripts.testing.analysis.rq1.cli.overhead --run-dir "$RUN_DIR"

echo "  decision_quality..."
python3 -m source.scripts.testing.analysis.rq1.cli.decision_quality --run-dir "$RUN_DIR"

echo "  cli_simple_run..."
python3 -m source.scripts.testing.analysis.cli_simple_run --run-dir "$RUN_DIR"

echo "  cli_overview..."
python3 -m source.scripts.testing.analysis.cli_overview --run-dir "$RUN_DIR"

echo "  cli_phase_summary..."
python3 -m source.scripts.testing.analysis.cli_phase_summary --run-dir "$RUN_DIR"

# 4. Delete controller logs (parsed, no longer needed)
rm -f "$RUN_DIR/controller_lan1.log" "$RUN_DIR/controller_lan2.log"

# 5. Delete service logs (large, not needed for RQ1 analysis)
rm -rf "$RUN_DIR/service_logs/"

echo "=== Post-run complete ==="
ls -la "$RUN_DIR/"
