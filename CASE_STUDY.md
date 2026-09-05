# Agent Evaluation Framework: A Case Study

**What this is:** an instrumentation, evaluation, and regression-testing
pipeline for LLM agents, built and stress-tested on a real (if small)
agent over one focused build session. This document is the narrative
version -- what was built, what broke, what got fixed, and the one
finding that mattered most.

## The problem

Agents that plan, call tools, and reason over multiple steps fail in ways
standard testing doesn't catch. They don't crash -- they confidently
return a wrong, stale, or unstable answer, and the failure is buried
inside a chain of tool calls a human would have to read line by line to
catch. The bet behind this project: instrument every step, build an
automated judge to score runs at scale, and validate that judge against
real human labels before trusting it for anything -- rather than assuming
"ask another LLM if this was right" works out of the box.

## Architecture

```
Agent run -> Instrumentation -> Trace storage -> Eval engine -> Dashboard
                                                  (judge, regression,
                                                   consistency check)
```

- **Instrumentation**: a `@traced` decorator wraps every tool call and LLM
  call, capturing input/output/timing/errors without the agent's own logic
  knowing it's being watched.
- **Trace storage**: SQLite, two tables (`runs`, `spans`), queryable by
  run_id.
- **Eval engine**: an LLM judge (Claude, via forced tool-use for
  structured output) scoring pass/fail and failure category, calibrated
  against hand-written human labels using Cohen's kappa -- not trusted
  on faith.
- **Regression detection**: Fisher's exact test (exact, valid at small
  sample sizes) plus a bootstrap confidence interval, comparing pass
  rates before/after a change.
- **Consistency checker**: runs the same question N times and checks
  whether the agent's core claim is stable -- a mechanism added mid-project
  after discovering a failure category no single-trace judge could ever
  catch.
- **Dashboard**: a static HTML file aggregating all of the above. No
  server, just `python dashboard.py` and open the file.

Total dependencies: `anthropic`, `requests`, `pydantic`. Everything else,
including the statistics, is standard library.

## The failure taxonomy

Four distinct failure categories were found by actually running the agent
repeatedly on a small set of deliberately hard multi-hop questions and
diagnosing every failure down to a root cause, not just labeling pass/fail.

| Category | Description | Status |
|---|---|---|
| `query_reformulation_thrashing` | Agent issues many near-duplicate searches instead of conceding or committing, burning its entire step budget on one sub-question of a multi-hop task | **Fixed** via a system-prompt instruction to budget steps and give a best-effort answer with a caveat after 2-3 failed attempts |
| `stale_latest_claim` | Agent treats whatever its search returns as authoritative on recency, without checking dates | **Fixed** by injecting the actual current date into the system prompt -- the agent had no way to judge staleness without knowing what "now" was |
| `ungrounded_synthesis` | Agent retrieves a *correct, current* source but draws a conclusion the source doesn't actually support (e.g. conflating "fastest-growing" with "most popular") | **Fixed and validated** -- after instructing the agent to cross-reference at least one additional source and prefer primary sources for superlative claims, a retest correctly identified JavaScript (not Python) as most-used in the 2025 survey, explicitly distinguishing "most-used" from Python's real "fastest-growing" headline instead of conflating them |
| `ambiguous_metric_inconsistency` | Same question, same code, different confidently-stated answers across repeated runs, because the underlying ranking premise is genuinely contested | **Confirmed architectural, three times over** -- no judge prompt revision fixes it (tested twice), and an agent-level fix instructing it to cross-check multiple phrasings and hedge on disagreement also failed (tested once, 0/5 runs hedged, still a 3-way split). Detection requires running the question repeatedly and checking answer stability; the agent has no way to know its own evidence is incomplete from inside a single run. |

## The calibration story, with numbers

The judge wasn't trusted by default -- it was measured against hand
labels, the same way you'd validate any classifier.

- **First pass**: 81.2% raw agreement, but **Cohen's kappa = 0.333**.
  The gap between those two numbers is the actual headline: with 13 of 16
  labels being "pass," a judge that said "pass" every time would already
  score ~81% without doing any real work. Kappa corrected for that and
  revealed the judge had **zero recall on the failure class** -- all 3
  mismatches were real failures the judge called "pass."
- **Diagnosis**: each mismatch had a different root cause. One was the
  judge being too lenient about self-consistency vs. actual currency.
  One was the judge accepting the first supporting source without
  checking for contradictions elsewhere in the same trace. One (the
  ambiguous-metric case) was structurally unfixable by prompting at all.
- **Second pass, after revising the judge prompt**: 87.5% raw agreement,
  **kappa = 0.600**. The fixable issues were fixed; the structural one,
  predicted in advance to remain broken, remained broken -- confirming the
  diagnosis rather than just hoping a reworded prompt would help.

## The methodological lesson: n=1 comparisons lie

An early "before/after" test made the `query_reformulation_thrashing` fix
look like a clean 100% success -- one run before (failed), one run after
(succeeded). Running the same question 5 times later via the consistency
checker told a different story: **1 of 5 post-fix runs still hit
`MAX_STEPS_REACHED`.** The fix reduced the failure rate; it didn't
eliminate it, and a single comparison made it look like it did. This is
the kind of thing that's obvious in hindsight and invisible in the
moment -- exactly why repeated sampling, not single comparisons, has to be
the default for evaluating any nondeterministic system.

## Why even an agent-level fix couldn't touch the ambiguous-metric case

A third attempt was made to fix `ambiguous_metric_inconsistency` directly
in the agent's system prompt -- explicitly instructing it to try multiple
phrasings of a ranking claim within a single run and hedge if they
disagreed. Tested with 5 repeated runs: still a 3-way split (2x Southwest,
2x American, 1x step-exhaustion), and zero of the five runs hedged or
mentioned checking multiple phrasings at all. The instruction didn't fail
because it was poorly worded -- it failed because a single run's first
search can return self-reinforcing evidence (the open web genuinely
contains both "Southwest has the most routes" and "American has the most
destinations" as repeated claims), and the agent has no reason to doubt
evidence that looks internally consistent. This is the same root cause
that makes the judge unable to catch it: the unit being evaluated (one
run, one trace) is structurally the wrong unit for detecting this failure
at all. Only comparing *across* runs reveals it.

## The strongest version of the main finding: human labels on both sides

A second regression test was run after the independent labeling session, this
time with human labels on both sides -- not a mix of Claude labels and human
labels, but the same labeler applying the same standard to both the before and
after batches.

**Judge's report:** 60% → 87.5% pass rate, +27.5pp improvement.

**Reality by human labels:** Before batch: 3/10 pass (30%). After batch: 3/8
pass (37.5%). Effectively flat -- a 7.5pp difference far smaller than the
judge's 27.5pp claim.

The gap between what the judge reported and what actually happened is larger
here than in the earlier regression (where the judge reported 75% → 100% while
reality was 75% → 75%). This is because the human labeling standard is stricter:
stale queries that happened to produce correct answers are labeled fail under the
process-over-outcome standard, but the judge consistently calls them pass.

Fisher's exact test on the judge's own numbers returned p=0.31 -- not
significant even if the 87.5% figure had been real. So there are two independent
reasons not to trust the "it got better" story: the judge has a documented blind
spot on the specific failure class that dominates this question set, and the
sample size is too small to detect a real effect at this magnitude even without
the blind spot.

This is a cleaner result than the earlier regression because the labels are
entirely from one human reviewer applying one consistent standard, rather than a
mix of Claude labels and human labels. The conclusion is the same: automated
regression testing with a miscalibrated judge can silently report improvements
that didn't happen.

## The main finding: a kappa-0.6 judge can manufacture a false improvement

This is the result the whole project was building toward, and it landed
on real data rather than a hypothetical.

A formal regression test was run comparing 8 pre-fix runs against 8
post-fix runs, using the calibrated judge:

- **Judge's report**: 75% -> 100% pass rate. A 25-point improvement.
- **Reality, per careful human labeling of the same 16 runs**: 75% -> 75%.
  Flat. The specific failures changed (the two fixed categories
  disappeared) but two *new*, still-undetected categories
  (`ungrounded_synthesis`, `ambiguous_metric_inconsistency`) appeared in
  their place -- and the judge, with its known blind spot on exactly those
  two categories, missed both.
- **Independently**, Fisher's exact test on the judge's own numbers
  returned p = 0.467 -- not significant even if the 100% figure had been
  real, because n=8 per side is simply too small to distinguish a real
  effect from noise at that magnitude.

Two independent reasons not to trust the "it got better" story, and both
were caught before the claim was made, not after. This is the difference
between running an eval tool and understanding why eval tools need
calibration before their output means anything.

## Honest limitations

- Sample sizes throughout are small (8-16 labeled runs per batch, 5 runs
  per consistency check) -- enough to demonstrate the methodology, not
  enough to make strong claims about absolute failure rates.
- All test questions were self-authored multi-hop trivia, not a public
  benchmark (GAIA, WebArena) -- generalization beyond this question style
  is untested.
- One failure category remains genuinely unresolved after three independent
  fix attempts at two different layers (judge prompt, agent prompt):
  `ambiguous_metric_inconsistency` is correctly *detected* via the
  consistency checker, but the agent still has no way to know to hedge on
  contested-ranking questions from inside a single run.
  `ungrounded_synthesis`, by contrast, was successfully fixed once the
  agent was told to cross-reference sources rather than accept the first
  one that confirmed a claim.
- The judge's kappa of 0.6 is "good," not "excellent" -- there's a real
  ceiling on how much can be automated before some fraction of runs still
  need a human look.

## What this demonstrates

The instinct most people have when they hear "agent eval tool" is
LangSmith/Langfuse already do this. They do, and better, at scale. What
this project demonstrates isn't a better platform -- it's a specific,
evidenced understanding of *why* naive LLM-as-judge evaluation is
unreliable until it's calibrated, and *why* some failure modes need a
fundamentally different detection mechanism (repeated sampling) rather
than a smarter judge, no matter how well-prompted. That's the part that's
hard to fake and the part worth leading with.

## Independent human labeling session: what actually came out

A separate labeling session was conducted where a human reviewer (the
project author, not Claude) independently labeled 23 agent runs from
scratch -- reading each trace, checking the reasoning step by step, and
applying their own judgment about pass/fail and failure category.

**Key findings from independent labeling:**

**1. `stale_latest_claim` is the dominant failure mode.** Across the
labeled runs, stale queries appeared in the majority of failures --
the agent consistently searched with "2025" in queries despite knowing
today's date is 2026. This confirmed the date-injection fix was
necessary but insufficient: the agent still forms stale queries on
time-sensitive questions, even with today's date in the system prompt.

**2. Process reliability matters more than outcome correctness.** The
human labeler applied a stricter standard than the judge: a stale query
that happened to return a still-correct answer was labeled fail, because
the process is broken regardless of whether it got lucky. This is the
right standard for an eval system -- a process that only fails when the
underlying fact changed is still a broken process.

**3. The judge systematically disagrees on process-over-outcome cases.**
After independent labeling, kappa dropped to 0.240 -- lower than the
0.600 achieved against Claude's own labels. This confirmed that the
0.600 kappa was partially an artifact of Claude calibrating against
Claude: both the labeler and the judge shared the same implicit standard
of evaluating outcomes rather than process. When a genuinely different
human standard was applied, the judge's blind spots became much more
visible.

**4. A judge prompt change didn't fix it.** A new instruction was added
explicitly telling the judge to flag stale queries regardless of whether
the final answer was correct. Kappa did not improve after this change --
the judge continued finding other reasons to call those runs pass. This
is a harder-to-fix blind spot than the earlier ones: it requires the
judge to reason about what *could have* gone wrong if the underlying
fact had changed, not just what *did* go wrong in this specific run.

**The meta-finding:** the earlier calibration kappa of 0.600 was
measuring "does the judge agree with Claude's labels" -- which is a
weaker test than "does the judge agree with a human's labels." The
real calibration number, against an independent human reviewer applying
a principled process-over-outcome standard, is **0.161** with the same
model (Sonnet) as judge -- confirmed across 32 labeled runs.

A third experiment was then run: swapping the judge to a different
model family (Haiku, claude-haiku-4-5-20251001) while keeping the same
human labels. Haiku scored **0.241** -- meaningfully better agreement
with the human standard than Sonnet achieved.

| Judge | Labeler | Kappa | Interpretation |
|---|---|---|---|
| claude-sonnet-4-6 | Claude (same model) | 0.600 | Inflated by shared blind spots |
| claude-sonnet-4-6 | Human | 0.161 | Real agreement -- barely better than chance |
| claude-haiku-4-5-20251001 | Human | 0.241 | Better judge despite being a smaller model |

The counterintuitive finding: a cheaper, less capable model is a better
judge for this task. The explanation is that Haiku is more literal and
rule-following -- when the judge prompt says "stale query = fail
regardless of outcome," Haiku applies that rule directly. Sonnet is
capable enough to reason around it ("well yes the query said 2025 but
the answer is still correct so...") which is exactly the wrong behavior
for a calibration judge with explicit process rules.

This generalizes: for structured evaluation tasks with explicit scoring
rules, a more literal model may outperform a more capable one. The
right judge model is not necessarily the smartest one -- it's the one
that most reliably applies the specified standard without rationalizing
exceptions.

This specific three-way comparison -- 0.600 same-model, 0.161
cross-model same-family, 0.241 cross-model different-family -- is the
number worth leading with. It shows exactly why "we calibrated our
judge" means nothing without specifying who the labels came from and
which model did the judging.
