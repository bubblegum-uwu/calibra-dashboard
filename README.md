# Agent eval/observability -- week 1

The instrumentation layer and a real, working agent to run it on.

## What's actually been verified already (no keys needed)

- `tracing.py` -- the SQLite-backed tracer and `@traced` decorator. I ran
  this with fake functions, confirmed traces persist correctly to disk,
  and confirmed that when a wrapped function raises an error, the error is
  both (a) logged into the trace with the message captured, and (b)
  re-raised normally so your code's error handling still works. This part
  is done and correct.

## What needs your API keys to run

- `agent.py` -- a real ReAct-style agent: it gets a task, decides whether
  to search the web (via Tavily), reads results, and either searches again
  or gives a final answer. Every LLM call and every search call is wrapped
  with `@traced`, so the whole sequence lands in `traces.db` automatically.

## Exact setup steps

1. **Install Python packages**
   ```bash
   pip install anthropic requests
   ```

2. **Get an Anthropic API key**
   Go to console.anthropic.com -> API Keys -> Create Key.
   (This is separate from a claude.ai subscription -- you're billed per
   API call, usually a few cents for a run like this.)

3. **Get a Tavily API key**
   Go to tavily.com -> sign up -> copy your API key from the dashboard.
   Free tier covers about 1,000 searches/month, plenty for this project.

4. **Set both as environment variables**
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   export TAVILY_API_KEY=tvly-...
   ```

5. **Run the agent**
   ```bash
   python agent.py
   ```
   This runs one task ("What's the capital of the country with the largest
   population in Africa?"), prints the final answer, then pretty-prints
   the full step-by-step trace -- every search call, every LLM call, with
   timing.

6. **Look at the raw trace database**
   ```bash
   sqlite3 traces.db
   .tables
   SELECT * FROM runs;
   SELECT * FROM spans WHERE run_id = '<run_id from step 5>';
   ```
   (If you don't have the `sqlite3` CLI, `pip install sqlite-web` gives you
   a quick browser-based viewer instead.)

## What to check once it's running

- Does the trace show the agent searching twice (once for the country,
  once for the capital), or does it sometimes try to answer from memory
  without searching? Both are useful things to notice now, before you've
  built any eval tooling on top.
- Try a harder multi-hop question and see if the agent gets stuck retrying
  the same search, or gives up after one try. These are exactly the failure
  patterns the clustering step (week 3-5) will need to catch automatically.

## Next step (day 6-7 of week 1, already partially done)

`tracing.py` already persists to SQLite, so this part of the original plan
is finished. What's left for the rest of week 1:
- Run the agent on 5-10 different multi-hop questions, not just one
- Confirm traces look sane across all of them by eyeballing `traces.db`
- Start a running list of questions where the agent visibly struggles --
  this becomes your test set for week 2
