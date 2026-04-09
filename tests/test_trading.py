"""Tests for the Binance trading engine.

Coverage:
  - LOT_SIZE stepSize rounding
  - PRICE_FILTER tickSize rounding
  - Order rejected when below NOTIONAL minimum
  - Weight tracking blocks requests when approaching 3000
  - Market order placement with correct parameters
  - Limit order placement with correct parameters
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bcontroller.models import ExchangeFilters
from custom_components.bcontroller.trading import (
    BinanceClient,
    BinanceRateLimitError,
    BinanceWeightTracker,
    _round_step,
    _round_tick,
)


def _make_hass():
    hass = MagicMock()
    async def _exec(fn, *a, **kw):
        return fn(*a, **kw)
    hass.async_add_executor_job = AsyncMock(side_effect=_exec)
    return hass


def _make_filters(step_size=0.00001, tick_size=0.01, min_notional=5.0) -> ExchangeFilters:
    return ExchangeFilters(
        symbol="BTCUSDT",
        min_qty=0.00001,
        max_qty=9000.0,
        step_size=step_size,
        min_price=0.01,
        max_price=1000000.0,
        tick_size=tick_size,
        min_notional=min_notional,
    )


# --- Rounding helpers ---

def test_round_step_standard():
    """0.000123456 rounded down to stepSize=0.00001 gives 0.00012."""
    result = _round_step(0.000123456, 0.00001)
    assert abs(result - 0.00012) < 1e-9


def test_round_step_exact_multiple():
    result = _round_step(0.00050, 0.00001)
    assert abs(result - 0.00050) < 1e-9


def test_round_step_truncates_not_rounds():
    """Must round DOWN, not nearest."""
    result = _round_step(0.000129999, 0.00001)
    assert abs(result - 0.00012) < 1e-9


def test_round_tick_standard():
    """50001.234 rounded down to tickSize=0.01 gives 50001.23."""
    result = _round_tick(50001.234, 0.01)
    assert abs(result - 50001.23) < 1e-6


def test_round_tick_exact():
    result = _round_tick(50000.00, 0.01)
    assert abs(result - 50000.00) < 1e-6


def test_round_step_zero_step_returns_value():
    """Zero step should return value unchanged."""
    assert _round_step(1.5, 0.0) == 1.5


def test_round_tick_zero_tick_returns_value():
    assert _round_tick(1.5, 0.0) == 1.5


# --- Weight tracker ---

def test_weight_tracker_safe_initially():
    wt = BinanceWeightTracker(safe_limit=3000)
    assert wt.is_safe() is True


def test_weight_tracker_updates_from_header():
    wt = BinanceWeightTracker(safe_limit=3000)
    wt.update("2999")
    assert wt.used_weight == 2999
    assert wt.is_safe() is True


def test_weight_tracker_unsafe_at_limit():
    wt = BinanceWeightTracker(safe_limit=3000)
    wt.update("3000")
    assert wt.is_safe() is False


def test_weight_tracker_blocks_call_when_unsafe():
    """BinanceClient._call raises RateLimitError when weight is unsafe."""
    hass = _make_hass()
    client = BinanceClient(hass, "key", "secret")
    client._weight_tracker.update("3000")  # at limit

    async def _run():
        with pytest.raises(BinanceRateLimitError):
            await client._call(lambda: None)

    asyncio.get_event_loop().run_until_complete(_run())


# --- Market order ---

@pytest.mark.asyncio
async def test_place_market_order_buy_correct_params():
    """Market BUY order uses rounded qty and correct side."""
    hass = _make_hass()
    client = BinanceClient(hass, "key", "secret")

    filters = _make_filters(step_size=0.00001, min_notional=5.0)
    client.get_exchange_info = AsyncMock(return_value=filters)
    client._estimate_slippage = AsyncMock(return_value=0.1)  # under 1% threshold

    mock_sdk = MagicMock()
    mock_sdk.new_order.return_value = {
        "orderId": 99,
        "executedQty": "0.00050",
        "fills": [{"price": "50000.00", "commission": "0", "commissionAsset": "BNB"}],
    }
    client._get_client = MagicMock(return_value=mock_sdk)

    result = await client.place_market_order("BTCUSDT", "BUY", 0.000503, estimated_price=50000.0)

    call_kwargs = mock_sdk.new_order.call_args
    assert call_kwargs.kwargs["side"] == "BUY"
    assert call_kwargs.kwargs["type"] == "MARKET"
    # Quantity should be rounded down to stepSize
    assert abs(call_kwargs.kwargs["quantity"] - 0.00050) < 1e-9


@pytest.mark.asyncio
async def test_place_market_order_rejected_below_notional():
    """Market order rejected when qty * price < minNotional."""
    hass = _make_hass()
    client = BinanceClient(hass, "key", "secret")

    filters = _make_filters(step_size=0.00001, min_notional=5.0)
    client.get_exchange_info = AsyncMock(return_value=filters)

    with pytest.raises(ValueError, match="notional"):
        await client.place_market_order("BTCUSDT", "BUY", 0.00001, estimated_price=100.0)
        # 0.00001 * 100 = 0.001 USDT < 5 USDT minimum


# --- Limit order ---

@pytest.mark.asyncio
async def test_place_limit_order_correct_params():
    """Limit order uses rounded price and GTC timeInForce."""
    hass = _make_hass()
    client = BinanceClient(hass, "key", "secret")

    filters = _make_filters(step_size=0.00001, tick_size=0.01, min_notional=5.0)
    client.get_exchange_info = AsyncMock(return_value=filters)

    mock_sdk = MagicMock()
    mock_sdk.new_order.return_value = {"orderId": 100, "executedQty": "0.00100"}
    client._get_client = MagicMock(return_value=mock_sdk)

    await client.place_limit_order("BTCUSDT", "BUY", 0.00100, price=49999.999)

    call_kwargs = mock_sdk.new_order.call_args.kwargs
    assert call_kwargs["type"] == "LIMIT"
    assert call_kwargs["timeInForce"] == "GTC"
    # Price must be rounded to tickSize
    assert call_kwargs["price"] == "49999.99"


@pytest.mark.asyncio
async def test_place_limit_order_rejected_below_notional():
    hass = _make_hass()
    client = BinanceClient(hass, "key", "secret")

    filters = _make_filters(step_size=0.00001, tick_size=0.01, min_notional=5.0)
    client.get_exchange_info = AsyncMock(return_value=filters)

    with pytest.raises(ValueError, match="notional"):
        await client.place_limit_order("BTCUSDT", "BUY", 0.00001, price=100.0)


# --- Exchange info caching ---

@pytest.mark.asyncio
async def test_exchange_info_cached():
    """Second call uses cache — SDK not called twice."""
    hass = _make_hass()
    client = BinanceClient(hass, "key", "secret")

    raw_response = {
        "symbols": [{
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
                {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
                {"filterType": "NOTIONAL", "minNotional": "5.0", "maxNotional": "9000000"},
            ]
        }]
    }

    mock_sdk = MagicMock()
    mock_sdk.exchange_info.return_value = raw_response
    client._get_client = MagicMock(return_value=mock_sdk)
    # Override weight tracker so _call doesn't block
    client._weight_tracker._used_weight = 0

    # Override _call to call the sync function directly for test simplicity
    async def _mock_call(fn, *a, **kw):
        return fn(*a, **kw)
    client._call = _mock_call

    f1 = await client.get_exchange_info("BTCUSDT")
    f2 = await client.get_exchange_info("BTCUSDT")
    assert f1 is f2  # same cached instance
    assert mock_sdk.exchange_info.call_count == 1
