"""Tests for RiskManager -- L1-L4 cascade, state persistence, and capital protection."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bcontroller.models import Position, RiskState
from custom_components.bcontroller.risk import RiskManager, TradeBlockedError


def _make_hass():
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    return hass


def _make_store(initial_data=None):
    store = MagicMock()
    store.async_load = AsyncMock(return_value=initial_data)
    store.async_save = AsyncMock()
    return store


def _make_position(pair="BTCUSDT", qty=0.001, vwap=50000.0) -> Position:
    now = datetime.utcnow()
    return Position(
        pair=pair,
        quantity=qty,
        vwap_entry_price=vwap,
        open_timestamp=now,
        last_buy_timestamp=now,
        cost_basis_usdt=qty * vwap,
    )


async def _make_rm(**kwargs) -> RiskManager:
    hass = _make_hass()
    store = _make_store()
    with patch("custom_components.bcontroller.risk.Store", return_value=store):
        rm = RiskManager(hass, **kwargs)
        await rm.async_load()
    return rm


# --- L1 stop-loss ---

@pytest.mark.asyncio
async def test_l1_triggers_at_threshold():
    rm = await _make_rm(stop_loss_l1=-3.0)
    pos = _make_position(vwap=50000.0)
    current = 50000.0 * 0.97  # exactly -3%
    assert rm.check_stop_loss(pos, current) is True


@pytest.mark.asyncio
async def test_l1_no_trigger_below_threshold():
    rm = await _make_rm(stop_loss_l1=-3.0)
    pos = _make_position(vwap=50000.0)
    current = 50000.0 * 0.975  # -2.5%
    assert rm.check_stop_loss(pos, current) is False


@pytest.mark.asyncio
async def test_l1_no_trigger_on_gain():
    rm = await _make_rm(stop_loss_l1=-3.0)
    pos = _make_position(vwap=50000.0)
    assert rm.check_stop_loss(pos, 52000.0) is False


@pytest.mark.asyncio
async def test_l1_hard_minimum_clamped():
    rm = await _make_rm(stop_loss_l1=-1.0)
    assert rm.stop_loss_l1 == -2.0


# --- L2 portfolio pause ---

@pytest.mark.asyncio
async def test_l2_triggers_on_5pct_1h_drawdown():
    rm = await _make_rm(stop_loss_l2=-5.0)
    triggered = await rm.check_portfolio_drawdown(950.0, 1000.0, None)
    assert triggered == "L2"
    assert rm._state.level == "paused"


@pytest.mark.asyncio
async def test_l2_no_trigger_on_small_drop():
    rm = await _make_rm(stop_loss_l2=-5.0)
    triggered = await rm.check_portfolio_drawdown(960.0, 1000.0, None)
    assert triggered is None
    assert rm._state.level == "active"


@pytest.mark.asyncio
async def test_l2_blocks_trading():
    rm = await _make_rm(stop_loss_l2=-5.0)
    await rm.check_portfolio_drawdown(950.0, 1000.0, None)
    with pytest.raises(TradeBlockedError) as exc:
        await rm.check_trade_allowed()
    assert exc.value.level == "L2"


@pytest.mark.asyncio
async def test_l2_expires_automatically():
    rm = await _make_rm(stop_loss_l2=-5.0)
    await rm.check_portfolio_drawdown(950.0, 1000.0, None)
    rm._state.l2_expires_at = datetime.utcnow() - timedelta(seconds=1)
    await rm.check_trade_allowed()  # must not raise
    assert rm._state.level == "active"


# --- L3 portfolio halt ---

@pytest.mark.asyncio
async def test_l3_triggers_on_8pct_24h_drawdown():
    rm = await _make_rm(stop_loss_l3=-8.0)
    triggered = await rm.check_portfolio_drawdown(920.0, None, 1000.0)
    assert triggered == "L3"
    assert rm._state.level == "halted"


@pytest.mark.asyncio
async def test_l3_blocks_trading():
    rm = await _make_rm(stop_loss_l3=-8.0)
    await rm.check_portfolio_drawdown(920.0, None, 1000.0)
    with pytest.raises(TradeBlockedError) as exc:
        await rm.check_trade_allowed()
    assert exc.value.level == "L3"


# --- L4 full stop ---

@pytest.mark.asyncio
async def test_l4_triggers_on_15pct_total_drawdown():
    rm = await _make_rm(stop_loss_l4=-15.0)
    result = await rm.check_total_drawdown(850.0, 1000.0)
    assert result is True
    assert rm._state.level == "stopped"


@pytest.mark.asyncio
async def test_l4_blocks_trading_permanently():
    rm = await _make_rm(stop_loss_l4=-15.0)
    await rm.check_total_drawdown(850.0, 1000.0)
    with pytest.raises(TradeBlockedError) as exc:
        await rm.check_trade_allowed()
    assert exc.value.level == "L4"


@pytest.mark.asyncio
async def test_l4_hard_minimum_clamped():
    rm = await _make_rm(stop_loss_l4=-5.0)
    assert rm.stop_loss_l4 == -10.0


@pytest.mark.asyncio
async def test_l4_manual_reset_clears_stop():
    rm = await _make_rm(stop_loss_l4=-15.0)
    await rm.check_total_drawdown(850.0, 1000.0)
    await rm.manual_reset_l4()
    assert rm._state.level == "active"
    assert rm._state.l4_triggered_at is None


# --- Cascade escalation ---

@pytest.mark.asyncio
async def test_l2_active_l3_condition_escalates():
    rm = await _make_rm(stop_loss_l2=-5.0, stop_loss_l3=-8.0)
    await rm.check_portfolio_drawdown(950.0, 1000.0, None)
    assert rm._state.level == "paused"
    await rm.check_portfolio_drawdown(920.0, None, 1000.0)
    assert rm._state.level == "halted"
    assert rm._state.l2_triggered_at is None
    assert rm._state.l2_expires_at is None


# --- State persistence ---

@pytest.mark.asyncio
async def test_l4_state_persists_across_restart():
    hass = _make_hass()
    l4_state = RiskState(
        level="stopped",
        l4_triggered_at=datetime.utcnow(),
    ).to_dict()
    store = _make_store(initial_data=l4_state)
    with patch("custom_components.bcontroller.risk.Store", return_value=store):
        rm = RiskManager(hass)
        await rm.async_load()
    assert rm._state.level == "stopped"
    with pytest.raises(TradeBlockedError) as exc:
        await rm.check_trade_allowed()
    assert exc.value.level == "L4"


# --- Cash reserve (R8) ---

@pytest.mark.asyncio
async def test_cash_reserve_healthy():
    rm = await _make_rm(cash_reserve_pct=20.0)
    assert rm.check_cash_reserve(250.0, 1000.0) is True


@pytest.mark.asyncio
async def test_cash_reserve_below_threshold():
    rm = await _make_rm(cash_reserve_pct=20.0)
    assert rm.check_cash_reserve(150.0, 1000.0) is False


@pytest.mark.asyncio
async def test_cash_reserve_exactly_at_threshold():
    rm = await _make_rm(cash_reserve_pct=20.0)
    assert rm.check_cash_reserve(200.0, 1000.0) is True


# --- Savings rate (R7) ---

@pytest.mark.asyncio
async def test_savings_rate_10pct():
    rm = await _make_rm(savings_rate=10.0)
    assert abs(rm.apply_savings_rate(50.0) - 5.0) < 0.001


@pytest.mark.asyncio
async def test_savings_rate_zero_on_loss():
    rm = await _make_rm(savings_rate=10.0)
    assert rm.apply_savings_rate(-20.0) == 0.0


@pytest.mark.asyncio
async def test_savings_rate_clamped_to_20pct():
    rm = await _make_rm(savings_rate=25.0)
    assert abs(rm.apply_savings_rate(100.0) - 20.0) < 0.001


# --- Budget gate ---

@pytest.mark.asyncio
async def test_budget_exhausted_blocks_trading():
    rm = await _make_rm()
    rm.set_budget_exhausted(True)
    with pytest.raises(TradeBlockedError) as exc:
        await rm.check_trade_allowed()
    assert exc.value.level == "budget"


@pytest.mark.asyncio
async def test_budget_not_exhausted_allows_trading():
    rm = await _make_rm()
    rm.set_budget_exhausted(False)
    await rm.check_trade_allowed()  # must not raise
