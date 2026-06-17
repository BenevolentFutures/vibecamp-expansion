"""Hermetic tests for the concierge's cost estimate and model switching.

No network, no LLM — just the pure pricing math and the runtime model setter.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vibecamp_expansion import bot_llm


def test_usage_cost_sonnet() -> None:
    # Sonnet 4.6: $3/$15/$3.75/$0.30 per 1M (in/out/cache_w/cache_r).
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=1000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    cost = bot_llm.usage_cost("claude-sonnet-4-6", usage)
    assert cost == pytest.approx((1000 * 3 + 1000 * 15) / 1_000_000)


def test_usage_cost_uses_cache_rates() -> None:
    usage = SimpleNamespace(
        input_tokens=0,
        output_tokens=500,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=20_000,
    )
    cost = bot_llm.usage_cost("claude-sonnet-4-6", usage)
    expected = (20_000 * 0.30 + 500 * 15) / 1_000_000
    assert cost == pytest.approx(expected)


def test_usage_cost_unknown_model_is_zero() -> None:
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=1000,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    assert bot_llm.usage_cost("some-unknown-model", usage) == 0.0
    assert bot_llm.usage_cost("claude-sonnet-4-6", None) == 0.0


def test_set_model_aliases_and_validation() -> None:
    original = bot_llm.MODEL
    try:
        assert bot_llm.set_model("haiku") == "claude-haiku-4-5"
        assert bot_llm.MODEL == "claude-haiku-4-5"
        assert bot_llm.set_model("opus") == "claude-opus-4-8"
        assert bot_llm.set_model("claude-sonnet-4-6") == "claude-sonnet-4-6"
        with pytest.raises(ValueError):
            bot_llm.set_model("gpt-4")
        # Failed switch leaves the prior model intact.
        assert bot_llm.MODEL == "claude-sonnet-4-6"
    finally:
        bot_llm.MODEL = original
