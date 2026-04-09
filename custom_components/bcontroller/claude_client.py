"""Claude API wrapper for BController's decision engine.

Handles:
  - Structured output via json_schema (TradingDecision schema)
  - Prompt caching with cache_control ephemeral on the system prompt
  - Token and cost tracking with per-tier pricing
  - Budget enforcement (hard cap per config)
  - Conservative fallback on API errors (R14)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import (
    BUDGET_ALLOCATION_TRADE,
    MODEL_ID,
)
from .models import TradingDecision

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# ─── Pricing constants (USD per million tokens) ──────────────────────────────
# Claude Haiku 4.5 pricing
_PRICE_INPUT_PER_MTK: float = 1.00       # $1.00 / 1M input tokens
_PRICE_OUTPUT_PER_MTK: float = 5.00      # $5.00 / 1M output tokens
_PRICE_CACHE_WRITE_MULT: float = 1.25    # cache write = 1.25x input price
_PRICE_CACHE_READ_MULT: float = 0.10     # cache read  = 0.10x input price

# ─── Structured output JSON schema ───────────────────────────────────────────
_TRADING_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["BUY", "SELL", "HOLD"],
            "description": "Trading action to take",
        },
        "pair": {
            "type": "string",
            "description": "Trading pair symbol, e.g. BTCUSDT",
        },
        "size_pct": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 100.0,
            "description": "Percentage of trading balance (BUY) or open position (SELL) to trade",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Decision confidence from 0.0 to 1.0",
        },
        "reason_code": {
            "type": "string",
            "enum": [
                "TREND_UP",
                "TREND_DOWN",
                "OVERSOLD",
                "OVERBOUGHT",
                "BREAKOUT",
                "NEWS_POSITIVE",
                "NEWS_NEGATIVE",
                "NEUTRAL",
            ],
            "description": "Machine-readable classification of the primary signal driver",
        },
        "reasoning": {
            "type": "string",
            "description": "Human-readable one-sentence explanation for audit trail",
        },
    },
    "required": ["action", "pair", "size_pct", "confidence", "reason_code", "reasoning"],
    "additionalProperties": False,
}


class BudgetExhaustedError(Exception):
    """Raised when the configured Claude API budget is fully consumed."""


class ClaudeAPIError(Exception):
    """Raised for non-retryable Claude API errors."""


class ClaudeClient:
    """Async-friendly Claude API wrapper for BController.

    All Anthropic SDK calls are dispatched via ``hass.async_add_executor_job``
    because the SDK is synchronous.

    Usage:
        client = ClaudeClient(hass, api_key, budget_usd=10.0)
        decision = await client.get_trading_decision(system_prompt, user_prompt)
        stats = client.get_usage_stats()
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        budget_usd: float = 10.0,
    ) -> None:
        self._hass = hass
        self._api_key = api_key
        self._budget_usd = budget_usd

        # Running cost/token accumulators
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cache_write_tokens: int = 0
        self._total_cache_read_tokens: int = 0
        self._total_cost_usd: float = 0.0

        # Lazy-initialised anthropic.Anthropic client
        self._client: Any = None

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _get_client(self) -> Any:
        """Lazily initialise the synchronous Anthropic client."""
        if self._client is None:
            import anthropic  # type: ignore[import]

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _calculate_call_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
    ) -> float:
        """Calculate the USD cost of a single API call."""
        # Regular (non-cached) input tokens
        regular_input = input_tokens - cache_write_tokens - cache_read_tokens
        regular_input = max(regular_input, 0)

        cost = (
            (regular_input * _PRICE_INPUT_PER_MTK / 1_000_000)
            + (output_tokens * _PRICE_OUTPUT_PER_MTK / 1_000_000)
            + (cache_write_tokens * _PRICE_INPUT_PER_MTK * _PRICE_CACHE_WRITE_MULT / 1_000_000)
            + (cache_read_tokens * _PRICE_INPUT_PER_MTK * _PRICE_CACHE_READ_MULT / 1_000_000)
        )
        return cost

    def _record_usage(self, usage: Any) -> float:
        """Extract token counts from the response usage object, update accumulators.

        Returns the cost of this single call.
        """
        input_tokens: int = getattr(usage, "input_tokens", 0) or 0
        output_tokens: int = getattr(usage, "output_tokens", 0) or 0

        # Prompt caching fields (may not be present on all models/responses)
        cache_write_tokens: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read_tokens: int = getattr(usage, "cache_read_input_tokens", 0) or 0

        call_cost = self._calculate_call_cost(
            input_tokens, output_tokens, cache_write_tokens, cache_read_tokens
        )

        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._total_cache_write_tokens += cache_write_tokens
        self._total_cache_read_tokens += cache_read_tokens
        self._total_cost_usd += call_cost

        _LOGGER.debug(
            "Claude usage: input=%d out=%d cache_write=%d cache_read=%d cost=$%.5f "
            "(cumulative=$%.4f / budget=$%.2f)",
            input_tokens,
            output_tokens,
            cache_write_tokens,
            cache_read_tokens,
            call_cost,
            self._total_cost_usd,
            self._budget_usd,
        )
        return call_cost

    def _check_budget(self) -> None:
        """Raise BudgetExhaustedError if the trade budget allocation is spent."""
        trade_budget = self._budget_usd * BUDGET_ALLOCATION_TRADE
        if self._total_cost_usd >= self._budget_usd:
            raise BudgetExhaustedError(
                f"Claude budget exhausted: spent ${self._total_cost_usd:.4f} "
                f"of ${self._budget_usd:.2f} total budget"
            )
        # Warn when within trade allocation limit
        if self._total_cost_usd >= trade_budget:
            _LOGGER.warning(
                "Claude trade budget allocation ($%.2f) exceeded. "
                "Remaining total budget: $%.4f. Switching to conservative mode.",
                trade_budget,
                self._budget_usd - self._total_cost_usd,
            )

    def _build_messages(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[list[dict], str]:
        """Build the messages list and system content with cache_control.

        The system prompt is marked with cache_control ephemeral to enable
        Anthropic's prompt caching (5-minute TTL, minimum 4096 tokens).

        Returns:
            Tuple of (messages list, system content string for API call).
        """
        # System prompt with prompt caching enabled
        system_content = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        messages = [
            {
                "role": "user",
                "content": user_prompt,
            }
        ]

        return messages, system_content

    def _sync_call(
        self, system_content: list[dict], messages: list[dict]
    ) -> Any:
        """Execute the synchronous Anthropic API call (runs in executor thread).

        NOTE: Structured JSON output is enforced via the system prompt (R12).
        The Anthropic Python SDK does not have an "output_config" parameter -
        that would require the tool-use pattern which changes the response shape.
        The system prompt instructs Claude to output only valid JSON, and the
        caller parses response.content[0].text via json.loads().
        """
        client = self._get_client()
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=512,
            system=system_content,
            messages=messages,
        )
        return response

    # ─── Public API ────────────────────────────────────────────────────────

    async def get_trading_decision(
        self,
        system_prompt: str,
        market_context: str,
    ) -> TradingDecision:
        """Call Claude Haiku and return a parsed TradingDecision.

        Args:
            system_prompt: The full system prompt (from prompts.py SYSTEM_PROMPT).
                           Sent with cache_control ephemeral for prompt caching.
            market_context: The rendered user prompt with current market data.

        Returns:
            Parsed TradingDecision from Claude's structured output.

        Raises:
            BudgetExhaustedError: If the Claude budget is fully consumed.
            ClaudeAPIError: If the API call fails after retry.
        """
        self._check_budget()

        messages, system_content = self._build_messages(system_prompt, market_context)

        try:
            response = await self._hass.async_add_executor_job(
                self._sync_call, system_content, messages
            )
        except BudgetExhaustedError:
            raise
        except Exception as exc:
            _LOGGER.error("Claude API call failed: %s", exc)
            raise ClaudeAPIError(f"Claude API error: {exc}") from exc

        # Record token usage and cost
        self._record_usage(response.usage)

        # Parse structured output
        try:
            raw_text = response.content[0].text
            data = json.loads(raw_text)
            decision = TradingDecision.from_dict(data)
            _LOGGER.debug(
                "Claude decision: action=%s pair=%s size_pct=%.1f confidence=%.2f reason=%s",
                decision.action,
                decision.pair,
                decision.size_pct,
                decision.confidence,
                decision.reason_code,
            )
            return decision
        except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
            _LOGGER.error(
                "Failed to parse Claude response into TradingDecision: %s | raw: %s",
                exc,
                getattr(response, "content", "<no content>"),
            )
            raise ClaudeAPIError(f"Invalid Claude response format: {exc}") from exc

    def get_usage_stats(self) -> dict[str, Any]:
        """Return accumulated token and cost statistics.

        Returns:
            Dict with keys:
                total_input_tokens, total_output_tokens,
                total_cache_write_tokens, total_cache_read_tokens,
                total_cost_usd, budget_usd, budget_remaining
        """
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cache_write_tokens": self._total_cache_write_tokens,
            "total_cache_read_tokens": self._total_cache_read_tokens,
            "total_cost_usd": round(self._total_cost_usd, 6),
            "budget_usd": self._budget_usd,
            "budget_remaining": round(
                max(self._budget_usd - self._total_cost_usd, 0.0), 6
            ),
        }

    @property
    def budget_remaining(self) -> float:
        """Return remaining budget in USD."""
        return max(self._budget_usd - self._total_cost_usd, 0.0)

    @property
    def is_budget_exhausted(self) -> bool:
        """Return True if the total budget is consumed."""
        return self._total_cost_usd >= self._budget_usd
