"""Intelligence orchestrator for BController.

Ties together data sources, technical indicators, prompt building, and
Claude Haiku invocation to produce a TradingDecision for each cycle.

Flow per cycle:
  1. Fetch news headlines (cached 5min)
  2. Fetch Fear & Greed Index (cached 1h)
  3. Compute technical indicators from klines (pandas-ta, in executor)
  4. Build source weights (constrained to 10-40% each, sum to 100%)
  5. Render user prompt with all context
  6. Call Claude Haiku with structured output
  7. Return (TradingDecision, source_weights)

On Claude API failure: conservative HOLD fallback (R14).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .claude_client import BudgetExhaustedError, ClaudeAPIError, ClaudeClient
from .const import (
    CONF_CASH_RESERVE_PCT,
    CONF_CLAUDE_BUDGET_USD,
    CONF_PAIR_WHITELIST,
    DEFAULT_CASH_RESERVE_PCT,
    DEFAULT_CLAUDE_BUDGET_USD,
    DEFAULT_PAIR_WHITELIST,
    SOURCE_WEIGHT_MAX,
    SOURCE_WEIGHT_MIN,
)
from .data_sources import DataSources
from .models import TradingDecision
from .prompts import SYSTEM_PROMPT, build_user_prompt

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# ─── Default source weights (equal-weight baseline) ──────────────────────────
_DEFAULT_WEIGHTS: dict[str, float] = {
    "news": 25.0,
    "trend": 25.0,
    "sentiment": 25.0,
    "history": 25.0,
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    """Normalise weights so they sum to 100.0 and each is within [10%, 40%].

    Algorithm:
      1. Clamp each weight to [SOURCE_WEIGHT_MIN*100, SOURCE_WEIGHT_MAX*100].
      2. Rescale the clamped values so they sum to 100.
      3. If rescaling pushes any value out of bounds, iterate (max 10 passes).
    """
    min_w = SOURCE_WEIGHT_MIN * 100  # 10.0
    max_w = SOURCE_WEIGHT_MAX * 100  # 40.0

    w = {k: _clamp(v, min_w, max_w) for k, v in weights.items()}

    for _ in range(10):
        total = sum(w.values())
        if total == 0:
            # Reset to defaults if everything zeroed out
            w = dict(_DEFAULT_WEIGHTS)
            break
        scale = 100.0 / total
        w = {k: v * scale for k, v in w.items()}
        # Check bounds after scaling
        if all(min_w - 0.01 <= v <= max_w + 0.01 for v in w.values()):
            break
        # Re-clamp and retry
        w = {k: _clamp(v, min_w, max_w) for k, v in w.items()}

    # Final rounding pass: ensure sum is exactly 100 by adjusting the largest
    rounded = {k: round(v, 2) for k, v in w.items()}
    diff = round(100.0 - sum(rounded.values()), 2)
    if diff != 0.0:
        largest_key = max(rounded, key=lambda k: rounded[k])
        rounded[largest_key] = round(rounded[largest_key] + diff, 2)

    return rounded


def _build_positions_summary(positions: dict[str, Any]) -> str:
    """Build a human-readable positions summary string for the prompt."""
    if not positions:
        return "None"
    parts = []
    for pair, pos in positions.items():
        qty = getattr(pos, "quantity", 0.0)
        vwap = getattr(pos, "vwap_entry_price", 0.0)
        parts.append(f"{pair}: qty={qty:.6f} @ ${vwap:.2f} VWAP")
    return " | ".join(parts)


def _infer_weights_from_data(
    indicators: dict[str, float],
    headlines: list[str],
    fg_value: int,
) -> dict[str, float]:
    """Adjust source weights based on data quality and signal strength.

    Rules:
      - Sparse news (< 3 headlines): reduce news weight toward 10%.
      - Strong technical confluence (RSI extreme): increase trend weight.
      - Extreme F&G (<= 20 or >= 80): increase sentiment weight.
      - Otherwise: balanced 25% each.
    """
    w = dict(_DEFAULT_WEIGHTS)

    # News quality
    n_headlines = len(headlines)
    if n_headlines == 0:
        w["news"] = 10.0
        freed = 15.0  # redistribute 15 pts
        w["trend"] += freed * 0.5
        w["sentiment"] += freed * 0.3
        w["history"] += freed * 0.2
    elif n_headlines < 3:
        w["news"] = 15.0
        freed = 10.0
        w["trend"] += freed * 0.5
        w["sentiment"] += freed * 0.3
        w["history"] += freed * 0.2

    # Technical signal strength — RSI extreme gives confidence in trend
    rsi = indicators.get("rsi", 50.0)
    if rsi < 25 or rsi > 75:
        w["trend"] = min(w["trend"] + 10.0, SOURCE_WEIGHT_MAX * 100)

    # Sentiment extremes
    if fg_value <= 20 or fg_value >= 80:
        w["sentiment"] = min(w["sentiment"] + 10.0, SOURCE_WEIGHT_MAX * 100)

    return _normalise_weights(w)


class IntelligenceEngine:
    """Orchestrates market data collection and Claude Haiku decision-making.

    Instantiated once per config entry in the coordinator.

    Args:
        hass: Home Assistant instance.
        claude_client: Configured ClaudeClient.
        config: Config entry data dict (from entry.data).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        claude_client: ClaudeClient,
        config: dict[str, Any],
    ) -> None:
        self._hass = hass
        self._claude = claude_client
        self._config = config
        self._data_sources = DataSources(hass)

    # ─── Public API ────────────────────────────────────────────────────────

    async def analyze_and_decide(
        self,
        market_data: dict[str, Any],
    ) -> tuple[TradingDecision, dict[str, float]]:
        """Run a full decision cycle and return a TradingDecision + source weights.

        Args:
            market_data: Dict produced by the coordinator's cached data:
                {
                    "pair": str,           # trading pair to analyse
                    "price_map": dict,     # {symbol: price}
                    "klines": list,        # 1h OHLCV klines for the pair
                    "usdt_balance": float,
                    "positions": dict,     # {pair: Position}
                    "portfolio_value": float,
                }

        Returns:
            (TradingDecision, source_weights dict)
            On any failure: conservative HOLD decision.
        """
        pair = market_data.get("pair", "BTCUSDT")

        try:
            return await self._run_cycle(pair, market_data)
        except BudgetExhaustedError as exc:
            _LOGGER.warning("Claude budget exhausted — returning HOLD: %s", exc)
            return self._make_hold("Budget exhausted — monitoring-only mode"), _DEFAULT_WEIGHTS
        except ClaudeAPIError as exc:
            _LOGGER.error("Claude API error — conservative HOLD fallback (R14): %s", exc)
            return self._make_hold(f"Claude unavailable: {exc}"), _DEFAULT_WEIGHTS
        except Exception as exc:
            _LOGGER.exception("Unexpected error in intelligence cycle: %s", exc)
            return self._make_hold(f"Internal error: {exc}"), _DEFAULT_WEIGHTS

    # ─── Internal cycle ────────────────────────────────────────────────────

    async def _run_cycle(
        self,
        pair: str,
        market_data: dict[str, Any],
    ) -> tuple[TradingDecision, dict[str, float]]:
        """Execute the full intelligence pipeline for one decision cycle."""

        # 1. Fetch news headlines (cached 5min)
        headlines = await self._data_sources.get_news_headlines()

        # 2. Fetch Fear & Greed Index (cached 1h)
        fg_value, fg_label = await self._data_sources.get_fear_greed()

        # 3. Compute technical indicators from klines
        klines: list[list] = market_data.get("klines", [])
        indicators = await self._data_sources.compute_technical_indicators(klines)
        ohlcv = self._data_sources.extract_ohlcv_stats(klines)

        # 4. Build adaptive source weights
        source_weights = _infer_weights_from_data(indicators, headlines, fg_value)

        # 5. Build account context
        price_map: dict[str, float] = market_data.get("price_map", {})
        current_price = price_map.get(pair, ohlcv.get("current_price", 0.0))
        usdt_balance: float = market_data.get("usdt_balance", 0.0)
        portfolio_value: float = market_data.get("portfolio_value", 0.0)
        positions = market_data.get("positions", {})

        cash_reserve_pct_cfg = float(
            self._config.get(CONF_CASH_RESERVE_PCT, DEFAULT_CASH_RESERVE_PCT)
        )
        # Compute actual cash reserve as percentage
        if portfolio_value > 0:
            actual_cash_pct = (usdt_balance / portfolio_value) * 100.0
        else:
            actual_cash_pct = cash_reserve_pct_cfg

        positions_summary = _build_positions_summary(positions)
        budget_remaining = self._claude.budget_remaining

        # 6. Render user prompt
        user_prompt = build_user_prompt(
            pair=pair,
            price=current_price,
            high=ohlcv.get("high_24h", current_price),
            low=ohlcv.get("low_24h", current_price),
            volume_usdt=ohlcv.get("volume_usdt_24h", 0.0),
            rsi=indicators.get("rsi", 50.0),
            macd=indicators.get("macd", 0.0),
            signal=indicators.get("macd_signal", 0.0),
            hist=indicators.get("macd_hist", 0.0),
            bb_upper=indicators.get("bb_upper", 0.0),
            bb_mid=indicators.get("bb_mid", 0.0),
            bb_lower=indicators.get("bb_lower", 0.0),
            ema20=indicators.get("ema20", 0.0),
            ema50=indicators.get("ema50", 0.0),
            usdt_balance=usdt_balance,
            positions_summary=positions_summary,
            cash_reserve_pct=actual_cash_pct,
            budget_remaining=budget_remaining,
            fg_value=fg_value,
            fg_label=fg_label,
            headlines=headlines,
            source_weights=source_weights,
            timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        )

        _LOGGER.debug(
            "Intelligence cycle for %s: %d headlines, F&G=%d (%s), "
            "RSI=%.1f, weights=%s",
            pair,
            len(headlines),
            fg_value,
            fg_label,
            indicators.get("rsi", 50.0),
            source_weights,
        )

        # 7. Call Claude Haiku
        decision = await self._claude.get_trading_decision(
            system_prompt=SYSTEM_PROMPT,
            market_context=user_prompt,
        )

        return decision, source_weights

    # ─── Conservative fallback helpers ────────────────────────────────────

    @staticmethod
    def _make_hold(reason: str) -> TradingDecision:
        """Create a conservative HOLD decision for error/fallback scenarios."""
        return TradingDecision(
            action="HOLD",
            pair="BTCUSDT",
            size_pct=0.0,
            confidence=0.0,
            reason_code="NEUTRAL",
            reasoning=reason[:200],  # TradingDecision.reasoning has no length limit but keep sane
        )

    # ─── Diagnostics ──────────────────────────────────────────────────────

    def get_claude_usage_stats(self) -> dict[str, Any]:
        """Proxy to ClaudeClient.get_usage_stats() for sensor exposure."""
        return self._claude.get_usage_stats()
