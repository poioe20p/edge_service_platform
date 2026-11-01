"""cli_tdb_drivers — OLS regression of T_db_write on storage_count and cross_region_ratio.

Tests the hypothesis: T_db_write ~ a + b·storage_count + c·cross_region_ratio.
If b > 0 with a meaningful R², adding storage nodes makes writes slower.

Uses no external libraries — Gaussian elimination is implemented inline.

Usage:
    python -m source.scripts.testing.analysis.cli_tdb_drivers --run-dir <dir>
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

# Fallback cross_region_ratio per phase when phases_snapshot.json is missing.
# Mirror the values from testing_workloads.md.
_FALLBACK_CROSS_REGION: dict[str, float] = {
    "baseline":             0.0,
    "local_light":          0.0,
    "local_moderate":       0.0,
    "local_heavy":          0.0,
    "cross_region_moderate": 0.5,
    "cross_region_hotspot":  0.8,
    "demand_drop":          0.0,
}


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Minimal Gaussian elimination (no numpy)
# ---------------------------------------------------------------------------

def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve Ax = b by Gaussian elimination with partial pivoting."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[pivot] = M[pivot], M[col]
        if abs(M[col][col]) < 1e-12:
            continue
        for row in range(col + 1, n):
            factor = M[row][col] / M[col][col]
            for j in range(col, n + 1):
                M[row][j] -= factor * M[col][j]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        if abs(M[i][i]) < 1e-12:
            x[i] = 0.0
        else:
            x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return x


def ols(X: list[list[float]], y: list[float]) -> tuple[list[float], float]:
    """Closed-form OLS via normal equations. X must include a constant-1 column.

    Returns (coefficients, R²).
    """
    n, k = len(X), len(X[0])
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    Xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    beta = _solve(XtX, Xty)
    y_hat = [sum(beta[a] * X[i][a] for a in range(k)) for i in range(n)]
    ss_res = sum((y[i] - y_hat[i]) ** 2 for i in range(n))
    ybar = sum(y) / n
    ss_tot = sum((yi - ybar) ** 2 for yi in y) or 1.0
    r2 = 1.0 - ss_res / ss_tot
    return beta, r2


# ---------------------------------------------------------------------------
# Model fitting
# ---------------------------------------------------------------------------

def fit_tdb_write_model(run) -> dict | None:
    """Fit T_db_write ~ a + b·storage_count + c·cross_region_ratio.

    Returns None with a warning when the avg_time_db_write_ms column is absent.
    """
    # Prefer debug_rows for decomposed DB fields when domain_rows doesn't have them
    db_rows = run.domain_rows
    has_write_in_domain = any(
        row.get("avg_time_db_write_ms") not in ("", None)
        for row in run.domain_rows
    )
    if not has_write_in_domain and run.debug_rows:
        db_rows = run.debug_rows

    has_write = any(
        row.get("avg_time_db_write_ms") not in ("", None)
        for row in db_rows
    )
    if not has_write:
        warnings.warn(
            "avg_time_db_write_ms column is missing or all-empty — "
            "cli_tdb_drivers requires the telemetry decomposition to be deployed. "
            "Skipping regression."
        )
        return None

    # Build phase → cross_region_ratio lookup
    if run.phases:
        cr_by_phase = {p.name: p.cross_region_ratio for p in run.phases}
    else:
        warnings.warn(
            "phases_snapshot.json not found — falling back to hard-coded "
            "cross_region_ratio values from testing_workloads.md."
        )
        cr_by_phase = _FALLBACK_CROSS_REGION

    X: list[list[float]] = []
    y: list[float] = []
    for r in db_rows:
        tw = r.get("avg_time_db_write_ms")
        if tw in ("", None):
            continue
        phase = r.get("phase", "")
        cr = cr_by_phase.get(phase, 0.0)
        sc = _safe_float(r.get("storage_count", 0))
        X.append([1.0, sc, cr])
        y.append(_safe_float(tw))

    if len(y) < 4:
        warnings.warn(f"Only {len(y)} data points — regression unreliable.")
        return None

    beta, r2 = ols(X, y)
    return {
        "intercept": beta[0],
        "b_storage_count": beta[1],
        "b_cross_region": beta[2],
        "r2": r2,
        "n": len(y),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(run_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[cli_tdb_drivers] matplotlib not installed — install via analysis/requirements.txt")
        return

    from .loader import load_run
    from .phase_window import phase_boundaries
    from .plots import shade_phases

    r = load_run(run_dir)
    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = fit_tdb_write_model(r)

    if result is None:
        _append_summary(out_dir / "summary.md",
                        "T_db Drivers: avg_time_db_write_ms missing — skipped.\n")
        return

    intercept = result["intercept"]
    b_sc = result["b_storage_count"]
    b_cr = result["b_cross_region"]
    r2 = result["r2"]
    n = result["n"]

    print(f"\nT_db_write \u2248 {intercept:.1f} "
          f"+ {b_sc:.1f}\u00b7storage_count "
          f"+ {b_cr:.1f}\u00b7cross_region_ratio  "
          f"(R\u00b2={r2:.3f}, n={n})")

    if b_sc > 0 and r2 > 0.3:
        print("  \u26a0\ufe0f  b_storage_count > 0: adding storage nodes correlates with HIGHER T_db_write.")
    elif b_sc <= 0:
        print("  \u2713  b_storage_count <= 0: storage scale-up does not worsen write latency.")

    # Prefer debug_rows for DB decomposition fields
    db_rows = r.debug_rows if r.debug_rows and not any(
        row.get("avg_time_db_write_ms") not in ("", None) for row in r.domain_rows
    ) else r.domain_rows
    # db_rows and domain_rows correspond to the same windows — use both as needed
    _db = db_rows  # alias for decomposed fields
    _dom = r.domain_rows  # alias for domain-level fields

    # ── Scatter: T_db_write vs storage_count ────────────────────────────────
    t_domain = [_safe_float(row.get("window_end", 0)) - r.t0 for row in _dom]
    bounds = phase_boundaries(r.t0, r.phases)

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle(f"T_db drivers — {run_dir.name}", fontsize=12)

    # Scatter: T_db_write vs storage_count
    ax = axes[0]
    phases_col = {row.get("phase", "") for row in _dom}
    colours = ["#1a7abf", "#bf5a1a", "#1abf4a", "#bf1a8c", "#8c1abf", "#4abf1a", "#1a4abf", "#bf1a1a"]
    colour_map = {ph: colours[i % len(colours)] for i, ph in enumerate(sorted(phases_col))}
    for i, row in enumerate(_dom):
        ph = row.get("phase", "")
        db_row = _db[i] if i < len(_db) else {}
        ax.scatter(
            _safe_float(row.get("storage_count", 0)),
            _safe_float(db_row.get("avg_time_db_write_ms", 0)),
            color=colour_map.get(ph, "#888888"), s=8, alpha=0.6,
        )
    # Regression line
    sc_range = sorted({_safe_float(row.get("storage_count", 0)) for row in _dom})
    if sc_range:
        reg_x = [sc_range[0], sc_range[-1]]
        reg_y = [intercept + b_sc * x for x in reg_x]
        ax.plot(reg_x, reg_y, color="black", linewidth=1.2,
                label=f"slope={b_sc:.1f}, R²={r2:.2f}")
    ax.set_xlabel("storage_count")
    ax.set_ylabel("avg_time_db_write_ms")
    ax.set_title("T_db_write vs storage_count")
    ax.legend(fontsize=8)

    # Time series: T_db_write over time
    ax = axes[1]
    tw_series = [_safe_float(row.get("avg_time_db_write_ms", 0)) for row in _db]
    tr_series = [_safe_float(row.get("avg_time_db_read_ms", 0)) for row in _db]
    ax.plot(t_domain, tw_series, color="#bf5a1a", label="T_db_write")
    ax.plot(t_domain, tr_series, color="#1a7abf", label="T_db_read")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("ms")
    ax.set_title("T_db decomposition over time")
    ax.legend(fontsize=7)
    shade_phases(ax, bounds, r.t0)

    plt.tight_layout()
    out_path = out_dir / "tdb_drivers.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[cli_tdb_drivers] wrote {out_path}")

    _append_summary(out_dir / "summary.md", result)


def _append_summary(summary_path: Path, result) -> None:
    with summary_path.open("a", encoding="utf-8") as f:
        f.write("\n## T_db Drivers\n\n")
        if isinstance(result, str):
            f.write(result)
            return
        f.write(
            f"OLS regression: "
            f"`T_db_write ~ {result['intercept']:.1f} "
            f"+ {result['b_storage_count']:.1f}·storage_count "
            f"+ {result['b_cross_region']:.1f}·cross_region_ratio  "
            f"(R²={result['r2']:.3f}, n={result['n']})`\n\n"
        )
        if result["b_storage_count"] > 0 and result["r2"] > 0.3:
            f.write(
                "**b_storage_count > 0**: adding storage nodes correlates with "
                "higher write latency. See `analysis/tdb_drivers.png`.\n"
            )
        else:
            f.write("b_storage_count ≤ 0 — storage scale-up does not worsen write latency.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, metavar="DIR")
    args = parser.parse_args()
    run(Path(args.run_dir))


if __name__ == "__main__":
    main()
