"""
Statistical analysis of the memory-allocation benchmark results.

Answers the research questions the raw benchmark CSVs leave open:

  RQ(i)  Is the memory -> cold-start relationship linear for Python?
         -> linear regression (slope, R^2, p-value) of cold-start vs memory.
  #5     Statistical significance across memory tiers.
         -> one-way ANOVA + Kruskal-Wallis on the raw warm-latency samples.
  RQ(iv) Pareto-optimal configuration (minimise latency AND cost).
         -> Pareto frontier over (warm avg latency, cost per 1k).

Inputs:
  --agg   aggregated CSV written by benchmark.py (one row per memory tier)
  --raw   optional raw per-invocation CSV (benchmark.py --raw-out) enabling
          the ANOVA / Kruskal-Wallis tests across tiers.

Usage:
  python analyze.py --agg health_results.csv --raw raw_health.csv --label Health
"""

import argparse
import csv

import numpy as np
from scipy import stats


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_agg(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({k: _num(v) for k, v in r.items()})
    rows = [r for r in rows if r.get("memory_mb") is not None]
    rows.sort(key=lambda r: r["memory_mb"])
    return rows


def load_raw(path):
    """Return {memory_mb: [warm billed_ms samples]}."""
    groups = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("phase") or "").strip() != "warm":
                continue
            mem = _num(r.get("memory_mb"))
            val = _num(r.get("billed_ms"))
            if mem is None or val is None:
                continue
            groups.setdefault(int(mem), []).append(val)
    return dict(sorted(groups.items()))


def linearity(rows, ycol, label):
    x = np.array([r["memory_mb"] for r in rows if r.get(ycol) is not None])
    y = np.array([r[ycol] for r in rows if r.get(ycol) is not None])
    if len(x) < 3:
        print(f"  [{label}] not enough points for regression")
        return
    lr = stats.linregress(x, y)
    r2 = lr.rvalue ** 2
    verdict = "LINEAR" if r2 >= 0.9 else ("roughly linear" if r2 >= 0.7 else "NON-LINEAR")
    print(f"  [{label}] slope={lr.slope:.4f} ms/MB  intercept={lr.intercept:.1f}  "
          f"R^2={r2:.3f}  p={lr.pvalue:.2e}  -> {verdict}")


def significance(groups):
    labels = list(groups.keys())
    samples = [groups[m] for m in labels if len(groups[m]) >= 2]
    if len(samples) < 2:
        print("  not enough per-tier samples for ANOVA/Kruskal-Wallis "
              "(re-run benchmark.py with --raw-out and more --warm-requests/--runs)")
        return
    f_stat, p_anova = stats.f_oneway(*samples)
    h_stat, p_kw = stats.kruskal(*samples)
    print(f"  one-way ANOVA:      F={f_stat:.2f}  p={p_anova:.2e}  "
          f"-> {'significant' if p_anova < 0.05 else 'not significant'} difference across tiers")
    print(f"  Kruskal-Wallis:     H={h_stat:.2f}  p={p_kw:.2e}  "
          f"-> {'significant' if p_kw < 0.05 else 'not significant'} (non-parametric)")


def pareto(rows):
    pts = [r for r in rows
           if r.get("warm_billed_avg_ms") is not None and r.get("est_cost_per_1k_usd") is not None]
    frontier = []
    for r in pts:
        dominated = any(
            o is not r
            and o["warm_billed_avg_ms"] <= r["warm_billed_avg_ms"]
            and o["est_cost_per_1k_usd"] <= r["est_cost_per_1k_usd"]
            and (o["warm_billed_avg_ms"] < r["warm_billed_avg_ms"]
                 or o["est_cost_per_1k_usd"] < r["est_cost_per_1k_usd"])
            for o in pts
        )
        if not dominated:
            frontier.append(r)
    frontier.sort(key=lambda r: r["warm_billed_avg_ms"])
    for r in frontier:
        print(f"  {int(r['memory_mb']):>5} MB   "
              f"warm avg {r['warm_billed_avg_ms']:.1f} ms   "
              f"cost/1k ${r['est_cost_per_1k_usd']:.5f}")
    return frontier


def main():
    ap = argparse.ArgumentParser(description="Statistical analysis of benchmark CSVs")
    ap.add_argument("--agg", required=True, help="Aggregated results CSV (benchmark.py --out)")
    ap.add_argument("--raw", default=None, help="Raw per-invocation CSV (benchmark.py --raw-out)")
    ap.add_argument("--label", default="endpoint", help="Label for this endpoint in the report")
    args = ap.parse_args()

    rows = load_agg(args.agg)
    print(f"\n=== Statistical analysis: {args.label}  ({len(rows)} memory tiers) ===")

    print("\nRQ(i) Linearity of memory vs latency (linear regression):")
    linearity(rows, "cold_init_ms", "cold-start init")
    linearity(rows, "warm_billed_avg_ms", "warm avg")

    print("\n#5 Significance of memory tier on warm latency:")
    if args.raw:
        significance(load_raw(args.raw))
    else:
        print("  (pass --raw <file> from benchmark.py --raw-out to run ANOVA/Kruskal-Wallis)")

    print("\nRQ(iv) Pareto-optimal configurations (latency vs cost, non-dominated):")
    pareto(rows)
    print()


if __name__ == "__main__":
    main()
