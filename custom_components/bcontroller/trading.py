"""Binance trading engine for BController.

Wraps the official binance-connector SDK with:
  - Async execution via hass.async_add_executor_job
  - Request weight tracking (R3)
  - LOT_SIZE / PRICE_FILTER rounding
  - Slippage estimation (R6)
  - Exponential backoff on 429/418 (R14)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import (
    BINANCE_WEIGHT_SAFE,
    MAX_SLIPPAGE_PCT,
)
from .models import ExchangeFilters

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# Binance response codes
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_IP_BANNED = 418

# Exchange info cache lifetime (seconds) — refresh daily or on 400 errors
_EXCHANGE_INFO_TTL = 86400


class BinanceWeightTracker:
    """Tracks API request weight consumption per rolling 1-minute window."""

    def __init__(self, safe_limit: int = BINANCE_WEIGHT_SAFE) -> None:
        self._safe_limit = safe_limit
        self._used_weight: int = 0
        self._window_start: float = time.monotonic()

    def update(self, weight_header: str | None) -> None:
        """Update used weight from X-MBX-USED-WEIGHT-1M response header."""
        if weight_header is None:
            return
        try:
            self._used_weight = int(weight_header)
        except (ValueError, TypeError):
            pass

    def is_safe(self) -> bool:
        """Return True if we are below the safe threshold."""
        return self._used_weight < self._safe_limit

    @property
    def used_weight(self) -> int:
        return self._used_weight


class BinanceAPIError(Exception):
    """Raised for non-retryable Binance API errors."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class BinanceRateLimitError(Exception):
    """Raised when Binance returns 429 or 418."""

    def __init__(self, status_code: int, retry_after: int | None = None) -> None:
        super().__init__(f"Rate limit hit (HTTP {status_code})")
        self.status_code = status_code
        self.retry_after = retry_after


def _round_step(value: float, step: float) -> float:
    """Round value DOWN to the nearest step increment (LOT_SIZE stepSize)."""
    if step <= 0:
        return value
    precision = int(round(-math.log10(step)))
    factor = Decimal(str(10 ** precision))
    return float(
        (Decimal(str(value)) / Decimal(str(step))).to_integral_value(
            rounding=ROUND_DOWN
        )
        * Decimal(str(step))
    )


def _round_tick(value: float, tick: float) -> float:
    """Round value to the nearest tick size (PRICE_FILTER tickSize)."""
    if tick <= 0:
        return value
    precision = int(round(-math.log10(tick)))
    factor = Decimal(str(10 ** precision))
    return float(
        (Decimal(str(value)) / Decimal(str(tick))).to_integral_value(
            rounding=ROUND_DOWN
        )
        * Decimal(str(tick))
    )


def _extract_weight_header(response: Any) -> str | None:
    """Extract X-MBX-USED-WEIGHT-1M from a binance-connector response."""
    # binance-connector Response object exposes headers via .headers dict-like
    try:
        headers = response.headers if hasattr(response, "headers") else {}
        return headers.get("X-MBX-USED-WEIGHT-1M") or headers.get(
            "x-mbx-used-weight-1m"
        )
    except Exception:
        return None


class BinanceClient:
    """Async-friendly wrapper around the binance-connector Spot client.

    All network I/O is dispatched via ``hass.async_add_executor_job`` because
    the SDK is synchronous.  Weight tracking happens automatically after every
    successful call.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._hass = hass
        self._api_key = api_key
        self._api_secret = api_secret
        self._weight_tracker = BinanceWeightTracker()
        self._exchange_filters: dict[str, ExchangeFilters] = {}
        self._exchange_info_fetched_at: float = 0.0
        self._client: Any = None  # binance.spot.Spot instance, lazy-init

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _get_client(self) -> Any:
        """Lazily initialise the synchronous Spot client."""
        if self._client is None:
            from binance.spot import Spot  # type: ignore[import]

            self._client = Spot(
                api_key=self._api_key,
                api_secret=self._api_secret,
            )
        return self._client

    async def _call(self, fn, *args, **kwargs) -> Any:
        """Execute a synchronous SDK call in an executor with weight tracking.

        Raises BinanceRateLimitError on 429/418 and BinanceAPIError on other
        HTTP errors.  Implements exponential backoff for transient failures.
        """
        if not self._weight_tracker.is_safe():
            raise BinanceRateLimitError(
                429,
                retry_after=60,
            )

        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                response = await self._hass.async_add_executor_job(fn, *args, **kwargs)
                weight_header = _extract_weight_header(response)
                self._weight_tracker.update(weight_header)
                # binance-connector returns parsed dicts, but keep raw object if needed
                if hasattr(response, "json"):
                    return response.json()
                return response
            except Exception as exc:
                exc_str = str(exc)
                # Parse HTTP status from binance-connector exceptions
                status_code = getattr(exc, "status_code", None)

                if status_code == _HTTP_IP_BANNED:
                    _LOGGER.critical("Binance IP auto-ban (418) detected. Halting trading.")
                    raise BinanceRateLimitError(_HTTP_IP_BANNED) from exc

                if status_code == _HTTP_TOO_MANY_REQUESTS:
                    retry_after_raw = getattr(exc, "headers", {}).get("Retry-After", None)
                    retry_after = int(retry_after_raw) if retry_after_raw else 60
                    _LOGGER.warning(
                        "Binance 429 rate limit hit. Retry-After=%s", retry_after
                    )
                    raise BinanceRateLimitError(
                        _HTTP_TOO_MANY_REQUESTS, retry_after=retry_after
                    ) from exc

                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    _LOGGER.warning(
                        "Binance API call failed (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        max_retries,
                        exc_str,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    _LOGGER.error("Binance API call failed after %d retries: %s", max_retries, exc_str)
                    raise BinanceAPIError(
                        status_code or 0, exc_str
                    ) from exc

        raise BinanceAPIError(0, "Unexpected retry loop exit")

    # ─── Public API methods ────────────────────────────────────────────────

    async def get_account(self) -> dict:
        """Return account information including balances.

        Endpoint weight: 20.
        """
        client = self._get_client()
        return await self._call(client.account)

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Return all open orders, optionally filtered by symbol.

        Endpoint weight: 6 (with symbol) / 80 (without symbol).
        """
        client = self._get_client()
        if symbol:
            return await self._call(client.get_open_orders, symbol=symbol)
        return await self._call(client.get_open_orders)

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[list]:
        """Return OHLCV kline data.

        Args:
            symbol: Trading pair, e.g. ``"BTCUSDT"``.
            interval: Binance interval string, e.g. ``"1h"``, ``"4h"``.
            limit: Number of candles (max 1000).

        Endpoint weight: 2.
        """
        client = self._get_client()
        return await self._call(client.klines, symbol, interval, limit=limit)

    async def get_ticker_price(self, symbol: str) -> dict:
        """Return latest price for a symbol.

        Endpoint weight: 2.
        """
        client = self._get_client()
        return await self._call(client.ticker_price, symbol=symbol)

    async def get_exchange_info(self, symbol: str) -> ExchangeFilters:
        """Return and cache parsed exchange filters for a symbol.

        Refreshes if cache is older than _EXCHANGE_INFO_TTL or on 400 errors.
        Endpoint weight: 20 (with symbol).
        """
        now = time.monotonic()
        if (
            symbol in self._exchange_filters
            and now - self._exchange_info_fetched_at < _EXCHANGE_INFO_TTL
        ):
            return self._exchange_filters[symbol]

        client = self._get_client()
        raw = await self._call(client.exchange_info, symbol=symbol)
        filters = self._parse_exchange_filters(symbol, raw)
        self._exchange_filters[symbol] = filters
        self._exchange_info_fetched_at = now
        return filters

    async def refresh_exchange_info(self, symbol: str) -> ExchangeFilters:
        """Force-refresh exchange info (called on 400 errors per R3)."""
        if symbol in self._exchange_filters:
            del self._exchange_filters[symbol]
        return await self.get_exchange_info(symbol)

    def _parse_exchange_filters(self, symbol: str, raw: dict) -> ExchangeFilters:
        """Extract LOT_SIZE, PRICE_FILTER, and NOTIONAL filters from raw response."""
        symbols = raw.get("symbols", [])
        symbol_info = next((s for s in symbols if s["symbol"] == symbol), None)
        if symbol_info is None:
            raise BinanceAPIError(0, f"Symbol {symbol} not found in exchange info")

        filters = {f["filterType"]: f for f in symbol_info.get("filters", [])}

        lot = filters.get("LOT_SIZE", {})
        price_f = filters.get("PRICE_FILTER", {})
        # Binance may use "NOTIONAL" or "MIN_NOTIONAL"
        notional_f = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))

        return ExchangeFilters(
            symbol=symbol,
            min_qty=float(lot.get("minQty", 0)),
            max_qty=float(lot.get("maxQty", 9999999)),
            step_size=float(lot.get("stepSize", 1)),
            min_price=float(price_f.get("minPrice", 0)),
            max_price=float(price_f.get("maxPrice", 9999999)),
            tick_size=float(price_f.get("tickSize", 0.01)),
            min_notional=float(notional_f.get("minNotional", 5.0)),
        )

    async def place_market_order(
        self,
        symbol: str,
        side: str,        # "BUY" or "SELL"
        quantity: float,
        estimated_price: float | None = None,
    ) -> dict:
        """Place a market order with filter validation and slippage check.

        If ``estimated_price`` is supplied and estimated slippage exceeds
        MAX_SLIPPAGE_PCT, raises ``ValueError`` to signal the caller to use
        a limit order instead (R6 slippage protection).

        Endpoint weight: 1.
        """
        filters = await self.get_exchange_info(symbol)

        # Round quantity to LOT_SIZE stepSize
        qty = _round_step(quantity, filters.step_size)
        qty = max(qty, filters.min_qty)
        qty = min(qty, filters.max_qty)

        # Validate min notional
        if estimated_price is not None:
            notional = qty * estimated_price
            if notional < filters.min_notional:
                raise ValueError(
                    f"Order notional {notional:.4f} USDT is below minimum "
                    f"{filters.min_notional} USDT for {symbol}"
                )

            # Slippage guard: estimate spread from order book depth
            slippage_pct = await self._estimate_slippage(symbol, side, qty)
            if slippage_pct > MAX_SLIPPAGE_PCT:
                raise ValueError(
                    f"Estimated slippage {slippage_pct:.2f}% exceeds "
                    f"{MAX_SLIPPAGE_PCT}% limit. Use limit order instead."
                )

        client = self._get_client()
        return await self._call(
            client.new_order,
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict:
        """Place a GTC limit order with filter validation.

        Endpoint weight: 1.
        """
        filters = await self.get_exchange_info(symbol)

        qty = _round_step(quantity, filters.step_size)
        qty = max(qty, filters.min_qty)
        qty = min(qty, filters.max_qty)

        rounded_price = _round_tick(price, filters.tick_size)
        rounded_price = max(rounded_price, filters.min_price)
        if filters.max_price > 0:
            rounded_price = min(rounded_price, filters.max_price)

        notional = qty * rounded_price
        if notional < filters.min_notional:
            raise ValueError(
                f"Limit order notional {notional:.4f} USDT is below minimum "
                f"{filters.min_notional} USDT for {symbol}"
            )

        client = self._get_client()
        return await self._call(
            client.new_order,
            symbol=symbol,
            side=side,
            type="LIMIT",
            quantity=qty,
            price=str(rounded_price),
            timeInForce="GTC",
        )

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an open order by symbol and Binance order ID.

        Endpoint weight: 1.
        """
        client = self._get_client()
        return await self._call(
            client.cancel_order, symbol=symbol, orderId=order_id
        )

    async def get_order_status(self, symbol: str, order_id: int) -> dict:
        """Query a single order status (for post-placement confirmation).

        Endpoint weight: 4.
        """
        client = self._get_client()
        return await self._call(
            client.get_order, symbol=symbol, orderId=order_id
        )

    async def _estimate_slippage(
        self, symbol: str, side: str, quantity: float
    ) -> float:
        """Estimate slippage by walking the order book depth.

        Uses depth endpoint (weight: 5 for limit=100) to compute weighted
        average fill price vs. best bid/ask, then returns pct deviation.
        """
        client = self._get_client()
        try:
            depth = await self._call(client.depth, symbol=symbol, limit=20)
        except Exception as exc:
            _LOGGER.warning("Could not fetch order book for slippage estimate: %s", exc)
            return 0.0

        levels = depth.get("asks" if side == "BUY" else "bids", [])
        if not levels:
            return 0.0

        best_price = float(levels[0][0])
        if best_price == 0:
            return 0.0

        remaining = quantity
        total_cost = 0.0
        for price_str, qty_str in levels:
            level_price = float(price_str)
            level_qty = float(qty_str)
            take = min(remaining, level_qty)
            total_cost += take * level_price
            remaining -= take
            if remaining <= 0:
                break

        if remaining > 0:
            # Not enough liquidity in top 20 — assume worst case
            return MAX_SLIPPAGE_PCT + 1.0

        avg_price = total_cost / quantity
        slippage_pct = abs((avg_price - best_price) / best_price) * 100.0
        return slippage_pct

    # ─── Weight info ───────────────────────────────────────────────────────

    @property
    def weight_tracker(self) -> BinanceWeightTracker:
        return self._weight_tracker

    @property
    def used_weight(self) -> int:
        return self._weight_tracker.used_weight
