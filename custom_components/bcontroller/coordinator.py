"""DataUpdateCoordinator for BController.

Orchestrates the polling cycle:
  1. Fetch prices and account balances
  2. Record portfolio snapshot
  3. Run drawdown checks (L2/L3/L4)
  4. Run L1 stop-loss checks on open positions
  5. If trading is allowed: invoke decision cycle (intelligence → Claude → execute)
  6. Update sensors

The coordinator exposes a ``data`` dict consumed by all sensor entities.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BINANCE_API_KEY,
    CONF_BINANCE_API_SECRET,
    CONF_CLAUDE_API_KEY,
    CONF_PAIR_WHITELIST,
    CONF_POLL_INTERVAL,
    CONF_STOP_LOSS_L1,
    CONF_STOP_LOSS_L2,
    CONF_STOP_LOSS_L3,
    CONF_STOP_LOSS_L4,
    CONF_SAVINGS_RATE,
    CONF_CASH_RESERVE_PCT,
    CONF_MAX_TRADE_USDT,
    CONF_CLAUDE_BUDGET_USD,
    DATA_BINANCE_CLIENT,
    DEFAULT_CLAUDE_BUDGET_USD,
    DATA_PORTFOLIO,
    DATA_RISK_MANAGER,
    DEFAULT_PAIR_WHITELIST,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_TRADE_SIZE_PCT,
    MIN_CONFIDENCE_THRESHOLD,
    SENSOR_BTC_PRICE,
    SENSOR_LAST_DECISION,
    SENSOR_PORTFOLIO_VALUE,
    SENSOR_SAVINGS_BALANCE,
    SENSOR_SYSTEM_STATUS,
    TRADE_MAX_PER_HOUR,
    TRADE_MIN_GAP_SEC,
    TRADE_MIN_INTERVAL_SEC,
    TRADE_MAX_INTERVAL_SEC,
)
from .claude_client import ClaudeClient
from .intelligence import IntelligenceEngine
from .models import TradingDecision
from .portfolio import PortfolioState
from .risk import RiskManager, TradeBlockedError
from .trading import BinanceAPIError, BinanceClient, BinanceRateLimitError

_LOGGER = logging.getLogger(__name__)


class BControllerCoordinator(DataUpdateCoordinator):
    """Central coordinator managing BController's polling cycle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        poll_interval = entry.options.get(
            CONF_POLL_INTERVAL,
            entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )

        # Subsystems — initialised in async_setup
        self.binance: BinanceClient | None = None
        self.portfolio: PortfolioState | None = None
        self.risk: RiskManager | None = None
        self.intelligence: IntelligenceEngine | None = None

        # Trade timing state (R2)
        self._last_trade_time: float = 0.0
        self._trades_this_hour: list[float] = []
        self._next_trade_allowed_at: float = 0.0

        # Runtime state exposed to sensors
        self._last_decision: TradingDecision | None = None
        self._last_error: str | None = None
        self._btc_price: float = 0.0
        self._portfolio_value: float = 0.0
        self._initial_portfolio_value: float | None = None
        self._binance_banned: bool = False

    # ─── Setup ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Initialise all subsystems and run startup recovery sequence."""
        data = self._entry.data

        # Build Binance client
        self.binance = BinanceClient(
            hass=self.hass,
            api_key=data[CONF_BINANCE_API_KEY],
            api_secret=data[CONF_BINANCE_API_SECRET],
        )

        # Build Claude client and intelligence engine
        claude_client = ClaudeClient(
            hass=self.hass,
            api_key=data[CONF_CLAUDE_API_KEY],
            budget_usd=float(data.get(CONF_CLAUDE_BUDGET_USD, DEFAULT_CLAUDE_BUDGET_USD)),
        )
        self.intelligence = IntelligenceEngine(
            hass=self.hass,
            claude_client=claude_client,
            config=data,
        )

        # Build risk manager with configured thresholds
        self.risk = RiskManager(
            hass=self.hass,
            stop_loss_l1=data.get(CONF_STOP_LOSS_L1, -3.0),
            stop_loss_l2=data.get(CONF_STOP_LOSS_L2, -5.0),
            stop_loss_l3=data.get(CONF_STOP_LOSS_L3, -8.0),
            stop_loss_l4=data.get(CONF_STOP_LOSS_L4, -15.0),
            savings_rate=data.get(CONF_SAVINGS_RATE, 10.0),
            cash_reserve_pct=data.get(CONF_CASH_RESERVE_PCT, 20.0),
        )
        await self.risk.async_load()

        # Build portfolio state and run recovery
        self.portfolio = PortfolioState(hass=self.hass)
        await self.portfolio.async_load()
        await self.portfolio.async_recover(self.binance)

        # API key security check (R13)
        try:
            account = await self.binance.get_account()
            warnings = RiskManager.check_api_permissions(account)
            for warning in warnings:
                _LOGGER.warning(warning)
        except Exception as exc:
            _LOGGER.error("Startup account check failed: %s", exc)

        # Capture initial portfolio value for L4 total-drawdown reference
        if self._initial_portfolio_value is None and self.portfolio:
            try:
                price_map = await self._build_price_map()
                self._initial_portfolio_value = self.portfolio.get_total_value(price_map)
                _LOGGER.info(
                    "Initial portfolio value set to %.2f USDT",
                    self._initial_portfolio_value,
                )
            except Exception as exc:
                _LOGGER.warning("Could not determine initial portfolio value: %s", exc)

    # ─── Main polling cycle ────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Execute one full polling cycle.

        Returns a dict consumed by sensor entities.
        """
        if self.binance is None or self.portfolio is None or self.risk is None:
            raise UpdateFailed("Coordinator not yet initialised")

        if self._binance_banned:
            raise UpdateFailed("Binance IP auto-ban active. Trading halted.")

        try:
            # 1. Fetch current prices
            price_map = await self._build_price_map()
            self._btc_price = price_map.get("BTCUSDT", 0.0)

            # 2. Compute portfolio value
            self._portfolio_value = self.portfolio.get_total_value(price_map)

            # 3. Record snapshot
            await self.portfolio.record_snapshot(
                total_value_usdt=self._portfolio_value,
                btc_price_usdt=self._btc_price,
            )

            # 4. Run drawdown checks
            await self._run_drawdown_checks()

            # 5. L1 stop-loss checks on open positions
            await self._check_l1_stop_losses(price_map)

            # 6. Trade decision cycle (if allowed and timing permits)
            if await self._is_trade_timing_ok():
                await self._run_decision_cycle(price_map)

        except BinanceRateLimitError as exc:
            if exc.status_code == 418:
                self._binance_banned = True
                _LOGGER.critical(
                    "Binance IP auto-ban (418). All trading halted. "
                    "Manual intervention required."
                )
            elif exc.retry_after:
                _LOGGER.warning(
                    "Binance 429 rate limit. Will retry in %ds.", exc.retry_after
                )
            self._last_error = str(exc)
        except BinanceAPIError as exc:
            _LOGGER.error("Binance API error in polling cycle: %s", exc)
            self._last_error = str(exc)
        except Exception as exc:
            _LOGGER.exception("Unexpected error in polling cycle: %s", exc)
            self._last_error = str(exc)
            raise UpdateFailed(str(exc)) from exc

        return self._build_sensor_data()

    # ─── Drawdown checks ───────────────────────────────────────────────────

    async def _run_drawdown_checks(self) -> None:
        """Run L2, L3, and L4 drawdown checks against portfolio snapshots."""
        value_1h_ago = self.portfolio.get_value_at(3600)
        value_24h_ago = self.portfolio.get_value_at(86400)

        triggered = await self.risk.check_portfolio_drawdown(
            current_value=self._portfolio_value,
            value_1h_ago=value_1h_ago,
            value_24h_ago=value_24h_ago,
        )

        if triggered:
            _LOGGER.warning(
                "%s triggered. Current portfolio value: %.2f USDT",
                triggered,
                self._portfolio_value,
            )

        # L4 check
        if self._initial_portfolio_value:
            l4_triggered = await self.risk.check_total_drawdown(
                current_value=self._portfolio_value,
                initial_value=self._initial_portfolio_value,
            )
            if l4_triggered:
                _LOGGER.critical(
                    "L4 Emergency Stop. Portfolio value: %.2f USDT (initial: %.2f USDT)",
                    self._portfolio_value,
                    self._initial_portfolio_value,
                )

    # ─── L1 stop-loss ──────────────────────────────────────────────────────

    async def _check_l1_stop_losses(self, price_map: dict[str, float]) -> None:
        """Check each open position against L1 stop-loss threshold."""
        for pair, position in list(self.portfolio.positions.items()):
            current_price = price_map.get(pair)
            if current_price is None:
                continue

            if self.risk.check_stop_loss(position, current_price):
                _LOGGER.warning(
                    "L1 stop-loss: closing position %s at %.4f (VWAP was %.4f)",
                    pair,
                    current_price,
                    position.vwap_entry_price,
                )
                await self._execute_emergency_close(pair, position.quantity, current_price)

    async def _execute_emergency_close(
        self, pair: str, quantity: float, current_price: float
    ) -> None:
        """Execute an emergency market close for L1 stop-loss."""
        try:
            result = await self.binance.place_market_order(
                symbol=pair,
                side="SELL",
                quantity=quantity,
                estimated_price=current_price,
            )
            fill_price = float(result.get("fills", [{}])[0].get("price", current_price))
            pnl = await self.portfolio.close_position(pair, quantity, fill_price)
            savings_amount = self.risk.apply_savings_rate(max(pnl, 0.0))
            if savings_amount > 0:
                await self.portfolio.apply_savings(savings_amount)
            _LOGGER.info(
                "Emergency close %s: fill=%.4f, P&L=%.4f USDT", pair, fill_price, pnl
            )
        except ValueError as exc:
            # Slippage too high — fall back to limit order
            _LOGGER.warning(
                "Market order slippage too high for %s, using limit order: %s",
                pair,
                exc,
            )
            try:
                # Limit 0.5% below current price for faster fill
                limit_price = current_price * 0.995
                await self.binance.place_limit_order(
                    symbol=pair, side="SELL", quantity=quantity, price=limit_price
                )
            except Exception as limit_exc:
                _LOGGER.error("Limit order also failed for %s: %s", pair, limit_exc)
        except Exception as exc:
            _LOGGER.error("Emergency close failed for %s: %s", pair, exc)

    # ─── Trade timing guard (R2) ───────────────────────────────────────────

    async def _is_trade_timing_ok(self) -> bool:
        """Return True if trade timing rules allow a new decision cycle."""
        now = time.monotonic()

        # Minimum gap between consecutive trades
        if now - self._last_trade_time < TRADE_MIN_GAP_SEC:
            return False

        # Not yet reached the randomized next-trade window
        if now < self._next_trade_allowed_at:
            return False

        # Hourly burst limit
        cutoff = now - 3600
        self._trades_this_hour = [t for t in self._trades_this_hour if t > cutoff]
        if len(self._trades_this_hour) >= TRADE_MAX_PER_HOUR:
            _LOGGER.debug("Trade burst limit reached (%d/h). Skipping cycle.", TRADE_MAX_PER_HOUR)
            return False

        return True

    def _schedule_next_trade_window(self) -> None:
        """Set a randomized next-trade timestamp (R2 — uniform + jitter)."""
        interval = random.uniform(TRADE_MIN_INTERVAL_SEC, TRADE_MAX_INTERVAL_SEC)
        self._next_trade_allowed_at = time.monotonic() + interval
        _LOGGER.debug("Next trade window in %.0f seconds", interval)

    # ─── Decision cycle ────────────────────────────────────────────────────

    async def _run_decision_cycle(self, price_map: dict[str, float]) -> None:
        """Check risk gate, call intelligence layer, execute if appropriate.

        The actual intelligence + Claude call is delegated to the intelligence
        module (Wave 2 scope).  This skeleton handles the orchestration.
        """
        try:
            await self.risk.check_trade_allowed()
        except TradeBlockedError as exc:
            _LOGGER.debug("Trade blocked: %s", exc)
            return

        # Cash reserve check (R8)
        if not self.risk.check_cash_reserve(
            self.portfolio.cash_balance, self._portfolio_value
        ):
            _LOGGER.debug(
                "Cash reserve below %.1f%%. No new positions.", self.risk.cash_reserve_pct
            )
            return

        if self.intelligence is None:
            _LOGGER.error("Intelligence engine not initialised — skipping decision cycle")
            return

        # Fetch klines for the primary pair (first in whitelist)
        pair_whitelist = self._entry.data.get(CONF_PAIR_WHITELIST, DEFAULT_PAIR_WHITELIST)
        pair = pair_whitelist[0] if pair_whitelist else "BTCUSDT"

        klines: list = []
        try:
            klines = await self.binance.get_klines(pair, "1h", limit=100)
        except Exception as exc:
            _LOGGER.warning("Could not fetch klines for %s: %s — proceeding without", pair, exc)

        market_data = {
            "pair": pair,
            "price_map": price_map,
            "klines": klines,
            "usdt_balance": self.portfolio.cash_balance if self.portfolio else 0.0,
            "positions": self.portfolio.positions if self.portfolio else {},
            "portfolio_value": self._portfolio_value,
        }

        decision, source_weights = await self.intelligence.analyze_and_decide(market_data)

        self._last_decision = decision
        await self._execute_decision(decision, source_weights, price_map)
        self._schedule_next_trade_window()

    async def _execute_decision(
        self,
        decision: TradingDecision,
        source_weights: dict[str, float],
        price_map: dict[str, float],
    ) -> None:
        """Validate and execute a TradingDecision from Claude.

        Called by the intelligence layer once available.
        """
        if not decision.is_executable(MIN_CONFIDENCE_THRESHOLD):
            _LOGGER.info(
                "Decision HOLD or confidence %.2f below threshold. No trade.",
                decision.confidence,
            )
            record = self.portfolio.create_trade_record(
                decision=decision,
                source_weights=source_weights,
                executed=False,
                reject_reason=f"confidence={decision.confidence:.2f} or HOLD",
            )
            await self.portfolio.record_trade(record)
            return

        pair = decision.pair
        pair_whitelist = self._entry.data.get(CONF_PAIR_WHITELIST, DEFAULT_PAIR_WHITELIST)
        if pair not in pair_whitelist:
            _LOGGER.warning("Decision pair %s not in whitelist %s. Skipped.", pair, pair_whitelist)
            return

        current_price = price_map.get(pair)
        if current_price is None or current_price <= 0:
            _LOGGER.error("No price available for %s. Cannot execute trade.", pair)
            return

        # Apply post-L2/L3 size reduction (R9)
        size_multiplier = self.risk.get_position_size_multiplier()
        adjusted_size_pct = decision.size_pct * size_multiplier
        if adjusted_size_pct <= 0:
            _LOGGER.info("Position size multiplier is 0 — trading blocked by risk state.")
            return

        # Clamp to MAX_TRADE_SIZE_PCT and max USDT cap
        trading_balance = self.portfolio.get_trading_balance()
        max_trade_usdt = self._entry.data.get(CONF_MAX_TRADE_USDT, 100.0)
        trade_usdt = min(
            trading_balance * (adjusted_size_pct / 100.0),
            trading_balance * (MAX_TRADE_SIZE_PCT / 100.0),
            max_trade_usdt,
        )

        if trade_usdt <= 0:
            _LOGGER.info("Calculated trade size is zero. Skipping.")
            return

        quantity = trade_usdt / current_price
        record = self.portfolio.create_trade_record(
            decision=decision,
            source_weights=source_weights,
            executed=False,
        )

        try:
            if decision.action == "BUY":
                result = await self.binance.place_market_order(
                    symbol=pair,
                    side="BUY",
                    quantity=quantity,
                    estimated_price=current_price,
                )
            else:  # SELL
                pos = self.portfolio.positions.get(pair)
                if pos is None:
                    _LOGGER.warning("SELL decision for %s but no open position.", pair)
                    return
                result = await self.binance.place_market_order(
                    symbol=pair,
                    side="SELL",
                    quantity=min(quantity, pos.quantity),
                    estimated_price=current_price,
                )

            fills = result.get("fills", [])
            fill_price = float(fills[0]["price"]) if fills else current_price
            fill_qty = float(result.get("executedQty", quantity))

            record.executed = True
            record.order_id = str(result.get("orderId", ""))
            record.fill_price = fill_price
            record.fill_quantity = fill_qty
            if fills:
                record.commission = sum(float(f.get("commission", 0)) for f in fills)
                record.commission_asset = fills[0].get("commissionAsset", "BNB")

            if decision.action == "BUY":
                await self.portfolio.open_position(pair, fill_qty, fill_price)
            else:
                pnl = await self.portfolio.close_position(pair, fill_qty, fill_price)
                record.pnl = pnl
                record.pnl_pct = (
                    ((fill_price - self.portfolio.positions.get(pair, None) and
                      self.portfolio.positions[pair].vwap_entry_price or fill_price)
                     / fill_price) * 100.0
                    if pnl != 0 else 0.0
                )
                savings_amount = self.risk.apply_savings_rate(max(pnl, 0.0))
                if savings_amount > 0:
                    await self.portfolio.apply_savings(savings_amount)

            self._last_decision = decision
            self._last_trade_time = time.monotonic()
            self._trades_this_hour.append(self._last_trade_time)
            _LOGGER.info(
                "Trade executed: %s %s %.8f @ %.4f (confidence=%.2f)",
                decision.action,
                pair,
                fill_qty,
                fill_price,
                decision.confidence,
            )

        except ValueError as exc:
            # Slippage too high — retry with limit order
            _LOGGER.info(
                "Market order rejected (slippage): %s. Retrying with limit order.", exc
            )
            try:
                limit_price = (
                    current_price * 1.001 if decision.action == "BUY" else current_price * 0.999
                )
                result = await self.binance.place_limit_order(
                    symbol=pair,
                    side=decision.action,
                    quantity=quantity,
                    price=limit_price,
                )
                record.executed = True
                record.order_id = str(result.get("orderId", ""))
                record.fill_price = limit_price
                record.fill_quantity = quantity
            except Exception as limit_exc:
                _LOGGER.error("Limit order also failed: %s", limit_exc)
                record.reject_reason = f"limit_order_failed: {limit_exc}"

        except (BinanceAPIError, BinanceRateLimitError) as exc:
            _LOGGER.error("Order placement failed: %s", exc)
            record.reject_reason = str(exc)
        finally:
            await self.portfolio.record_trade(record)

    # ─── Helpers ───────────────────────────────────────────────────────────

    async def _build_price_map(self) -> dict[str, float]:
        """Fetch current ticker prices for all whitelisted pairs + BTC."""
        whitelist = self._entry.data.get(CONF_PAIR_WHITELIST, DEFAULT_PAIR_WHITELIST)
        pairs = list({*whitelist, "BTCUSDT"})
        price_map: dict[str, float] = {}

        for pair in pairs:
            try:
                ticker = await self.binance.get_ticker_price(pair)
                price_map[pair] = float(ticker["price"])
            except Exception as exc:
                _LOGGER.warning("Could not fetch price for %s: %s", pair, exc)

        return price_map

    def _build_sensor_data(self) -> dict[str, Any]:
        """Build the data dict consumed by sensor entities."""
        last_decision_str = None
        if self._last_decision:
            last_decision_str = (
                f"{self._last_decision.action} {self._last_decision.pair} "
                f"({self._last_decision.confidence:.0%}) — {self._last_decision.reason_code}"
            )

        return {
            SENSOR_BTC_PRICE: self._btc_price,
            SENSOR_PORTFOLIO_VALUE: self._portfolio_value,
            SENSOR_SAVINGS_BALANCE: self.portfolio.savings_balance if self.portfolio else 0.0,
            SENSOR_SYSTEM_STATUS: self.risk.get_state() if self.risk else "unknown",
            SENSOR_LAST_DECISION: last_decision_str or "No decision yet",
            "binance_weight": self.binance.used_weight if self.binance else 0,
            "open_positions": len(self.portfolio.positions) if self.portfolio else 0,
            "last_error": self._last_error,
        }
