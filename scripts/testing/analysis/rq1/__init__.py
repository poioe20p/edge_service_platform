# RQ1 analysis tools — telemetry delivery cadence evaluation.
#
# Analysis CLIs (run on individual experiment folders):
#   cli/timings.py            — reaction latency & staleness
#   cli/overhead.py           — controller CPU/RAM overhead
#   cli/overhead_compare.py   — cross-run overhead comparison
#   cli/decision_quality.py   — scaling outcome per phase
#
# Shared libraries:
#   lib/breach_detector.py    — breach-detection logic (shared)
#
# Campaign tooling (run across multiple experiment folders):
#   scripts/collect_metrics.py           — extract per-run metrics to table/CSV
#   scripts/generate_comparison_graphs.py — mode-comparison bar charts
#   scripts/post_run.sh                  — post-run workflow (CLIs + cleanup)
#
# Debug/verification:
#   debug/                    — one-off debug and fix scripts
#
# Findings (historical):
#   findings/eval_v1_findings.md
#   findings/eval_v2_findings.md
#   findings/eval_v2_replicates_findings.md
