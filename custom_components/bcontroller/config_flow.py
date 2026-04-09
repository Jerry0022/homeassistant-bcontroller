"""Config flow for BController — multi-step UI wizard.

Step 1: Binance API Key + Secret  (validated via GET /api/v3/account)
Step 2: Claude API Key            (validated via messages.create test call)
Step 3: Risk parameters           (stop-loss levels, savings rate, poll interval)

Options flow: allows updating risk parameters and poll interval post-setup.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BINANCE_API_KEY,
    CONF_BINANCE_API_SECRET,
    CONF_CASH_RESERVE_PCT,
    CONF_CLAUDE_API_KEY,
    CONF_CLAUDE_BUDGET_USD,
    CONF_MAX_TRADE_USDT,
    CONF_PAIR_WHITELIST,
    CONF_POLL_INTERVAL,
    CONF_SAVINGS_RATE,
    CONF_STOP_LOSS_L1,
    CONF_STOP_LOSS_L2,
    CONF_STOP_LOSS_L3,
    CONF_STOP_LOSS_L4,
    DEFAULT_CASH_RESERVE_PCT,
    DEFAULT_CLAUDE_BUDGET_USD,
    DEFAULT_MAX_TRADE_USDT,
    DEFAULT_PAIR_WHITELIST,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SAVINGS_RATE,
    DEFAULT_STOP_LOSS_L1,
    DEFAULT_STOP_LOSS_L2,
    DEFAULT_STOP_LOSS_L3,
    DEFAULT_STOP_LOSS_L4,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MAX_SAVINGS_RATE,
    MIN_POLL_INTERVAL,
    MIN_STOP_LOSS_L1,
    MIN_STOP_LOSS_L4,
    MODEL_ID,
)

_LOGGER = logging.getLogger(__name__)

# ─── Step schemas ─────────────────────────────────────────────────────────────

STEP_BINANCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BINANCE_API_KEY): str,
        vol.Required(CONF_BINANCE_API_SECRET): str,
    }
)

STEP_CLAUDE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLAUDE_API_KEY): str,
        vol.Optional(CONF_CLAUDE_BUDGET_USD, default=DEFAULT_CLAUDE_BUDGET_USD): vol.All(
            vol.Coerce(float), vol.Range(min=1.0, max=100.0)
        ),
    }
)


def _risk_schema(
    stop_loss_l1: float = DEFAULT_STOP_LOSS_L1,
    stop_loss_l2: float = DEFAULT_STOP_LOSS_L2,
    stop_loss_l3: float = DEFAULT_STOP_LOSS_L3,
    stop_loss_l4: float = DEFAULT_STOP_LOSS_L4,
    savings_rate: float = DEFAULT_SAVINGS_RATE,
    cash_reserve_pct: float = DEFAULT_CASH_RESERVE_PCT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    max_trade_usdt: float = DEFAULT_MAX_TRADE_USDT,
    pair_whitelist: str = ",".join(DEFAULT_PAIR_WHITELIST),
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_STOP_LOSS_L1, default=stop_loss_l1): vol.All(
                vol.Coerce(float), vol.Range(max=MIN_STOP_LOSS_L1)
            ),
            vol.Optional(CONF_STOP_LOSS_L2, default=stop_loss_l2): vol.All(
                vol.Coerce(float), vol.Range(max=-1.0)
            ),
            vol.Optional(CONF_STOP_LOSS_L3, default=stop_loss_l3): vol.All(
                vol.Coerce(float), vol.Range(max=-1.0)
            ),
            vol.Optional(CONF_STOP_LOSS_L4, default=stop_loss_l4): vol.All(
                vol.Coerce(float), vol.Range(max=MIN_STOP_LOSS_L4)
            ),
            vol.Optional(CONF_SAVINGS_RATE, default=savings_rate): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=MAX_SAVINGS_RATE)
            ),
            vol.Optional(CONF_CASH_RESERVE_PCT, default=cash_reserve_pct): vol.All(
                vol.Coerce(float), vol.Range(min=5.0, max=50.0)
            ),
            vol.Optional(CONF_POLL_INTERVAL, default=poll_interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
            ),
            vol.Optional(CONF_MAX_TRADE_USDT, default=max_trade_usdt): vol.All(
                vol.Coerce(float), vol.Range(min=5.0, max=10000.0)
            ),
            vol.Optional(CONF_PAIR_WHITELIST, default=pair_whitelist): str,
        }
    )


# ─── Validators ───────────────────────────────────────────────────────────────

async def _validate_binance(
    hass, api_key: str, api_secret: str
) -> dict[str, str]:
    """Validate Binance credentials by calling GET /api/v3/account.

    Returns empty dict on success, or {"base": error_key} on failure.
    """
    try:
        from binance.spot import Spot  # type: ignore[import]
        from binance.error import ClientError  # type: ignore[import]

        def _test_account():
            client = Spot(api_key=api_key, api_secret=api_secret)
            return client.account()

        account = await hass.async_add_executor_job(_test_account)

        # Warn but do not block if withdrawal permission is enabled
        if account.get("canWithdraw", False):
            _LOGGER.warning(
                "Binance API key has withdrawal permission. "
                "This is a security risk. Disable it in Binance API Management."
            )
        return {}

    except Exception as exc:
        exc_str = str(exc).lower()
        if "invalid api-key" in exc_str or "api-key" in exc_str:
            return {"base": "invalid_binance_key"}
        if "signature" in exc_str:
            return {"base": "invalid_binance_secret"}
        if "connection" in exc_str or "timeout" in exc_str:
            return {"base": "cannot_connect"}
        _LOGGER.error("Binance validation error: %s", exc)
        return {"base": "binance_unknown_error"}


async def _validate_claude(hass, api_key: str) -> dict[str, str]:
    """Validate Claude API key by making a minimal messages.create call."""
    try:
        import anthropic  # type: ignore[import]

        def _test_claude():
            client = anthropic.Anthropic(api_key=api_key)
            # Minimal test call — 1 token in, 1 token out
            response = client.messages.create(
                model=MODEL_ID,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return response

        await hass.async_add_executor_job(_test_claude)
        return {}

    except Exception as exc:
        exc_str = str(exc).lower()
        if "authentication" in exc_str or "api key" in exc_str or "401" in exc_str:
            return {"base": "invalid_claude_key"}
        if "connection" in exc_str or "timeout" in exc_str:
            return {"base": "cannot_connect"}
        _LOGGER.error("Claude validation error: %s", exc)
        return {"base": "claude_unknown_error"}


def _parse_pair_whitelist(raw: str) -> list[str]:
    """Parse comma-separated pair string into a clean list."""
    return [p.strip().upper() for p in raw.split(",") if p.strip()]


# ─── Config flow ──────────────────────────────────────────────────────────────

class BControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle BController setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Binance credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _validate_binance(
                self.hass,
                user_input[CONF_BINANCE_API_KEY],
                user_input[CONF_BINANCE_API_SECRET],
            )
            if not errors:
                self._data.update(user_input)
                return await self.async_step_claude()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_BINANCE_SCHEMA,
            errors=errors,
            description_placeholders={
                "setup_guide": (
                    "1. binance.com → API Management\n"
                    "2. Create key with label 'BController'\n"
                    "3. Enable 'Spot Trading' ONLY\n"
                    "4. Disable 'Enable Withdrawals'\n"
                    "5. Restrict to this server's IP (recommended)"
                )
            },
        )

    async def async_step_claude(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Claude API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _validate_claude(
                self.hass, user_input[CONF_CLAUDE_API_KEY]
            )
            if not errors:
                self._data.update(user_input)
                return await self.async_step_risk()

        return self.async_show_form(
            step_id="claude",
            data_schema=STEP_CLAUDE_SCHEMA,
            errors=errors,
            description_placeholders={
                "model_id": MODEL_ID,
                "estimated_cost": "~$10/month at default 120s polling",
            },
        )

    async def async_step_risk(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Risk parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate cascade ordering (L1 > L2 > L3 > L4 in severity)
            l1 = user_input.get(CONF_STOP_LOSS_L1, DEFAULT_STOP_LOSS_L1)
            l2 = user_input.get(CONF_STOP_LOSS_L2, DEFAULT_STOP_LOSS_L2)
            l3 = user_input.get(CONF_STOP_LOSS_L3, DEFAULT_STOP_LOSS_L3)
            l4 = user_input.get(CONF_STOP_LOSS_L4, DEFAULT_STOP_LOSS_L4)

            if not (l1 > l2 > l3 > l4):
                errors["base"] = "invalid_cascade_order"
            elif l1 < MIN_STOP_LOSS_L1:
                errors[CONF_STOP_LOSS_L1] = "stop_loss_too_tight"
            elif l4 < MIN_STOP_LOSS_L4:
                errors[CONF_STOP_LOSS_L4] = "stop_loss_too_tight"
            else:
                # Parse whitelist
                raw_whitelist = user_input.pop(CONF_PAIR_WHITELIST, ",".join(DEFAULT_PAIR_WHITELIST))
                user_input[CONF_PAIR_WHITELIST] = _parse_pair_whitelist(raw_whitelist)

                self._data.update(user_input)
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="BController", data=self._data)

        return self.async_show_form(
            step_id="risk",
            data_schema=_risk_schema(),
            errors=errors,
            description_placeholders={
                "l1_min": str(MIN_STOP_LOSS_L1),
                "l4_min": str(MIN_STOP_LOSS_L4),
                "savings_max": str(MAX_SAVINGS_RATE),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "BControllerOptionsFlow":
        return BControllerOptionsFlow(config_entry)


# ─── Options flow ─────────────────────────────────────────────────────────────

class BControllerOptionsFlow(OptionsFlow):
    """Allow modifying risk parameters and poll interval after initial setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        current = self._config_entry.options or self._config_entry.data

        if user_input is not None:
            l1 = user_input.get(CONF_STOP_LOSS_L1, DEFAULT_STOP_LOSS_L1)
            l2 = user_input.get(CONF_STOP_LOSS_L2, DEFAULT_STOP_LOSS_L2)
            l3 = user_input.get(CONF_STOP_LOSS_L3, DEFAULT_STOP_LOSS_L3)
            l4 = user_input.get(CONF_STOP_LOSS_L4, DEFAULT_STOP_LOSS_L4)

            if not (l1 > l2 > l3 > l4):
                errors["base"] = "invalid_cascade_order"
            else:
                raw_whitelist = user_input.pop(CONF_PAIR_WHITELIST, ",".join(DEFAULT_PAIR_WHITELIST))
                user_input[CONF_PAIR_WHITELIST] = _parse_pair_whitelist(raw_whitelist)
                return self.async_create_entry(title="", data=user_input)

        # Populate form with current values
        current_whitelist = current.get(CONF_PAIR_WHITELIST, DEFAULT_PAIR_WHITELIST)
        if isinstance(current_whitelist, list):
            current_whitelist = ",".join(current_whitelist)

        return self.async_show_form(
            step_id="init",
            data_schema=_risk_schema(
                stop_loss_l1=current.get(CONF_STOP_LOSS_L1, DEFAULT_STOP_LOSS_L1),
                stop_loss_l2=current.get(CONF_STOP_LOSS_L2, DEFAULT_STOP_LOSS_L2),
                stop_loss_l3=current.get(CONF_STOP_LOSS_L3, DEFAULT_STOP_LOSS_L3),
                stop_loss_l4=current.get(CONF_STOP_LOSS_L4, DEFAULT_STOP_LOSS_L4),
                savings_rate=current.get(CONF_SAVINGS_RATE, DEFAULT_SAVINGS_RATE),
                cash_reserve_pct=current.get(CONF_CASH_RESERVE_PCT, DEFAULT_CASH_RESERVE_PCT),
                poll_interval=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                max_trade_usdt=current.get(CONF_MAX_TRADE_USDT, DEFAULT_MAX_TRADE_USDT),
                pair_whitelist=current_whitelist,
            ),
            errors=errors,
        )
