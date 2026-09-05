"""
run_batch.py -- run the agent over a batch of test questions in one go.

These questions are chosen to actually stress the agent: multi-hop chains,
time-sensitive facts, and "most/largest/highest" claims where sources can
disagree. Run this, then go hand-label each result -- pass/fail, and if
fail, why. Those labels become your calibration set for the LLM judge in
week 3.

Run:
    python run_batch.py
"""

import json
import time

from agent import run_agent

TEST_QUESTIONS = [
    "What's the capital of the country with the largest population in Africa?",
    "Who is the current head of state of the country with the second-largest economy in South America?",
    "What is the population of the capital city of the country where the Eiffel Tower is located?",
    "Who directed the highest-grossing film starring the actor who won Best Actor at the most recent Oscars?",
    "What is the GDP per capita of the country that won the most recent FIFA World Cup?",
    "Which programming language is ranked most popular in the latest Stack Overflow developer survey, and who originally created it?",
    "Who is the CEO of the parent company of the airline with the most domestic routes in the United States?",
    "What is the tallest building in the city that hosted the most recent Summer Olympics?",
]


if __name__ == "__main__":
    group_id = f"batch_{int(time.time())}"
    results = []
    for i, question in enumerate(TEST_QUESTIONS, 1):
        print(f"\n[{i}/{len(TEST_QUESTIONS)}] {question}")
        run_id, answer = run_agent(question, group_id=group_id)
        print(f"  -> {answer}")
        results.append({"run_id": run_id, "task": question, "final_answer": answer})

    with open("batch_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nSaved {len(results)} results to batch_results.json (group_id: {group_id})")
    print("\nRun_ids for reference (use these to pull full traces from traces.db):")
    for r in results:
        print(f"  {r['run_id']}: {r['task'][:70]}")
