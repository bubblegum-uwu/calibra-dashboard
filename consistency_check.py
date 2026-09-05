"""
consistency_check.py -- catch failures invisible to a single-trace judge.

Some failures (like d52c7e87 -- the airline question) look perfectly fine
from any single trace, because the trace's own evidence genuinely supports
whatever answer it landed on. No amount of better judge prompting can
catch this; it needs a structurally different check: run the same
question multiple times and see if the agent's core answer is stable.

Run:
    python consistency_check.py "your question here" --runs 5
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter

from pydantic import BaseModel, Field

try:
    import anthropic
except ImportError:
    anthropic = None

from agent import run_agent

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if (anthropic and ANTHROPIC_API_KEY) else None


class CoreClaimExtraction(BaseModel):
    core_claim: str = Field(
        ...,
        description=(
            "The single key entity/fact this answer asserts as the answer, normalized to "
            "a short canonical form -- e.g. just 'Southwest Airlines', not the full sentence"
        ),
    )


EXTRACTION_SCHEMA = CoreClaimExtraction.model_json_schema()


def extract_core_claim(question: str, answer: str) -> str:
    """Reduce a verbose answer down to the one entity/fact it's actually asserting,
    so two answers can be compared even if they're worded completely differently."""
    # Known non-answer sentinel -- skip the API call and guarantee a canonical label,
    # rather than letting the model word "no answer" differently each time.
    if answer.strip() == "MAX_STEPS_REACHED":
        return "NO_CLEAR_ANSWER"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        tools=[
            {
                "name": "record_claim",
                "description": "Record the core claim of this answer.",
                "input_schema": EXTRACTION_SCHEMA,
            }
        ],
        tool_choice={"type": "tool", "name": "record_claim"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\nAnswer: {answer}\n\n"
                    "What is the single core entity/fact this answer asserts as the answer "
                    "to the question? Normalize it to a short canonical form. "
                    "If the answer does NOT commit to one clear entity/fact -- it hedges, "
                    "presents multiple options without picking one, or fails to answer -- "
                    "respond with exactly the string 'NO_CLEAR_ANSWER' rather than describing "
                    "why in your own words. This exact string must be used consistently so "
                    "non-answers can be counted as one category, not several differently-worded ones."
                ),
            }
        ],
    )
    tool_call = next(b for b in response.content if b.type == "tool_use")
    return CoreClaimExtraction.model_validate(tool_call.input).core_claim


def check_consistency(question: str, n_runs: int = 5) -> dict:
    group_id = f"consistency_{int(time.time())}"
    answers = []
    for i in range(n_runs):
        print(f"  Run {i + 1}/{n_runs}...")
        _, final_answer = run_agent(question, group_id=group_id)
        answers.append(final_answer)

    core_claims = [extract_core_claim(question, a) for a in answers]
    counts = Counter(core_claims)

    return {
        "question": question,
        "n_runs": n_runs,
        "group_id": group_id,
        "answers": answers,
        "core_claims": core_claims,
        "claim_counts": dict(counts),
        "is_consistent": len(counts) == 1,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check whether an agent gives a stable answer across repeated runs.")
    parser.add_argument("question")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--save", action="store_true", help="Save the result to a JSON file for the dashboard to pick up")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        raise SystemExit("Set ANTHROPIC_API_KEY first.")

    result = check_consistency(args.question, args.runs)
    print()
    print(json.dumps(result, indent=2))

    if result["is_consistent"]:
        print(f"\nCONSISTENT: all {args.runs} runs converged on the same core claim.")
    else:
        print(f"\nINCONSISTENT: {len(result['claim_counts'])} distinct claims across {args.runs} runs:")
        for claim, count in result["claim_counts"].items():
            print(f"  {claim}: {count}/{args.runs}")
        print(
            "\nThis question's premise is likely genuinely contested. Worth checking "
            "whether the agent should be instructed to hedge on this type of question "
            "rather than commit to one answer."
        )

    if args.save:
        filename = f"consistency_{int(time.time())}.json"
        with open(filename, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nSaved to {filename}")
