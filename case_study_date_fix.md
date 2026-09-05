# Case study: a real fix, measured before and after

This happened naturally in week 1, but it's exactly the kind of evidence the
"regression detection" piece of the project (week 6-8) is meant to produce
deliberately later. Worth keeping as a template for that.

## The setup

Same 8 test questions, run twice, with one change in between: added a
system prompt instructing the agent to (a) budget tool calls across
multi-hop sub-questions, (b) give a best-effort answer with a caveat after
2-3 failed attempts at an exact metric instead of reformulating forever,
and (c) check source recency before treating something as "latest."

## Result: one failure mode fixed, one unchanged

| Question | Before | After | Verdict |
|---|---|---|---|
| #7 -- airline CEO | `MAX_STEPS_REACHED` (6 nearly-identical search reformulations, never committed to an answer) | Converged: American Airlines / Robert Isom | **Fixed** |
| #6 -- Stack Overflow survey | Cited "2024 survey" as "the latest" | Cited "2024 survey" as "the latest" -- identical | **Unchanged** |

## Why the fix only worked for one of them

The "budget your steps" and "give a best-effort answer" instructions
directly addressed *behavior* (when to stop searching and commit). The
"check source recency" instruction didn't work because it asked the model
to judge something it had no information to judge -- the agent calling the
API was never told what today's actual date is, so it couldn't tell
whether "2024" was stale or current, and couldn't search with the right
year either.

Second fix applied: injected the actual current date into the system
prompt (`Today's date is {date.today().isoformat()}`) and instructed the
agent to include the current year explicitly in searches about "latest"
topics. Re-run pending to confirm this one actually works -- unlike the
first fix, this hasn't been validated against real data yet.

## The takeaway worth remembering

A prompt instruction that *sounds* like it addresses a failure can fail
silently if the model doesn't have the information it needs to follow the
instruction. "Check if this is current" is not actionable without knowing
what "current" means right now. This is worth watching for again during
calibration in week 3 -- an instruction that doesn't change behavior on
re-test is a sign to look for a missing piece of context, not just reword
the instruction harder.
