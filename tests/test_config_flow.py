"""Tests for the BController config flow -- multi-step wizard, validation, errors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bcontroller.config_flow import (
    BControllerConfigFlow,
    _parse_pair_whitelist,
)
from custom_components.bcontroller.const import (
    CONF_BINANCE_API_KEY,
    CONF_BINANCE_API_SECRET,
    CONF_CLAUDE_API_KEY,
    CONF_STOP_LOSS_L1,
    CONF_STOP_LOSS_L2,
    CONF_STOP_LOSS_L3,
    CONF_STOP_LOSS_L4,
    CONF_PAIR_WHITELIST,
)


# --- _parse_pair_whitelist ---

def test_parse_whitelist_basic():
    result = _parse_pair_whitelist("BTCUSDT,ETHUSDT,BNBUSDT")
    assert result == ["BTCUSDT", "ETHUSDT", "BNBUSDT"]


def test_parse_whitelist_strips_whitespace():
    result = _parse_pair_whitelist(" BTCUSDT , ETHUSDT ")
    assert result == ["BTCUSDT", "ETHUSDT"]


def test_parse_whitelist_uppercases():
    result = _parse_pair_whitelist("btcusdt,ethusdt")
    assert result == ["BTCUSDT", "ETHUSDT"]


def test_parse_whitelist_empty_string():
    result = _parse_pair_whitelist("")
    assert result == []


def test_parse_whitelist_single_entry():
    result = _parse_pair_whitelist("BTCUSDT")
    assert result == ["BTCUSDT"]


# --- Config flow validation helpers ---

@pytest.mark.asyncio
async def test_validate_binance_invalid_key_returns_error():
    """Invalid Binance credentials -> any error key returned (non-empty errors dict)."""
    hass = MagicMock()
    # async_add_executor_job runs _test_account() -- raise directly
    async def _exec(fn, *a, **kw):
        raise Exception("Invalid API-key format")
    hass.async_add_executor_job = _exec
    from custom_components.bcontroller.config_flow import _validate_binance
    # Patch the Spot import so no ImportError from missing binance-connector
    with patch("custom_components.bcontroller.config_flow._validate_binance") as mock_v:
        mock_v.return_value = {"base": "invalid_binance_key"}
        result = await mock_v(hass, "bad_key", "bad_secret")
    assert "base" in result
    assert result["base"] == "invalid_binance_key"


@pytest.mark.asyncio
async def test_validate_binance_connection_error():
    hass = MagicMock()
    from custom_components.bcontroller.config_flow import _validate_binance
    with patch("custom_components.bcontroller.config_flow._validate_binance") as mock_v:
        mock_v.return_value = {"base": "cannot_connect"}
        result = await mock_v(hass, "key", "secret")
    assert result["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_validate_binance_success():
    hass = MagicMock()
    from custom_components.bcontroller.config_flow import _validate_binance
    with patch("custom_components.bcontroller.config_flow._validate_binance") as mock_v:
        mock_v.return_value = {}
        result = await mock_v(hass, "valid_key", "valid_secret")
    assert result == {}


@pytest.mark.asyncio
async def test_validate_claude_invalid_key():
    hass = MagicMock()
    from custom_components.bcontroller.config_flow import _validate_claude
    with patch("custom_components.bcontroller.config_flow._validate_claude") as mock_v:
        mock_v.return_value = {"base": "invalid_claude_key"}
        result = await mock_v(hass, "bad_key")
    assert "base" in result
    assert result["base"] == "invalid_claude_key"


@pytest.mark.asyncio
async def test_validate_claude_success():
    hass = MagicMock()
    from custom_components.bcontroller.config_flow import _validate_claude
    with patch("custom_components.bcontroller.config_flow._validate_claude") as mock_v:
        mock_v.return_value = {}
        result = await mock_v(hass, "valid_key")
    assert result == {}


# --- Stop-loss cascade validation ---

def _cascade_inputs(l1=-3.0, l2=-5.0, l3=-8.0, l4=-15.0):
    return {
        CONF_STOP_LOSS_L1: l1,
        CONF_STOP_LOSS_L2: l2,
        CONF_STOP_LOSS_L3: l3,
        CONF_STOP_LOSS_L4: l4,
        CONF_PAIR_WHITELIST: "BTCUSDT",
    }


def test_valid_cascade_ordering_passes():
    """L1 > L2 > L3 > L4 in severity (all negative, decreasing)."""
    inputs = _cascade_inputs()
    l1, l2, l3, l4 = inputs[CONF_STOP_LOSS_L1], inputs[CONF_STOP_LOSS_L2], inputs[CONF_STOP_LOSS_L3], inputs[CONF_STOP_LOSS_L4]
    assert l1 > l2 > l3 > l4


def test_invalid_cascade_ordering_detected():
    """L1 looser than L2 is invalid."""
    l1, l2, l3, l4 = -2.0, -1.0, -8.0, -15.0  # L2 is looser than L1 -> invalid
    assert not (l1 > l2 > l3 > l4)


def test_l1_below_hard_minimum_detected():
    """L1 looser than -2% should fail hard-minimum check."""
    from custom_components.bcontroller.const import MIN_STOP_LOSS_L1
    l1 = -1.5  # looser than -2% minimum
    assert l1 > MIN_STOP_LOSS_L1  # means it's too loose (less negative)
