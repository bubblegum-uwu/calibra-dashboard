"""
agent.py -- a real ReAct-style agent, fully instrumented.

This is the actual agent you'll be evaluating: it gets a task, decides
whether to search the web, reads results, and either searches again or
answers. Every LLM call and every search call is wrapped with @traced,
so the full sequence of steps lands in traces.db automatically.

Requires:
    pip install anthropic requests
    export ANTHROPIC_API_KEY=sk-ant-...
    export TAVILY_API_KEY=tvly-...

Run:
    python agent.py
"""

from __future__ import annotations

import os

import requests
from anthropic import Anthropic

from tracing import Tracer, traced

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

tracer = Tracer()
client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this whenever you need "
        "a fact you don't already know with confidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "the search query"}},
        "required": ["query"],
    },
}


@traced("web_search", tracer)
def web_search(query: str) -> str:
    response = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 3},
        timeout=15,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return "No results found."
    return "\n\n".join(f"{r['title']}: {r['content']}" for r in results[:3])


def summarize_claude_response(response) -> dict:
    """Pull out just the fields worth logging -- not the whole SDK object."""
    tool_calls = [
        {"tool": b.name, "input": b.input} for b in response.content if b.type == "tool_use"
    ]
    text = next((b.text for b in response.content if b.type == "text"), None)
    return {
        "stop_reason": response.stop_reason,
        "text": text,
        "tool_calls": tool_calls,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


from datetime import date

SYSTEM_PROMPT = f"""You are a research agent that answers questions by searching the web.
Today's date is {date.today().isoformat()}.

You have a limited number of tool calls available, so budget them carefully. If a \
question requires multiple sub-facts (for example, "the capital of the country with the \
largest population"), don't spend your entire budget on the first sub-fact -- leave room \
to research the later ones too.

If a specific metric or fact proves hard to pin down after 2-3 search attempts -- for \
example, sources keep reporting a different but related metric instead of the exact one \
asked for -- give your best answer using the closest available data and explicitly say \
what metric you actually used and why the exact one wasn't available, rather than \
continuing to reformulate the same search over and over.

When asked about anything described as "latest," "current," or "most recent," include \
the actual current year (or the most recent likely year) in your search query instead of \
a vague term -- search engines often surface older, more established pages otherwise. \
Then check the publish date or context of your sources before treating a result as \
authoritative on recency. If your search only turns up data from a year or more ago, say \
so explicitly in your answer instead of presenting it as definitely current.

For "most/best/largest"-type claims based on a survey, ranking, or report, don't settle \
for the first source that confirms it. Check at least one more source, and prefer the \
primary/official source (e.g. the report's own site) over secondary commentary or blog \
posts when they're available -- a single blog's framing of a result is not the same as \
the result itself.

More broadly, for any "most/best/largest" ranking claim, deliberately try at least two \
differently-phrased searches for the same underlying question (for example "most routes" \
and "most destinations" and "largest domestic market share" are different phrasings that \
might surface different answers for the same airline question). If different phrasings \
lead to different answers, that usually means the ranking is genuinely contested, not \
that you searched wrong -- say so explicitly in your final answer instead of picking one \
with full confidence."""


@traced("llm_call", tracer, summarize=summarize_claude_response)
def call_claude(messages: list[dict]):
    return client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[WEB_SEARCH_TOOL],
        messages=messages,
    )


def run_agent(task: str, max_steps: int = 8, group_id: str | None = None) -> tuple[str, str]:
    run_id = tracer.start_run(task, group_id=group_id)
    messages = [{"role": "user", "content": task}]

    for _ in range(max_steps):
        response = call_claude(messages=messages, run_id=run_id)
        # Anthropic's content blocks aren't JSON-serializable by default;
        # convert before appending so future steps (and tracing) stay clean.
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text_blocks = [b.text for b in response.content if b.type == "text"]
            final_text = text_blocks[0] if text_blocks else "(no text response)"
            tracer.end_run(run_id, final_text)
            return run_id, final_text

        tool_results = []
        for tool_use in tool_uses:
            if tool_use.name == "web_search":
                result = web_search(query=tool_use.input["query"], run_id=run_id)
            else:
                result = f"Unknown tool: {tool_use.name}"
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tool_use.id, "content": result}
            )
        messages.append({"role": "user", "content": tool_results})

    tracer.end_run(run_id, "MAX_STEPS_REACHED")
    return run_id, "MAX_STEPS_REACHED"


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        raise SystemExit("Set ANTHROPIC_API_KEY first: export ANTHROPIC_API_KEY=sk-ant-...")
    if not TAVILY_API_KEY:
        raise SystemExit("Set TAVILY_API_KEY first: export TAVILY_API_KEY=tvly-...")

    task = "What's the capital of the country with the largest population in Africa?"
    run_id, answer = run_agent(task)

    print(f"\nFinal answer: {answer}")
    tracer.pretty_print(run_id)
    print(f"\nFull trace saved in traces.db under run_id={run_id}")
