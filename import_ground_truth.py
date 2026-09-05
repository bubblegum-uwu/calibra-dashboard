"""
import_ground_truth.py -- load ground truth answers from Excel into the system.

Reads ground_truth_answers.xlsx and saves to ground_truth_answers.json,
which the judge reads when scoring runs.

Run:
    python import_ground_truth.py
    python import_ground_truth.py --file path/to/other.xlsx

After importing, the judge will compare agent answers against the stored
correct answers instead of judging blindly from its own knowledge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ANSWERS_JSON = "ground_truth_answers.json"


def import_from_excel(path: str) -> dict:
    try:
        import openpyxl
    except ImportError:
        print("openpyxl not installed. Run: pip install openpyxl")
        sys.exit(1)

    wb = openpyxl.load_workbook(path, data_only=True)
    if "Ground Truth" not in wb.sheetnames:
        print(f"No 'Ground Truth' sheet found in {path}")
        sys.exit(1)

    ws = wb["Ground Truth"]
    answers = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        num, question, answer, notes, verified = row
        if not question or not answer:
            continue
        answers[str(num)] = {
            "question": str(question).strip(),
            "ground_truth": str(answer).strip(),
            "notes": str(notes).strip() if notes else "",
            "last_verified": str(verified).strip() if verified else "",
        }

    return answers


def save_answers(answers: dict) -> None:
    with open(ANSWERS_JSON, "w") as f:
        json.dump(answers, f, indent=2)


def load_answers() -> dict:
    """Load ground truth answers — called by judge.py."""
    if not os.path.exists(ANSWERS_JSON):
        return {}
    with open(ANSWERS_JSON) as f:
        return json.load(f)


def find_ground_truth(question: str, answers: dict) -> dict | None:
    """Find the ground truth entry for a given question by fuzzy match."""
    question_lower = question.lower().strip()
    for entry in answers.values():
        stored = entry["question"].lower().strip()
        # Check if questions are substantially similar (share key words)
        stored_words = set(stored.split())
        question_words = set(question_lower.split())
        overlap = len(stored_words & question_words)
        if overlap >= 5 or stored[:50] == question_lower[:50]:
            return entry
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import ground truth answers from Excel.")
    parser.add_argument("--file", default="ground_truth_answers.xlsx",
                        help="Path to the Excel file (default: ground_truth_answers.xlsx)")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"File not found: {args.file}")
        print("Download ground_truth_answers.xlsx and place it in your project folder.")
        sys.exit(1)

    answers = import_from_excel(args.file)
    save_answers(answers)
    print(f"Imported {len(answers)} ground truth answers to {ANSWERS_JSON}")
    print()
    for num, entry in answers.items():
        print(f"  Q{num}: {entry['question'][:60]}...")
        print(f"        -> {entry['ground_truth'][:80]}")
        print()
    print("Judge will now use these answers when scoring runs.")
    print("Run 'python calibrate.py' to re-score existing labels with the new standard.")
