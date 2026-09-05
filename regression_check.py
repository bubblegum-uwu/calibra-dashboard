"""
regression_check.py -- did a change actually help, or is it noise?

Takes two sets of run_ids (e.g. before/after a prompt change), judges each
one with the LLM judge, and reports whether the difference in pass rate is
statistically meaningful. With small sample sizes (5-10 runs per side,
which is realistic here), "60% vs 80%" can easily be noise -- this uses
Fisher's exact test (exact, not an asymptotic approximation, so it's valid
even at this scale) plus a bootstrap confidence interval on the difference.

Run:
    python regression_check.py --before run_id1 run_id2 ... --after run_id3 run_id4 ...
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time

from judge import judge_run
from tracing import Tracer


def hypergeom_pmf(k: int, n1: int, n2: int, m1: int) -> float:
    n = n1 + n2
    if k < max(0, m1 - n2) or k > min(n1, m1):
        return 0.0
    return math.comb(n1, k) * math.comb(n2, m1 - k) / math.comb(n, m1)


def fishers_exact_test(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher's exact test p-value for the 2x2 table:
              pass  fail
    before     a     b
    after      c     d
    """
    n1, n2 = a + b, c + d
    m1 = a + c
    observed_p = hypergeom_pmf(a, n1, n2, m1)
    k_min, k_max = max(0, m1 - n2), min(n1, m1)
    epsilon = 1e-9
    total = sum(
        hypergeom_pmf(k, n1, n2, m1)
        for k in range(k_min, k_max + 1)
        if hypergeom_pmf(k, n1, n2, m1) <= observed_p + epsilon
    )
    return min(total, 1.0)


def bootstrap_ci_diff(
    before_outcomes: list[int], after_outcomes: list[int], n_bootstrap: int = 10000, ci: float = 0.95
) -> tuple[float, float]:
    """Bootstrap CI for (after pass rate - before pass rate)."""
    n_before, n_after = len(before_outcomes), len(after_outcomes)
    diffs = []
    for _ in range(n_bootstrap):
        b_sample = [random.choice(before_outcomes) for _ in range(n_before)]
        a_sample = [random.choice(after_outcomes) for _ in range(n_after)]
        diffs.append(sum(a_sample) / n_after - sum(b_sample) / n_before)
    diffs.sort()
    lo = int((1 - ci) / 2 * n_bootstrap)
    hi = int((1 - (1 - ci) / 2) * n_bootstrap) - 1
    return diffs[lo], diffs[hi]


def judge_set(run_ids: list[str], tracer: Tracer) -> tuple[list[int], list[dict]]:
    outcomes, details = [], []
    for run_id in run_ids:
        try:
            verdict = judge_run(run_id, tracer)
        except ValueError:
            print(f"  Skipping {run_id}: not found in traces.db")
            continue
        outcomes.append(1 if verdict.verdict == "pass" else 0)
        details.append(
            {"run_id": run_id, "verdict": verdict.verdict, "category": verdict.failure_category}
        )
    return outcomes, details


def regression_report(before_ids: list[str], after_ids: list[str]) -> dict:
    tracer = Tracer()

    print("Judging 'before' runs...")
    before_outcomes, before_details = judge_set(before_ids, tracer)
    print("Judging 'after' runs...")
    after_outcomes, after_details = judge_set(after_ids, tracer)

    if not before_outcomes or not after_outcomes:
        raise SystemExit("Need at least one valid run on each side to compare.")

    n_before, n_after = len(before_outcomes), len(after_outcomes)
    pass_before, pass_after = sum(before_outcomes), sum(after_outcomes)
    rate_before, rate_after = pass_before / n_before, pass_after / n_after
    fail_before, fail_after = n_before - pass_before, n_after - pass_after

    p_value = fishers_exact_test(pass_before, fail_before, pass_after, fail_after)
    ci_lo, ci_hi = bootstrap_ci_diff(before_outcomes, after_outcomes)

    return {
        "before": {"n": n_before, "pass_rate": rate_before, "details": before_details},
        "after": {"n": n_after, "pass_rate": rate_after, "details": after_details},
        "diff": rate_after - rate_before,
        "ci_95": [ci_lo, ci_hi],
        "fishers_p_value": p_value,
        "significant_at_0.05": p_value < 0.05,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test whether a change actually improved pass rate, or if it's noise.")
    parser.add_argument("--before", nargs="+", required=True, help="run_ids from before the change")
    parser.add_argument("--after", nargs="+", required=True, help="run_ids from after the change")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first.")

    report = regression_report(args.before, args.after)

    print()
    print(json.dumps(report, indent=2))
    print(f"\nBefore: {report['before']['pass_rate']*100:.1f}% pass ({report['before']['n']} runs)")
    print(f"After:  {report['after']['pass_rate']*100:.1f}% pass ({report['after']['n']} runs)")
    print(f"Difference: {report['diff']*100:+.1f} percentage points")
    print(f"95% bootstrap CI on difference: [{report['ci_95'][0]*100:+.1f}, {report['ci_95'][1]*100:+.1f}] pp")
    print(f"Fisher's exact test p-value: {report['fishers_p_value']:.4f}")
    if report["significant_at_0.05"]:
        print("=> Significant at p<0.05 -- this difference is unlikely to be noise.")
    else:
        print("=> NOT significant at p<0.05 -- at this sample size, this difference could easily be noise.")

    filename = f"regression_{int(time.time())}.json"
    with open(filename, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved to {filename}")
