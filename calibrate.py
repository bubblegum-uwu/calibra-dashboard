"""
calibrate.py -- measure how well the judge agrees with your hand labels.

Loads every label file in ground_truth/, runs the judge on each
corresponding trace (pulled from traces.db by run_id), and reports raw
agreement and Cohen's kappa -- raw agreement alone is misleading if most
runs pass, since a judge that says "pass" every time would score high on
agreement without doing anything useful. Kappa corrects for that.

Run:
    python calibrate.py
"""

from __future__ import annotations

import glob
import json
import os

from judge import judge_run
from tracing import Tracer


def load_all_labels() -> dict:
    labels = {}
    for path in glob.glob(os.path.join("ground_truth", "*.json")):
        with open(path) as f:
            labels.update(json.load(f))
    return labels


def cohens_kappa(human_labels: list[str], judge_labels: list[str]) -> float:
    categories = sorted(set(human_labels) | set(judge_labels))
    n = len(human_labels)
    po = sum(h == j for h, j in zip(human_labels, judge_labels)) / n

    pe = 0.0
    for cat in categories:
        p_human = human_labels.count(cat) / n
        p_judge = judge_labels.count(cat) / n
        pe += p_human * p_judge

    if pe == 1.0:
        return 1.0  # no variation in either set -- avoid divide by zero
    return (po - pe) / (1 - pe)


if __name__ == "__main__":
    labels = load_all_labels()
    tracer = Tracer()

    human_verdicts, judge_verdicts, mismatches, skipped = [], [], [], []

    for run_id, label in labels.items():
        try:
            verdict = judge_run(run_id, tracer)
        except ValueError:
            skipped.append(run_id)
            continue

        human_verdicts.append(label["label"])
        judge_verdicts.append(verdict.verdict)

        if label["label"] != verdict.verdict:
            mismatches.append(
                (run_id, label["task"], label["label"], verdict.verdict, verdict.reasoning)
            )

    if skipped:
        print(f"Skipped {len(skipped)} run_id(s) not found in traces.db: {skipped}")

    if not human_verdicts:
        raise SystemExit("No labeled runs with matching traces found -- nothing to calibrate.")

    agree_pct = sum(h == j for h, j in zip(human_verdicts, judge_verdicts)) / len(human_verdicts) * 100
    kappa = cohens_kappa(human_verdicts, judge_verdicts)

    print(f"\nScored {len(human_verdicts)} runs")
    print(f"Raw agreement: {agree_pct:.1f}%")
    print(f"Cohen's kappa: {kappa:.3f}")
    print("  (< 0.4 = weak, 0.4-0.6 = moderate, 0.6-0.8 = good, > 0.8 = excellent)")

    if mismatches:
        print(f"\n{len(mismatches)} mismatch(es) -- read these closely, they're the interesting part:")
        for run_id, task, human, judge_v, reasoning in mismatches:
            print(f"\n  {run_id}: {task[:70]}")
            print(f"    human said: {human}  |  judge said: {judge_v}")
            print(f"    judge's reasoning: {reasoning}")
