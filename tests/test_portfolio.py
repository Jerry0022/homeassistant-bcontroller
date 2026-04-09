"""Tests for PortfolioState -- VWAP, startup recovery, savings, snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bcontroller.models import Position, PortfolioSnapshot
from custom_components.bcontroller.portfolio import PortfolioState, _MAX_SNAPSHOTS


def _make_hass():
    hass = MagicMock()
    async def _exec(fn, *a, **kw):
        return fn(*a, **kw)
    hass.async_add_executor_job = AsyncMock(side_effect=_exec)
    return hass


def _make_store(data=None):
    store = MagicMock()
    store.async_load = AsyncMock(return_value=data)
    store.async_save = AsyncMock()
    return store


async def _make_portfolio(cash=1000.0) -> PortfolioState:
    hass = _make_hass()
    with patch("custom_components.bcontroller.portfolio.Store", return_value=_make_store()):
        ps = PortfolioState(hass)
        await ps.async_load()
        ps.cash_balance = cash
    return ps


# --- VWAP calculation ---

@pytest.mark.asyncio
async def test_vwap_single_buy():
    ps = await _make_portfolio()
    await ps.open_position("BTCUSDT", 0.001, 50000.0)
    pos = ps.positions["BTCUSDT"]
    assert abs(pos.vwap_entry_price - 50000.0) < 0.01


@pytest.mark.asyncio
async def test_vwap_two_buys_equal_size():
    """DCA: two equal buys at different prices -> VWAP is average."""
    ps = await _make_portfolio(cash=2000.0)
    await ps.open_position("BTCUSDT", 0.001, 50000.0)
    await ps.open_position("BTCUSDT", 0.001, 60000.0)
    pos = ps.positions["BTCUSDT"]
    # VWAP = (0.001*50000 + 0.001*60000) / 0.002 = 55000
    assert abs(pos.vwap_entry_price - 55000.0) < 0.01
    assert abs(pos.quantity - 0.002) < 1e-9


@pytest.mark.asyncio
async def test_vwap_two_buys_unequal_size():
    """DCA: larger second buy should pull VWAP up."""
    ps = await _make_portfolio(cash=5000.0)
    await ps.open_position("BTCUSDT", 0.001, 50000.0)  # cost=50
    await ps.open_position("BTCUSDT", 0.003, 60000.0)  # cost=180
    pos = ps.positions["BTCUSDT"]
    # VWAP = (50 + 180) / 0.004 = 57500
    assert abs(pos.vwap_entry_price - 57500.0) < 0.01


# --- Close position / P&L ---

@pytest.mark.asyncio
async def test_close_full_position_calculates_pnl():
    ps = await _make_portfolio(cash=100.0)
    await ps.open_position("BTCUSDT", 0.001, 50000.0)
    pnl = await ps.close_position("BTCUSDT", 0.001, 55000.0)
    # P&L = (55000 - 50000) * 0.001 = 5 USDT
    assert abs(pnl - 5.0) < 0.001
    assert "BTCUSDT" not in ps.positions


@pytest.mark.asyncio
async def test_close_nonexistent_position_returns_zero():
    ps = await _make_portfolio()
    pnl = await ps.close_position("ETHUSDT", 0.1, 3000.0)
    assert pnl == 0.0


@pytest.mark.asyncio
async def test_partial_close_reduces_quantity():
    ps = await _make_portfolio(cash=200.0)
    await ps.open_position("BTCUSDT", 0.002, 50000.0)
    await ps.close_position("BTCUSDT", 0.001, 55000.0)
    assert "BTCUSDT" in ps.positions
    assert abs(ps.positions["BTCUSDT"].quantity - 0.001) < 1e-9


# --- Cash balance ---

@pytest.mark.asyncio
async def test_open_position_reduces_cash():
    ps = await _make_portfolio(cash=1000.0)
    await ps.open_position("BTCUSDT", 0.001, 50000.0)
    # invested = 0.001 * 50000 = 50 USDT
    assert abs(ps.cash_balance - 950.0) < 0.01


@pytest.mark.asyncio
async def test_close_position_increases_cash():
    ps = await _make_portfolio(cash=950.0)
    await ps.open_position("BTCUSDT", 0.001, 50000.0)
    cash_after_buy = ps.cash_balance
    await ps.close_position("BTCUSDT", 0.001, 55000.0)
    assert ps.cash_balance > cash_after_buy


# --- Savings rate application ---

@pytest.mark.asyncio
async def test_apply_savings_moves_cash_to_savings():
    ps = await _make_portfolio(cash=100.0)
    await ps.apply_savings(10.0)
    assert abs(ps.savings_balance - 10.0) < 0.001
    assert abs(ps.cash_balance - 90.0) < 0.001


@pytest.mark.asyncio
async def test_apply_savings_capped_at_cash_balance():
    """Cannot save more than available cash."""
    ps = await _make_portfolio(cash=5.0)
    await ps.apply_savings(100.0)
    assert ps.savings_balance <= 5.0
    assert ps.cash_balance >= 0.0


# --- Portfolio snapshots ---

@pytest.mark.asyncio
async def test_snapshot_recorded():
    ps = await _make_portfolio()
    await ps.record_snapshot(1000.0, 50000.0)
    assert len(ps.portfolio_snapshots) == 1
    assert ps.portfolio_snapshots[0].total_value_usdt == 1000.0


@pytest.mark.asyncio
async def test_snapshots_trimmed_to_max():
    ps = await _make_portfolio()
    for i in range(_MAX_SNAPSHOTS + 50):
        await ps.record_snapshot(float(1000 + i), 50000.0)
    assert len(ps.portfolio_snapshots) <= _MAX_SNAPSHOTS


@pytest.mark.asyncio
async def test_get_value_at_returns_closest():
    ps = await _make_portfolio()
    # Manually inject snapshots
    now = datetime.utcnow()
    ps.portfolio_snapshots = [
        PortfolioSnapshot(timestamp=now - timedelta(seconds=3700), total_value_usdt=1000.0, btc_price_usdt=50000.0),
        PortfolioSnapshot(timestamp=now - timedelta(seconds=1800), total_value_usdt=980.0, btc_price_usdt=49000.0),
    ]
    val = ps.get_value_at(3600)  # 1h ago
    assert val == 1000.0


@pytest.mark.asyncio
async def test_get_value_at_returns_none_no_history():
    ps = await _make_portfolio()
    val = ps.get_value_at(3600)
    assert val is None


# --- Total portfolio value ---

@pytest.mark.asyncio
async def test_get_total_value_includes_positions():
    ps = await _make_portfolio(cash=500.0)
    ps.positions["BTCUSDT"] = Position(
        pair="BTCUSDT",
        quantity=0.01,
        vwap_entry_price=50000.0,
        open_timestamp=datetime.utcnow(),
        last_buy_timestamp=datetime.utcnow(),
        cost_basis_usdt=500.0,
    )
    total = ps.get_total_value({"BTCUSDT": 55000.0})
    # 500 cash + 0.01 * 55000 = 500 + 550 = 1050
    assert abs(total - 1050.0) < 0.01


# --- Startup recovery ---

@pytest.mark.asyncio
async def test_startup_loads_persisted_state():
    persisted = {
        "cash_balance": 750.0,
        "savings_balance": 50.0,
        "positions": {},
        "portfolio_snapshots": [],
    }
    hass = _make_hass()
    store = _make_store(data=persisted)
    with patch("custom_components.bcontroller.portfolio.Store", return_value=store):
        ps = PortfolioState(hass)
        await ps.async_load()
    assert abs(ps.cash_balance - 750.0) < 0.01
    assert abs(ps.savings_balance - 50.0) < 0.01
