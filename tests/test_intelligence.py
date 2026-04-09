"""Tests for the intelligence module -- source weights and Claude integration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bcontroller.intelligence import (
    IntelligenceEngine,
    _DEFAULT_WEIGHTS,
    _infer_weights_from_data,
    _normalise_weights,
)
from custom_components.bcontroller.models import TradingDecision


# --- _normalise_weights ---

def test_weights_sum_to_100():
    weights = {"news": 30.0, "trend": 30.0, "sentiment": 30.0, "history": 30.0}
    result = _normalise_weights(weights)
    assert abs(sum(result.values()) - 100.0) < 0.05


def test_weights_sum_to_100_unequal_input():
    weights = {"news": 50.0, "trend": 10.0, "sentiment": 25.0, "history": 15.0}
    result = _normalise_weights(weights)
    assert abs(sum(result.values()) - 100.0) < 0.05


def test_weights_clamped_to_min_10():
    weights = {"news": 5.0, "trend": 50.0, "sentiment": 40.0, "history": 5.0}
    result = _normalise_weights(weights)
    for v in result.values():
        assert v >= 9.99, f"Weight {v} below minimum"


def test_weights_clamped_to_max_40():
    weights = {"news": 70.0, "trend": 10.0, "sentiment": 10.0, "history": 10.0}
    result = _normalise_weights(weights)
    for v in result.values():
        assert v <= 40.01, f"Weight {v} above maximum"


def test_weights_all_sources_present():
    weights = {"news": 25.0, "trend": 25.0, "sentiment": 25.0, "history": 25.0}
    result = _normalise_weights(weights)
    assert set(result.keys()) == {"news", "trend", "sentiment", "history"}


# --- _infer_weights_from_data ---

def test_sparse_news_pushes_news_weight_to_minimum():
    """0 headlines: news weight should be pushed to minimum (10%)."""
    result = _infer_weights_from_data({}, [], 50)
    assert result["news"] <= 15.0


def test_single_headline_reduces_news_weight():
    """1 headline: news weight reduced toward minimum."""
    result = _infer_weights_from_data({}, ["BTC rises"], 50)
    assert result["news"] <= 20.0


def test_extreme_rsi_boosts_trend_weight():
    """RSI at 20 (extreme low): trend weight should be boosted."""
    result_extreme = _infer_weights_from_data({"rsi": 20.0}, [], 50)
    result_normal = _infer_weights_from_data({"rsi": 50.0}, [], 50)
    assert result_extreme["trend"] > result_normal["trend"]


def test_extreme_fg_boosts_sentiment_weight():
    """Fear & Greed at 10 (extreme fear): sentiment weight boosted."""
    result_extreme = _infer_weights_from_data({}, [], 10)
    result_neutral = _infer_weights_from_data({}, [], 50)
    assert result_extreme["sentiment"] > result_neutral["sentiment"]


def test_all_weights_valid_range_with_data():
    """Weights computed with real data stay in [10, 40] range."""
    indicators = {"rsi": 28.0, "macd": -100.0}
    headlines = ["Bitcoin drops", "Ethereum dips", "Crypto sell-off"]
    result = _infer_weights_from_data(indicators, headlines, 15)
    for k, v in result.items():
        assert 9.99 <= v <= 40.01, f"Weight {k}={v} out of range"
    assert abs(sum(result.values()) - 100.0) < 0.05


# --- IntelligenceEngine ---

def _make_hass():
    hass = MagicMock()
    async def _exec(fn, *a, **kw):
        return fn(*a, **kw)
    hass.async_add_executor_job = AsyncMock(side_effect=_exec)
    return hass


def _make_claude_client(action="HOLD", confidence=0.0):
    from custom_components.bcontroller.models import TradingDecision
    decision = TradingDecision(
        action=action,
        pair="BTCUSDT",
        size_pct=0.0,
        confidence=confidence,
        reason_code="NEUTRAL",
        reasoning="Test",
    )
    client = MagicMock()
    client.get_trading_decision = AsyncMock(return_value=decision)
    client.budget_remaining = 5.0
    return client


def _make_market_data() -> dict:
    return {
        "pair": "BTCUSDT",
        "price_map": {"BTCUSDT": 50000.0},
        "klines": [],
        "usdt_balance": 1000.0,
        "positions": {},
        "portfolio_value": 1000.0,
    }


@pytest.mark.asyncio
async def test_conservative_hold_on_budget_exhausted():
    """BudgetExhaustedError in Claude client returns conservative HOLD."""
    from custom_components.bcontroller.claude_client import BudgetExhaustedError

    hass = _make_hass()
    claude = MagicMock()
    claude.get_trading_decision = AsyncMock(side_effect=BudgetExhaustedError("exhausted"))
    claude.budget_remaining = 0.0

    engine = IntelligenceEngine(hass, claude, {})
    engine._data_sources = MagicMock()
    engine._data_sources.get_news_headlines = AsyncMock(return_value=[])
    engine._data_sources.get_fear_greed = AsyncMock(return_value=(50, "Neutral"))
    engine._data_sources.compute_technical_indicators = AsyncMock(return_value={"rsi": 50.0})
    engine._data_sources.extract_ohlcv_stats = MagicMock(return_value={"current_price": 50000.0})

    decision, weights = await engine.analyze_and_decide(_make_market_data())
    assert decision.action == "HOLD"
    assert decision.confidence == 0.0


@pytest.mark.asyncio
async def test_conservative_hold_on_claude_api_error():
    """ClaudeAPIError returns conservative HOLD (R14)."""
    from custom_components.bcontroller.claude_client import ClaudeAPIError

    hass = _make_hass()
    claude = MagicMock()
    claude.get_trading_decision = AsyncMock(side_effect=ClaudeAPIError("network error"))
    claude.budget_remaining = 5.0

    engine = IntelligenceEngine(hass, claude, {})
    engine._data_sources = MagicMock()
    engine._data_sources.get_news_headlines = AsyncMock(return_value=[])
    engine._data_sources.get_fear_greed = AsyncMock(return_value=(50, "Neutral"))
    engine._data_sources.compute_technical_indicators = AsyncMock(return_value={"rsi": 50.0})
    engine._data_sources.extract_ohlcv_stats = MagicMock(return_value={"current_price": 50000.0})

    decision, weights = await engine.analyze_and_decide(_make_market_data())
    assert decision.action == "HOLD"


@pytest.mark.asyncio
async def test_analyze_and_decide_returns_weights_summing_to_100():
    """Normal cycle returns weights summing to 100%."""
    hass = _make_hass()
    claude = _make_claude_client(action="HOLD")

    engine = IntelligenceEngine(hass, claude, {})
    engine._data_sources = MagicMock()
    engine._data_sources.get_news_headlines = AsyncMock(return_value=["Headline 1", "Headline 2", "Headline 3"])
    engine._data_sources.get_fear_greed = AsyncMock(return_value=(50, "Neutral"))
    engine._data_sources.compute_technical_indicators = AsyncMock(return_value={"rsi": 50.0})
    engine._data_sources.extract_ohlcv_stats = MagicMock(return_value={"current_price": 50000.0})

    decision, weights = await engine.analyze_and_decide(_make_market_data())
    assert abs(sum(weights.values()) - 100.0) < 0.1
