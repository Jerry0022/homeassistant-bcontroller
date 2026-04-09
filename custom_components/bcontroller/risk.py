"""Risk management for BController.

Implements the full stop-loss cascade (R6), savings rate application (R7),
capital protection (R8), and drawdown recovery (R9).

State is persisted via HA Store so halts/pauses survive restarts (R6).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_CASH_RESERVE_PCT,
    DEFAULT_SAVINGS_RATE,
    DEFAULT_STOP_LOSS_L1,
    DEFAULT_STOP_LOSS_L2,
    DEFAULT_STOP_LOSS_L3,
    DEFAULT_STOP_LOSS_L4,
    L2_PAUSE_SECONDS,
    L3_HALT_SECONDS,
    MAX_SLIPPAGE_PCT,
    MIN_STOP_LOSS_L1,
    MIN_STOP_LOSS_L4,
    POST_L2L3_POSITION_REDUCTION_PCT,
    POST_L2L3_RECOVERY_SECONDS,
    STORAGE_KEY_RISK,
    STORAGE_VERSION,
    STATUS_ACTIVE,
    STATUS_HALTED,
    STATUS_MONITORING,
    STATUS_PAUSED,
    STATUS_STOPPED,
)
from .models import Position, RiskState

_LOGGER = logging.getLogger(__name__)


class TradeBlockedError(Exception):
    """Raised by check_trade_allowed when trading is blocked."""

    def __init__(self, reason: str, level: str) -> None:
        super().__init__(reason)
        self.level = level


class RiskManager:
    """Enforces all risk rules for BController.

    Usage pattern:
      1. Call ``async_load()`` once during integration setup.
      2. Before each decision cycle: call ``check_trade_allowed()``.
      3. Before executing a trade: call ``check_stop_loss()`` on each open position.
      4. After each portfolio snapshot: call ``check_portfolio_drawdown()``.
      5. Continuously: call ``check_total_drawdown()`` against latest portfolio value.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        stop_loss_l1: float = DEFAULT_STOP_LOSS_L1,
        stop_loss_l2: float = DEFAULT_STOP_LOSS_L2,
        stop_loss_l3: float = DEFAULT_STOP_LOSS_L3,
        stop_loss_l4: float = DEFAULT_STOP_LOSS_L4,
        savings_rate: float = DEFAULT_SAVINGS_RATE,
        cash_reserve_pct: float = DEFAULT_CASH_RESERVE_PCT,
    ) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY_RISK)

        # Clamp to hard minimums (R6)
        # Use min() because thresholds are negative: min(-1, -2) = -2 (stricter)
        # This prevents setting a threshold looser than the hard minimum.
        self.stop_loss_l1 = min(stop_loss_l1, MIN_STOP_LOSS_L1)
        self.stop_loss_l2 = stop_loss_l2
        self.stop_loss_l3 = stop_loss_l3
        self.stop_loss_l4 = min(stop_loss_l4, MIN_STOP_LOSS_L4)
        self.savings_rate = min(savings_rate, 20.0)
        self.cash_reserve_pct = cash_reserve_pct

        self._state: RiskState = RiskState()
        self._budget_exhausted: bool = False

    # ─── Persistence ───────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load persisted risk state from HA storage."""
        data = await self._store.async_load()
        if data:
            self._state = RiskState.from_dict(data)
            _LOGGER.info("Risk state loaded: level=%s", self._state.level)
        else:
            _LOGGER.info("No persisted risk state found. Starting in 'active' state.")

    async def _async_save(self) -> None:
        try:
            await self._store.async_save(self._state.to_dict())
        except Exception as exc:
            _LOGGER.error("Failed to persist risk state: %s", exc)

    # ─── State accessors ───────────────────────────────────────────────────

    def get_state(self) -> str:
        """Return the current system state string."""
        if self._budget_exhausted and self._state.level == STATUS_ACTIVE:
            return STATUS_MONITORING
        return self._state.level

    def is_l4_active(self) -> bool:
        return self._state.level == STATUS_STOPPED

    def is_trading_allowed(self) -> bool:
        """Quick check — prefer check_trade_allowed() for error details."""
        try:
            self.check_trade_allowed_sync()
            return True
        except TradeBlockedError:
            return False

    # ─── Pre-trade gate ────────────────────────────────────────────────────

    def check_trade_allowed_sync(self) -> None:
        """Synchronous pre-trade check — raises TradeBlockedError if blocked.

        Checks (in order):
          1. L4 full stop (manual restart required)
          2. L3 daily halt (expires automatically)
          3. L2 session pause (expires automatically)
          4. Budget exhausted → monitoring-only mode
        """
        now = datetime.utcnow()

        # L4 — full stop, requires manual restart
        if self._state.level == STATUS_STOPPED:
            raise TradeBlockedError(
                f"L4 Emergency Stop active since {self._state.l4_triggered_at}. "
                "Manual restart required.",
                "L4",
            )

        # L3 — daily halt
        if self._state.level == STATUS_HALTED:
            if self._state.l3_expires_at and now < self._state.l3_expires_at:
                raise TradeBlockedError(
                    f"L3 Daily Halt active until {self._state.l3_expires_at}.",
                    "L3",
                )
            # Auto-expire L3
            _LOGGER.info("L3 halt expired. Resuming to active state.")
            self._state.level = STATUS_ACTIVE
            self._state.l3_triggered_at = None
            self._state.l3_expires_at = None

        # L2 — session pause
        if self._state.level == STATUS_PAUSED:
            if self._state.l2_expires_at and now < self._state.l2_expires_at:
                raise TradeBlockedError(
                    f"L2 Session Pause active until {self._state.l2_expires_at}.",
                    "L2",
                )
            # Auto-expire L2
            _LOGGER.info("L2 pause expired. Resuming to active state.")
            self._state.level = STATUS_ACTIVE
            self._state.l2_triggered_at = None
            self._state.l2_expires_at = None

        # Budget exhausted → no new trades
        if self._budget_exhausted:
            raise TradeBlockedError(
                "Claude API budget exhausted. Monitoring-only mode.",
                "budget",
            )

    async def check_trade_allowed(self) -> None:
        """Async pre-trade gate. Saves state if L2/L3 expired during check."""
        prev_level = self._state.level
        self.check_trade_allowed_sync()
        if self._state.level != prev_level:
            await self._async_save()

    # ─── L1 — per-position stop-loss ──────────────────────────────────────

    def check_stop_loss(self, position: Position, current_price: float) -> bool:
        """Return True if L1 stop-loss is triggered for this position.

        L1 compares current price against VWAP entry price (R6).
        """
        if position.vwap_entry_price <= 0:
            return False
        change_pct = (
            (current_price - position.vwap_entry_price) / position.vwap_entry_price
        ) * 100.0

        if change_pct <= self.stop_loss_l1:
            _LOGGER.warning(
                "L1 stop-loss triggered for %s: %.2f%% drawdown from VWAP %.4f "
                "(current: %.4f, threshold: %.1f%%)",
                position.pair,
                change_pct,
                position.vwap_entry_price,
                current_price,
                self.stop_loss_l1,
            )
            return True
        return False

    # ─── L2/L3 — portfolio drawdown ────────────────────────────────────────

    async def check_portfolio_drawdown(
        self,
        current_value: float,
        value_1h_ago: float | None,
        value_24h_ago: float | None,
    ) -> str | None:
        """Check L2 (1h) and L3 (24h) drawdown levels.

        Returns the triggered level string ("L2" or "L3"), or None.
        Cascade escalation: if L2 is active when L3 triggers, L3 takes over.
        """
        triggered = None
        now = datetime.utcnow()

        # L3 check (24h)
        if value_24h_ago is not None and value_24h_ago > 0:
            drawdown_24h = ((current_value - value_24h_ago) / value_24h_ago) * 100.0
            if drawdown_24h <= self.stop_loss_l3:
                _LOGGER.warning(
                    "L3 Daily Halt triggered: 24h drawdown=%.2f%% (threshold=%.1f%%)",
                    drawdown_24h,
                    self.stop_loss_l3,
                )
                await self._trigger_l3(now)
                triggered = "L3"

        # L2 check (1h) — only if L3 not already triggered
        if triggered is None and value_1h_ago is not None and value_1h_ago > 0:
            drawdown_1h = ((current_value - value_1h_ago) / value_1h_ago) * 100.0
            if drawdown_1h <= self.stop_loss_l2:
                _LOGGER.warning(
                    "L2 Session Pause triggered: 1h drawdown=%.2f%% (threshold=%.1f%%)",
                    drawdown_1h,
                    self.stop_loss_l2,
                )
                await self._trigger_l2(now)
                triggered = "L2"

        return triggered

    async def _trigger_l2(self, now: datetime) -> None:
        """Activate L2 session pause (2h)."""
        # If L3 is already active, do not downgrade
        if self._state.level in (STATUS_HALTED, STATUS_STOPPED):
            return

        self._state.level = STATUS_PAUSED
        self._state.l2_triggered_at = now
        self._state.l2_expires_at = now + timedelta(seconds=L2_PAUSE_SECONDS)

        # Post-recovery size reduction
        self._state.reduced_size_until = now + timedelta(seconds=POST_L2L3_RECOVERY_SECONDS)

        # Track for R9 30-day recurrence check
        self._state.l2l3_triggers_last_30d.append(now.isoformat())
        self._prune_trigger_history()

        await self._async_save()

    async def _trigger_l3(self, now: datetime) -> None:
        """Activate L3 daily halt (24h). Escalates from L2 if active."""
        if self._state.level == STATUS_STOPPED:
            return

        self._state.level = STATUS_HALTED
        self._state.l3_triggered_at = now
        self._state.l3_expires_at = now + timedelta(seconds=L3_HALT_SECONDS)

        # Clear L2 state — L3 takes over
        self._state.l2_triggered_at = None
        self._state.l2_expires_at = None

        # Post-recovery size reduction
        self._state.reduced_size_until = now + timedelta(seconds=POST_L2L3_RECOVERY_SECONDS)

        self._state.l2l3_triggers_last_30d.append(now.isoformat())
        self._prune_trigger_history()

        await self._async_save()

    # ─── L4 — total drawdown emergency stop ────────────────────────────────

    async def check_total_drawdown(
        self, current_value: float, initial_value: float
    ) -> bool:
        """Check L4 total portfolio drawdown.

        Returns True if L4 was triggered (or already active).
        L4 state persists across restarts and requires manual reset.
        """
        if self._state.level == STATUS_STOPPED:
            return True

        if initial_value <= 0:
            return False

        total_drawdown = ((current_value - initial_value) / initial_value) * 100.0
        if total_drawdown <= self.stop_loss_l4:
            _LOGGER.critical(
                "L4 Emergency Stop triggered: total drawdown=%.2f%% (threshold=%.1f%%)",
                total_drawdown,
                self.stop_loss_l4,
            )
            await self._trigger_l4()
            return True
        return False

    async def _trigger_l4(self) -> None:
        """Activate L4 full stop. This state requires manual restart."""
        self._state.level = STATUS_STOPPED
        self._state.l4_triggered_at = datetime.utcnow()
        # Clear lower-level states
        self._state.l2_triggered_at = None
        self._state.l2_expires_at = None
        self._state.l3_triggered_at = None
        self._state.l3_expires_at = None
        await self._async_save()

    async def manual_reset_l4(self) -> None:
        """Reset L4 state — only called by explicit user action from dashboard."""
        if self._state.level != STATUS_STOPPED:
            _LOGGER.warning("manual_reset_l4 called but L4 is not active")
            return
        _LOGGER.info("L4 Emergency Stop manually reset by user.")
        self._state.level = STATUS_ACTIVE
        self._state.l4_triggered_at = None
        # Enforce post-recovery size reduction after manual restart
        self._state.reduced_size_until = datetime.utcnow() + timedelta(
            seconds=POST_L2L3_RECOVERY_SECONDS
        )
        await self._async_save()

    # ─── Savings rate ──────────────────────────────────────────────────────

    def apply_savings_rate(self, realized_profit: float) -> float:
        """Calculate amount to move to savings from a realized profit.

        Returns the USDT amount to transfer to savings.
        Savings rate is clamped to 0-20% (R7).
        """
        if realized_profit <= 0:
            return 0.0
        rate = max(0.0, min(self.savings_rate, 20.0))
        return realized_profit * (rate / 100.0)

    # ─── Position size adjustment (post-L2/L3 recovery, R9) ───────────────

    def get_position_size_multiplier(self) -> float:
        """Return size multiplier for the current risk state.

        Returns 0.5 if in the 24h reduced-size recovery window post-L2/L3.
        Returns 0.0 if trading is blocked.
        Returns 1.0 otherwise.
        """
        now = datetime.utcnow()
        if self._state.level in (STATUS_STOPPED, STATUS_HALTED, STATUS_PAUSED):
            return 0.0
        if (
            self._state.reduced_size_until
            and now < self._state.reduced_size_until
        ):
            return (100.0 - POST_L2L3_POSITION_REDUCTION_PCT) / 100.0
        return 1.0

    # ─── Cash reserve check (R8) ───────────────────────────────────────────

    def check_cash_reserve(
        self, cash_balance: float, total_portfolio_value: float
    ) -> bool:
        """Return True if cash reserve is healthy (>= required %).

        If below threshold: no new positions should be opened (R8).
        """
        if total_portfolio_value <= 0:
            return True
        reserve_pct = (cash_balance / total_portfolio_value) * 100.0
        if reserve_pct < self.cash_reserve_pct:
            _LOGGER.info(
                "Cash reserve %.1f%% below required %.1f%%. No new positions.",
                reserve_pct,
                self.cash_reserve_pct,
            )
            return False
        return True

    # ─── Slippage protection helper (R6) ──────────────────────────────────

    @staticmethod
    def should_use_limit_order(estimated_slippage_pct: float) -> bool:
        """Return True if slippage estimate exceeds threshold."""
        return estimated_slippage_pct > MAX_SLIPPAGE_PCT

    # ─── Budget management (R11) ───────────────────────────────────────────

    def set_budget_exhausted(self, exhausted: bool) -> None:
        """Signal that the Claude API budget has been exhausted (R11).

        When exhausted: new trades are blocked, monitoring continues.
        Risk/stop-loss checks always proceed regardless.
        """
        if exhausted != self._budget_exhausted:
            _LOGGER.info(
                "Claude budget exhausted state changed: %s → %s",
                self._budget_exhausted,
                exhausted,
            )
        self._budget_exhausted = exhausted

    @property
    def budget_exhausted(self) -> bool:
        return self._budget_exhausted

    # ─── R9 — 30-day recurrence tracking ──────────────────────────────────

    def get_monthly_trigger_count(self) -> int:
        """Return count of L2/L3 triggers in the last 30 days."""
        return len(self._state.l2l3_triggers_last_30d)

    def _prune_trigger_history(self) -> None:
        """Remove trigger timestamps older than 30 days."""
        cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
        self._state.l2l3_triggers_last_30d = [
            ts for ts in self._state.l2l3_triggers_last_30d if ts >= cutoff
        ]

    # ─── API key permission check (R13) ────────────────────────────────────

    @staticmethod
    def check_api_permissions(account_info: dict) -> list[str]:
        """Check for dangerous API key permissions.

        Returns a list of warning strings if withdrawal or other risky
        permissions are detected.  Called during startup (R13).
        """
        warnings = []
        if account_info.get("canWithdraw", False):
            warnings.append(
                "CRITICAL: Binance API key has withdrawal permission enabled! "
                "Disable it immediately in Binance API Management."
            )
        if account_info.get("canDeposit", False) and not account_info.get("canTrade", False):
            warnings.append(
                "WARNING: API key cannot trade but has deposit permission. "
                "Verify key configuration."
            )
        return warnings

    # ─── Expose risk state for sensors ────────────────────────────────────

    @property
    def state(self) -> RiskState:
        return self._state
