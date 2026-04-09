"""Tests for data models -- TradingDecision, Position, RiskState parsing."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.bcontroller.models import (
    Position,
    RiskState,
    TradingDecision,
    TradeRecord,
)


# --- TradingDecision.from_dict ---

def test_from_dict_valid_buy():
    data = {
        "action": "BUY",
        "pair": "BTCUSDT",
        "size_pct": 5.0,
        "confidence": 0.85,
        "reason_code": "TREND_UP",
        "reasoning": "EMA20 crossed above EMA50 with RSI at 55.",
    }
    d = TradingDecision.from_dict(data)
    assert d.action == "BUY"
    assert d.pair == "BTCUSDT"
    assert abs(d.size_pct - 5.0) < 0.001
    assert abs(d.confidence - 0.85) < 0.001
    assert d.reason_code == "TREND_UP"


def test_from_dict_valid_hold():
    data = {
        "action": "HOLD",
        "pair": "ETHUSDT",
        "size_pct": 0.0,
        "confidence": 0.3,
        "reason_code": "NEUTRAL",
        "reasoning": "No clear signal.",
    }
    d = TradingDecision.from_dict(data)
    assert d.action == "HOLD"
    assert d.is_executable() is False


def test_from_dict_missing_key_raises():
    data = {
        "action": "BUY",
        "pair": "BTCUSDT",
        # missing size_pct, confidence, reason_code, reasoning
    }
    with pytest.raises(KeyError):
        TradingDecision.from_dict(data)


def test_from_dict_size_pct_coerced_from_string():
    data = {
        "action": "SELL",
        "pair": "BTCUSDT",
        "size_pct": "7.5",
        "confidence": "0.70",
        "reason_code": "OVERBOUGHT",
        "reasoning": "RSI above 70.",
    }
    d = TradingDecision.from_dict(data)
    assert abs(d.size_pct - 7.5) < 0.001
    assert abs(d.confidence - 0.70) < 0.001


# --- is_executable ---

def test_is_executable_buy_above_threshold():
    d = TradingDecision(
        action="BUY", pair="BTCUSDT", size_pct=5.0,
        confidence=0.75, reason_code="TREND_UP", reasoning="Test",
    )
    assert d.is_executable(min_confidence=0.6) is True


def test_is_executable_buy_below_threshold():
    d = TradingDecision(
        action="BUY", pair="BTCUSDT", size_pct=5.0,
        confidence=0.5, reason_code="TREND_UP", reasoning="Test",
    )
    assert d.is_executable(min_confidence=0.6) is False


def test_is_executable_hold_always_false():
    d = TradingDecision(
        action="HOLD", pair="BTCUSDT", size_pct=0.0,
        confidence=0.9, reason_code="NEUTRAL", reasoning="Test",
    )
    assert d.is_executable() is False


def test_is_executable_default_threshold_is_06():
    d = TradingDecision(
        action="BUY", pair="BTCUSDT", size_pct=3.0,
        confidence=0.6, reason_code="OVERSOLD", reasoning="Test",
    )
    assert d.is_executable() is True


# --- to_dict / round-trip ---

def test_to_dict_round_trip():
    d = TradingDecision(
        action="SELL", pair="ETHUSDT", size_pct=10.0,
        confidence=0.82, reason_code="NEWS_NEGATIVE", reasoning="Major hack reported.",
    )
    d2 = TradingDecision.from_dict(d.to_dict())
    assert d2.action == d.action
    assert d2.pair == d.pair
    assert abs(d2.confidence - d.confidence) < 0.001


# --- Position VWAP ---

def test_position_update_vwap_single():
    now = datetime.utcnow()
    pos = Position(
        pair="BTCUSDT", quantity=0.001, vwap_entry_price=50000.0,
        open_timestamp=now, last_buy_timestamp=now, cost_basis_usdt=50.0,
    )
    pos.update_vwap(0.001, 60000.0)
    assert abs(pos.vwap_entry_price - 55000.0) < 0.01
    assert abs(pos.quantity - 0.002) < 1e-9


def test_position_unrealized_pnl_pct_profit():
    now = datetime.utcnow()
    pos = Position(
        pair="BTCUSDT", quantity=0.001, vwap_entry_price=50000.0,
        open_timestamp=now, last_buy_timestamp=now, cost_basis_usdt=50.0,
    )
    pnl_pct = pos.unrealized_pnl_pct(55000.0)
    assert abs(pnl_pct - 10.0) < 0.01


def test_position_unrealized_pnl_pct_loss():
    now = datetime.utcnow()
    pos = Position(
        pair="BTCUSDT", quantity=0.001, vwap_entry_price=50000.0,
        open_timestamp=now, last_buy_timestamp=now, cost_basis_usdt=50.0,
    )
    pnl_pct = pos.unrealized_pnl_pct(47000.0)
    assert abs(pnl_pct - (-6.0)) < 0.01


def test_position_unrealized_pnl_zero_vwap():
    now = datetime.utcnow()
    pos = Position(
        pair="BTCUSDT", quantity=0.001, vwap_entry_price=0.0,
        open_timestamp=now, last_buy_timestamp=now, cost_basis_usdt=0.0,
    )
    assert pos.unrealized_pnl_pct(50000.0) == 0.0


# --- RiskState serialisation ---

def test_risk_state_roundtrip():
    now = datetime.utcnow()
    rs = RiskState(
        level="paused",
        l2_triggered_at=now,
        l2_expires_at=now,
        l2l3_triggers_last_30d=[now.isoformat()],
    )
    restored = RiskState.from_dict(rs.to_dict())
    assert restored.level == "paused"
    assert restored.l2_triggered_at is not None
    assert len(restored.l2l3_triggers_last_30d) == 1


def test_risk_state_default_is_active():
    rs = RiskState()
    assert rs.level == "active"
    assert rs.l4_triggered_at is None
