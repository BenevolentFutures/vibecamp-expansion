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

from vibecamp_expansion.bot_api import (
    DEFAULT_API_BASE,
    VibecampAPI,
    event_day,
    event_stars,
    is_future,
    now_local,
)
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
        f"- {e['name']} | {e.get('start_date')} {e['start_datetime'].split('T')[1][:5]} "
        f"| {e.get('location')} | {event_stars(e)} stars :: {(e.get('description') or '')[:140]}"
        for e in events
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
            "Each result line shows name | date time | venue | stars. Judge "
            "whether the returned events genuinely answer the guest's message. "
            "For 'now/next/soon' asks, the bot only has access to future and "
            "currently-running events; use CURRENT_TIME to confirm the picks are "
            "the soonest upcoming ones in chronological order — do NOT penalise it "
            "for showing later days when little is on right now (camp is quiet at "
            "some hours). For 'most popular' asks, check the stars column is high. "
            "For interests/venues, reward on-intent relevance and penalize "
            "off-topic or generic-popular filler.",
            messages=[{
                "role": "user",
                "content": f"CURRENT_TIME: {now_local().strftime('%A %Y-%m-%d %H:%M')} (Eastern)\n\n"
                f"Guest asked: {query!r}\n\nBot returned:\n{listing}",
            }],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    finally:
        await client.close()
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

        now_q = "what's happening right now?"
        pool_pop_q = "what's popular at the pool?"
        day_q = "what events are on Saturday?"

        results: dict[str, list[dict[str, Any]]] = {}
        for q in [
            next_q,
            ai_q,
            now_q,
            pool_pop_q,
            day_q,
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
        # 6. HARD RULE: no query may ever surface an event that's already over
        #    (ended more than an hour ago). This is the "only future" guarantee.
        now = now_local()
        for q, evs in results.items():
            stale = [e["name"] for e in evs if not is_future(e, now)]
            if stale:
                failures.append(f"stale (past) events shown for {q!r}: {stale}")
        # 7. "Happening right now" is ordered by start time (soonest first).
        now_evs = results[now_q]
        now_starts = [e.get("start_datetime") or "" for e in now_evs]
        if now_evs and now_starts != sorted(now_starts):
            failures.append("'happening right now' results are not in chronological order")
        # 8. "Popular at the pool" is Pool-only and ordered by stars descending.
        pp = results[pool_pop_q]
        non_pool = [e["name"] for e in pp if e.get("location") != "Pool"]
        if pp and non_pool:
            failures.append(f"'popular at the pool' returned non-Pool events: {non_pool}")
        pp_stars = [event_stars(e) for e in pp]
        if pp_stars != sorted(pp_stars, reverse=True):
            failures.append(f"'popular at the pool' not sorted by stars desc: {pp_stars}")
        # 9. A named-weekday query returns only that weekday's events (regression
        #    guard: day questions must not fall through to "soonest from now").
        day_results = results[day_q]
        wrong_day = [e["name"] for e in day_results if event_day(e) != "Saturday"]
        if not day_results:
            failures.append("'what's on Saturday' returned nothing")
        if wrong_day:
            failures.append(f"'what's on Saturday' returned non-Saturday events: {wrong_day}")

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
