"""
judge.py -- LLM-as-judge for scoring agent runs automatically.

This is the calibration step: given a full trace, the judge tries to do
what you've been doing by hand for the last 3 batches -- decide pass/fail,
guess a failure category, and explain why. The point of calibrate.py
(next file) is to measure how often this agrees with your own hand
labels, not to trust it blindly.

Run:
    python judge.py <run_id>
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

try:
    import anthropic
except ImportError:
    anthropic = None

from tracing import Tracer

JUDGE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]
DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"
CONFIG_FILE = "config.json"

def _get_judge_model() -> str:
    try:
        import json, os
        if os.path.exists(CONFIG_FILE):
            cfg = json.load(open(CONFIG_FILE))
            return cfg.get("judge_model", DEFAULT_JUDGE_MODEL)
    except Exception:
        pass
    return DEFAULT_JUDGE_MODEL

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if (anthropic and ANTHROPIC_API_KEY) else None

# Known categories, built directly from what was actually found in batches 1-3.
# "other" exists so the judge isn't forced into a bad fit -- if it picks
# "other" a lot, that's a signal the taxonomy itself needs another category.
KNOWN_FAILURE_CATEGORIES = [
    "max_steps_exhausted",
    "query_reformulation_thrashing",
    "stale_latest_claim",
    "ungrounded_synthesis",
    "ambiguous_metric_inconsistency",
    "other",
]


class JudgeVerdict(BaseModel):
    verdict: str = Field(..., description="Exactly 'pass' or 'fail'")
    failure_category: Optional[str] = Field(
        None,
        description=f"One of {KNOWN_FAILURE_CATEGORIES} if verdict is 'fail', else null",
    )
    reasoning: str = Field(..., description="1-3 sentences explaining the verdict")
    flagged_ambiguity: bool = Field(
        False,
        description=(
            "True if the question's premise involves a contested 'most/largest/best' "
            "ranking and the answer asserted one option with full confidence and no "
            "caveat, even though sources disagree"
        ),
    )


JUDGE_SCHEMA = JudgeVerdict.model_json_schema()

JUDGE_SYSTEM_PROMPT = f"""You are evaluating whether an AI research agent's answer to a \
question is correct and well-supported. Today's date is {date.today().isoformat()}.

A run must pass BOTH of the following standards to receive a verdict of "pass":

  STANDARD 1 -- OUTCOME CORRECTNESS: Did the agent get the right answer?
  STANDARD 2 -- PROCESS CORRECTNESS: Did the agent get there the right way?

A run that reaches the correct answer through a flawed process is still a FAIL.
A run that followed a correct process but got a wrong answer is also a FAIL.
Both standards must pass independently.

You will see the task, the full sequence of search queries and results the agent used,
and its final answer. If a GROUND TRUTH ANSWER is provided, use it as the authoritative
reference for outcome correctness. If no ground truth is provided, use your own knowledge.

Judge based on all of the following:

1. OUTCOME: Does the final answer actually answer the question asked, and is it correct?
   If a GROUND TRUTH ANSWER is provided above the trace, compare the agent's answer directly
   against it. A wrong answer is a fail regardless of how good the process was.

2. GROUNDING: Is the final answer supported by the search results the agent actually retrieved?
   Do not stop at the first source that supports the claim -- scan ALL search results in the
   trace for anything that contradicts it. If any retrieved source conflicts with the final
   answer and the agent didn't acknowledge or resolve that conflict, this is ungrounded_synthesis
   even if another source does support the answer. Weigh primary/official sources (e.g. a
   survey's own results page) more heavily than secondary commentary (blogs, opinion pieces)
   when they disagree.

3. AMBIGUITY: If the question's premise involves a "most/largest/best" ranking, do the retrieved
   sources actually agree on a single answer, or is it genuinely contested? If contested and
   the agent picked one answer with full confidence and no caveat, set flagged_ambiguity=true.
   If the GROUND TRUTH ANSWER starts with "CONTESTED", this question is known to be ambiguous --
   the agent MUST acknowledge the ambiguity to pass. A single confident answer with no caveat
   is an automatic fail (ambiguous_metric_inconsistency), regardless of which option it picked.

4. PROCESS -- RECENCY: If the question involves "latest/current/most recent," did the agent
   actively verify recency by searching with the current year ({date.today().year}) or finding
   a source dated at or near today? Being internally consistent with whatever it found is NOT
   sufficient. If the agent searched with a stale year (e.g. "2025" when today is {date.today().year})
   or accepted a result without checking if a more recent one exists, this is stale_latest_claim
   EVEN IF the final answer happens to be correct. A correct answer reached via a stale process
   is still a fail -- you are evaluating process reliability, not just outcome.

5. COMPLETION: Did the agent reach a clear answer at all, or get stuck
   (e.g. final_answer is literally "MAX_STEPS_REACHED")?

Pick a failure_category from the known list if verdict is 'fail'. Use 'other' only if
none of the known categories genuinely fit -- don't force a bad fit."""


def format_trace_for_judge(run, spans, ground_truth: dict | None = None) -> str:
    _, task, final_answer, _, _ = run
    lines = [f"TASK: {task}"]
    if ground_truth:
        lines.append(f"GROUND TRUTH ANSWER: {ground_truth['ground_truth']}")
        if ground_truth.get("notes"):
            lines.append(f"KEY FACTS / NOTES: {ground_truth['notes']}")
        lines.append("")
        lines.append("IMPORTANT: Compare the agent's final answer against the GROUND TRUTH ANSWER above.")
        lines.append("The ground truth is authoritative -- use it as the reference for correctness.")
        lines.append("")
        lines.append("SPECIAL RULE FOR CONTESTED QUESTIONS:")
        lines.append("If the GROUND TRUTH ANSWER starts with 'CONTESTED', this question has no single correct answer.")
        lines.append("In that case, do NOT judge whether the agent picked the right option.")
        lines.append("Instead judge ONLY whether the agent acknowledged the ambiguity.")
        lines.append("PASS: agent presented multiple valid answers or explicitly said the answer depends on the metric.")
        lines.append("FAIL (ambiguous_metric_inconsistency): agent picked one answer confidently with no caveat or hedge.")
    lines += [f"FINAL ANSWER: {final_answer}", "", "STEPS:"]
    for step_index, name, input_data, output_data, duration_ms, error in spans:
        lines.append(f"\nStep {step_index} ({name}):")
        lines.append(f"  input: {input_data}")
        lines.append(f"  output: {output_data}")
        if error:
            lines.append(f"  ERROR: {error}")
    return "\n".join(lines)


def judge_run(run_id: str, tracer: Tracer) -> JudgeVerdict:
    run, spans = tracer.get_trace(run_id)
    if run is None:
        raise ValueError(f"No run found with id {run_id}")

    # Load ground truth if available
    ground_truth = None
    try:
        from import_ground_truth import load_answers, find_ground_truth
        answers = load_answers()
        if answers:
            _, task, _, _, _ = run
            ground_truth = find_ground_truth(task, answers)
    except Exception:
        pass

    trace_text = format_trace_for_judge(run, spans, ground_truth)

    response = client.messages.create(
        model=_get_judge_model(),  # configurable via config.json
        max_tokens=600,
        system=JUDGE_SYSTEM_PROMPT,
        tools=[
            {
                "name": "record_verdict",
                "description": "Record the judgment for this run.",
                "input_schema": JUDGE_SCHEMA,
            }
        ],
        tool_choice={"type": "tool", "name": "record_verdict"},
        messages=[{"role": "user", "content": trace_text}],
    )
    tool_call = next(b for b in response.content if b.type == "tool_use")
    return JudgeVerdict.model_validate(tool_call.input)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python judge.py <run_id>")
    if not ANTHROPIC_API_KEY:
        raise SystemExit("Set ANTHROPIC_API_KEY first.")

    tracer = Tracer()
    verdict = judge_run(sys.argv[1], tracer)
    print(json.dumps(verdict.model_dump(), indent=2))
