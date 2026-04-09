"""Shared pytest fixtures for BController tests.

Provides:
  - mock_hass:            Minimal HA mock with async_add_executor_job, data dict, Store mock
  - mock_binance_client:  Realistic Binance API responses
  - mock_anthropic_client: Returns structured TradingDecision JSON
  - mock_coordinator:     Full coordinator with mocked subsystems
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Minimal HomeAssistant mock ──────────────────────────────────────────────

class MockStore:
    """Minimal HA helpers.storage.Store mock."""

    def __init__(self, *args, **kwargs) -> None:
        self._data: dict | None = None

    async def async_load(self) -> dict | None:
        return self._data

    async def async_save(self, data: dict) -> None:
        self._data = data


class MockHass:
    """Minimal HomeAssistant instance for tests."""

    def __init__(self) -> None:
        self.data: dict = {}
        self._loop = asyncio.get_event_loop()

    async def async_add_executor_job(self, fn, *args, **kwargs):
        """Run sync function directly in tests (no real executor)."""
        return fn(*args, **kwargs)


@pytest.fixture
def mock_hass():
    """Return a minimal HA mock."""
    return MockHass()


@pytest.fixture
def mock_store():
    """Return a fresh MockStore instance."""
    return MockStore()


# ─── Binance client mock ──────────────────────────────────────────────────────

MOCK_ACCOUNT = {
    "canTrade": True,
    "canWithdraw": False,
    "canDeposit": True,
    "balances": [
        {"asset": "USDT", "free": "1000.00", "locked": "0.00"},
        {"asset": "BTC", "free": "0.01000000", "locked": "0.00"},
        {"asset": "ETH", "free": "0.50000000", "locked": "0.00"},
    ],
}

MOCK_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00001000",
                    "maxQty": "9000.00000000",
                    "stepSize": "0.00001000",
                },
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.01000000",
                    "maxPrice": "1000000.00000000",
                    "tickSize": "0.01000000",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5.00000000",
                    "maxNotional": "9000000.00000000",
                },
            ],
        }
    ]
}

MOCK_ORDER_RESULT = {
    "orderId": 12345678,
    "symbol": "BTCUSDT",
    "status": "FILLED",
    "executedQty": "0.00050000",
    "fills": [
        {
            "price": "50000.00",
            "qty": "0.00050000",
            "commission": "0.00000050",
            "commissionAsset": "BNB",
        }
    ],
}

# One week of hourly kline stubs (100 candles)
def _make_klines(base_price: float = 50000.0, n: int = 100) -> list[list]:
    klines = []
    t = 1700000000000
    for i in range(n):
        p = base_price + i * 10
        klines.append([
            t + i * 3600000,  # open_time
            str(p),           # open
            str(p + 50),      # high
            str(p - 50),      # low
            str(p + 5),       # close
            "100.0",          # volume
            t + i * 3600000 + 3599999,  # close_time
            str(p * 100),     # quote_volume (USDT)
            150,              # trades
            "50.0",           # taker_buy_base
            str(p * 50),      # taker_buy_quote
            "0",              # ignore
        ])
    return klines


@pytest.fixture
def mock_binance_client():
    """Return a Binance client mock with realistic responses."""
    client = MagicMock()
    client.get_account = AsyncMock(return_value=MOCK_ACCOUNT)
    client.get_open_orders = AsyncMock(return_value=[])
    client.get_klines = AsyncMock(return_value=_make_klines())
    client.get_ticker_price = AsyncMock(return_value={"price": "50000.00"})
    client.get_exchange_info = AsyncMock(return_value=_make_mock_exchange_filters())
    client.place_market_order = AsyncMock(return_value=MOCK_ORDER_RESULT)
    client.place_limit_order = AsyncMock(return_value={**MOCK_ORDER_RESULT, "type": "LIMIT"})
    client.cancel_order = AsyncMock(return_value={"status": "CANCELED"})
    client.get_order_status = AsyncMock(return_value=MOCK_ORDER_RESULT)
    client.used_weight = 0
    client.weight_tracker = MagicMock()
    client.weight_tracker.is_safe.return_value = True
    client.weight_tracker.used_weight = 0
    return client


def _make_mock_exchange_filters():
    """Return an ExchangeFilters dataclass instance."""
    from custom_components.bcontroller.models import ExchangeFilters
    return ExchangeFilters(
        symbol="BTCUSDT",
        min_qty=0.00001,
        max_qty=9000.0,
        step_size=0.00001,
        min_price=0.01,
        max_price=1000000.0,
        tick_size=0.01,
        min_notional=5.0,
    )


# ─── Anthropic / Claude mock ──────────────────────────────────────────────────

def _make_claude_response(action="HOLD", pair="BTCUSDT", size_pct=0.0,
                          confidence=0.0, reason_code="NEUTRAL",
                          reasoning="Test decision") -> str:
    import json
    return json.dumps({
        "action": action,
        "pair": pair,
        "size_pct": size_pct,
        "confidence": confidence,
        "reason_code": reason_code,
        "reasoning": reasoning,
    })


class MockAnthropicResponse:
    """Minimal Anthropic messages.create() response mock."""

    def __init__(self, text: str) -> None:
        self.content = [MagicMock(text=text)]
        self.usage = MagicMock(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )


@pytest.fixture
def mock_anthropic_client():
    """Return an Anthropic client mock that returns a HOLD decision."""
    client = MagicMock()
    client.messages.create.return_value = MockAnthropicResponse(
        _make_claude_response()
    )
    return client


@pytest.fixture
def make_claude_response():
    """Factory fixture to build custom Claude response text."""
    return _make_claude_response


# ─── Portfolio state helper ────────────────────────────────────────────────────

@pytest.fixture
async def portfolio(mock_hass):
    """Return a fresh PortfolioState with mocked stores."""
    from custom_components.bcontroller.portfolio import PortfolioState

    with patch("custom_components.bcontroller.portfolio.Store", return_value=MockStore()):
        ps = PortfolioState(mock_hass)
        await ps.async_load()
        ps.cash_balance = 1000.0
        return ps


# ─── Risk manager helper ──────────────────────────────────────────────────────

@pytest.fixture
async def risk_manager(mock_hass):
    """Return a RiskManager with mocked store."""
    from custom_components.bcontroller.risk import RiskManager

    with patch("custom_components.bcontroller.risk.Store", return_value=MockStore()):
        rm = RiskManager(mock_hass)
        await rm.async_load()
        return rm
