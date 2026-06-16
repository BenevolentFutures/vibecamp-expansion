#!/usr/bin/env python3
"""Evaluation harness for the bots' free-text recommendation brain.

NOT a hermetic unit test — it hits the live REST API and the Anthropic API, so
it lives outside ``pytest`` (which must stay offline). Run it manually to prove
that different questions get genuinely different, on-intent answers:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python eval_bot.py

It checks each query two ways:
  * deterministic assertions (ordering, venue filtering, set distinctness), and
  * an LLM judge that scores relevance 1-5 and whether the picks fit the intent.

Exits non-zero if any hard check fails, so it doubles as a regression gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from vibecamp_expansion.bot_api import VibecampAPI, DEFAULT_API_BASE, event_stars
from vibecamp_expansion.bot_llm import curate

API_BASE = os.environ.get("VIBECAMP_API_BASE", DEFAULT_API_BASE)
JUDGE_MODEL = "claude-opus-4-8"


def _ids(events: list[dict[str, Any]]) -> set[str]:
    return {e["event_id"] for e in events}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


async def _judge(query: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Score relevance of a result set to the query via an independent LLM call."""
    from anthropic import AsyncAnthropic

    listing = "\n".join(
        f"- {e['name']} :: {(e.get('description') or '')[:160]}" for e in events
    ) or "(no results)"
    schema = {
        "type": "object",
        "properties": {
            "relevance": {"type": "integer", "description": "1 (off) to 5 (perfect)."},
            "fits_intent": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["relevance", "fits_intent", "reason"],
        "additionalProperties": False,
    }
    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=512,
            system="You are a strict evaluator of an event-recommendation bot. "
            "Judge whether the returned events genuinely answer the guest's "
            "message. Reward on-intent relevance; penalize generic popular "
            "events that ignore the ask.",
            messages=[{"role": "user", "content": f"Guest asked: {query!r}\n\nBot returned:\n{listing}"}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    finally:
        await client.aclose()
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(text)


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — cannot run the LLM eval.", file=sys.stderr)
        return 2

    api = VibecampAPI(API_BASE)
    failures: list[str] = []
    try:
        # The two queries from the failure report — must come back distinct.
        next_q = "hey whats the next event?"
        ai_q = "whats the events that would be best for me, interested in ai?"

        results: dict[str, list[dict[str, Any]]] = {}
        for q in [
            next_q,
            ai_q,
            "anything at the pool?",
            "find me the sea shanties",
            "what are the most popular events?",
            "I love live music and dancing",
        ]:
            curated = await curate(api, q)
            results[q] = curated["events"]
            print(f"\n### {q}")
            print(f"    framing: {curated['framing']}")
            for e in curated["events"][:6]:
                print(f"      • {e['start_date']} {e['start_datetime'].split('T')[1][:5]} "
                      f"· {e.get('location')} · {event_stars(e)}⭐ · {e['name'][:48]}")

        # --- Deterministic checks ---------------------------------------- #
        # 1. "next" is ordered by start time, ascending.
        nxt = results[next_q]
        starts = [e.get("start_datetime") or "" for e in nxt]
        if nxt and starts != sorted(starts):
            failures.append("'next event' results are not in chronological order")
        # 2. The two failure-report queries must NOT collapse to the same set.
        j = _jaccard(_ids(nxt), _ids(results[ai_q]))
        if j > 0.5:
            failures.append(f"'next' and 'ai' results too similar (Jaccard={j:.2f})")
        # 3. Pool query returns only Pool events.
        pool = results["anything at the pool?"]
        non_pool = [e["name"] for e in pool if e.get("location") != "Pool"]
        if pool and non_pool:
            failures.append(f"pool query returned non-Pool events: {non_pool}")
        # 4. Shanty query surfaces the shanty event.
        shanty = results["find me the sea shanties"]
        if not any("shant" in (e.get("name") or "").lower() for e in shanty):
            failures.append("shanty query did not surface a shanty event")
        # 5. Popular query top result is among the global top-5 by stars.
        top5 = _ids(await api.search_events(sort="stars", limit=5))
        pop = results["what are the most popular events?"]
        if pop and pop[0]["event_id"] not in top5:
            failures.append("'most popular' top result is not in the global top-5 by stars")

        # --- LLM-judge checks -------------------------------------------- #
        print("\n=== LLM judge ===")
        for q, evs in results.items():
            verdict = await _judge(q, evs)
            ok = verdict["relevance"] >= 4 and verdict["fits_intent"]
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] rel={verdict['relevance']} fits={verdict['fits_intent']} :: {q}")
            print(f"        {verdict['reason']}")
            if not ok:
                failures.append(f"judge rejected: {q} (rel={verdict['relevance']})")
    finally:
        await api.aclose()

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("ALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
