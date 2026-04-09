"""Portfolio state management with HA Store persistence.

Maintains positions, balances, trade log, and portfolio snapshots.
Implements the startup recovery sequence (R14).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORAGE_KEY_PORTFOLIO,
    STORAGE_TRADE_LOG,
    STORAGE_VERSION,
)
from .models import (
    PortfolioSnapshot,
    Position,
    TradeRecord,
    TradingDecision,
)

if TYPE_CHECKING:
    from .trading import BinanceClient

_LOGGER = logging.getLogger(__name__)

# How long to keep portfolio snapshots (48h window covers L3 + recovery)
_SNAPSHOT_RETENTION_SECONDS = 172800  # 48h
# Snapshot interval — taken every full coordinator cycle
_MAX_SNAPSHOTS = 2880  # ~48h at 60s interval


class PortfolioState:
    """Manages all persistent portfolio state for BController.

    Thread-safety: All state mutations should happen on the HA event loop.
    The ``_lock`` asyncio.Lock prevents concurrent decision cycles from
    corrupting portfolio state (R14 mutex requirement).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store_portfolio: Store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_PORTFOLIO,
        )
        self._store_trades: Store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_TRADE_LOG,
        )
        self._lock = asyncio.Lock()

        # In-memory state (mirrors persistent store)
        self.positions: dict[str, Position] = {}
        self.cash_balance: float = 0.0        # USDT available for trading
        self.savings_balance: float = 0.0     # locked savings, USDT
        self.portfolio_snapshots: list[PortfolioSnapshot] = []
        self.trade_log: list[TradeRecord] = []
        self._initialised: bool = False

    # ─── Startup recovery (R14, mandatory before any trading) ─────────────

    async def async_load(self) -> None:
        """Load persisted state from HA storage (step 1 of startup sequence)."""
        portfolio_data = await self._store_portfolio.async_load()
        if portfolio_data:
            self._deserialise_portfolio(portfolio_data)
            _LOGGER.info(
                "Portfolio state loaded: %d positions, cash=%.2f USDT, savings=%.2f USDT",
                len(self.positions),
                self.cash_balance,
                self.savings_balance,
            )
        else:
            _LOGGER.info("No persisted portfolio state found. Starting fresh.")

        trade_data = await self._store_trades.async_load()
        if trade_data and "trades" in trade_data:
            self.trade_log = [
                TradeRecord.from_dict(t) for t in trade_data["trades"]
            ]
            _LOGGER.info("Trade log loaded: %d records", len(self.trade_log))

        self._initialised = True

    async def async_recover(self, binance_client: "BinanceClient") -> None:
        """Run the full startup recovery sequence (R14).

        Steps:
          2. Query Binance for ALL open orders
          3. Reconcile with persisted state
          4. Check account balances and update cash/savings
        """
        if not self._initialised:
            await self.async_load()

        # Step 2 — fetch all open orders
        try:
            open_orders = await binance_client.get_open_orders()
            _LOGGER.info(
                "Startup reconciliation: %d open orders on Binance", len(open_orders)
            )
        except Exception as exc:
            _LOGGER.error("Failed to fetch open orders during recovery: %s", exc)
            open_orders = []

        # Step 3 — reconcile
        await self._reconcile_orders(open_orders)

        # Step 4 — refresh balances from Binance
        try:
            account = await binance_client.get_account()
            await self._sync_balances_from_account(account)
        except Exception as exc:
            _LOGGER.error(
                "Failed to sync balances from Binance during recovery: %s", exc
            )

    async def _reconcile_orders(self, open_orders: list[dict]) -> None:
        """Reconcile Binance open orders against persisted positions.

        Any position whose pair has no matching open order is left as-is
        (already filled or cancelled externally).  This method logs
        discrepancies for the audit trail.
        """
        async with self._lock:
            binance_open_pairs = {o.get("symbol") for o in open_orders}
            for pair, position in list(self.positions.items()):
                if pair not in binance_open_pairs and position.quantity > 0:
                    _LOGGER.warning(
                        "Position %s has %.8f units but no open order on Binance. "
                        "May have been filled or cancelled externally. "
                        "Position retained in state for manual review.",
                        pair,
                        position.quantity,
                    )

    async def _sync_balances_from_account(self, account: dict) -> None:
        """Update cash_balance from Binance USDT free balance."""
        async with self._lock:
            balances = {b["asset"]: float(b["free"]) for b in account.get("balances", [])}
            usdt_free = balances.get("USDT", 0.0)
            # Savings are tracked separately; cash = free USDT minus savings
            self.cash_balance = max(0.0, usdt_free - self.savings_balance)
            _LOGGER.debug(
                "Balances synced: USDT free=%.2f, savings=%.2f, cash=%.2f",
                usdt_free,
                self.savings_balance,
                self.cash_balance,
            )
            await self._async_save_portfolio()

    # ─── Position management ───────────────────────────────────────────────

    async def open_position(
        self,
        pair: str,
        quantity: float,
        fill_price: float,
    ) -> Position:
        """Open a new position or DCA into an existing one."""
        async with self._lock:
            now = datetime.utcnow()
            if pair in self.positions:
                self.positions[pair].update_vwap(quantity, fill_price)
                self.positions[pair].buy_records.append({
                    "timestamp": now.isoformat(),
                    "quantity": quantity,
                    "price": fill_price,
                })
                _LOGGER.info(
                    "DCA into %s: +%.8f @ %.4f, new VWAP=%.4f",
                    pair,
                    quantity,
                    fill_price,
                    self.positions[pair].vwap_entry_price,
                )
            else:
                self.positions[pair] = Position(
                    pair=pair,
                    quantity=quantity,
                    vwap_entry_price=fill_price,
                    open_timestamp=now,
                    last_buy_timestamp=now,
                    cost_basis_usdt=quantity * fill_price,
                    buy_records=[{
                        "timestamp": now.isoformat(),
                        "quantity": quantity,
                        "price": fill_price,
                    }],
                )
                _LOGGER.info(
                    "Opened position %s: %.8f @ %.4f",
                    pair,
                    quantity,
                    fill_price,
                )

            invested = quantity * fill_price
            self.cash_balance = max(0.0, self.cash_balance - invested)
            await self._async_save_portfolio()
            return self.positions[pair]

    async def close_position(
        self,
        pair: str,
        quantity: float,
        fill_price: float,
    ) -> float:
        """Close (partial or full) position and return realized P&L in USDT."""
        async with self._lock:
            if pair not in self.positions:
                _LOGGER.error("Attempted to close non-existent position: %s", pair)
                return 0.0

            position = self.positions[pair]
            close_qty = min(quantity, position.quantity)
            proceeds = close_qty * fill_price
            cost = close_qty * position.vwap_entry_price
            pnl = proceeds - cost

            self.cash_balance += proceeds

            if close_qty >= position.quantity:
                del self.positions[pair]
                _LOGGER.info(
                    "Closed full position %s: %.8f @ %.4f, P&L=%.4f USDT",
                    pair,
                    close_qty,
                    fill_price,
                    pnl,
                )
            else:
                position.quantity -= close_qty
                position.cost_basis_usdt = position.quantity * position.vwap_entry_price
                _LOGGER.info(
                    "Partial close %s: -%.8f @ %.4f, P&L=%.4f USDT, remaining=%.8f",
                    pair,
                    close_qty,
                    fill_price,
                    pnl,
                    position.quantity,
                )

            await self._async_save_portfolio()
            return pnl

    # ─── Balance helpers ───────────────────────────────────────────────────

    async def apply_savings(self, amount: float) -> None:
        """Move ``amount`` USDT from cash to savings balance (R7)."""
        async with self._lock:
            transfer = min(amount, self.cash_balance)
            self.savings_balance += transfer
            self.cash_balance -= transfer
            _LOGGER.info(
                "Savings updated: +%.4f USDT, total savings=%.4f USDT",
                transfer,
                self.savings_balance,
            )
            await self._async_save_portfolio()

    def get_trading_balance(self) -> float:
        """Return cash available for trading (total cash minus savings)."""
        return self.cash_balance

    def get_total_value(self, price_map: dict[str, float]) -> float:
        """Calculate total portfolio value in USDT using current prices.

        ``price_map`` should map symbol → current price in USDT.
        """
        position_value = sum(
            pos.quantity * price_map.get(pos.pair, pos.vwap_entry_price)
            for pos in self.positions.values()
        )
        return self.cash_balance + self.savings_balance + position_value

    # ─── Portfolio snapshots ───────────────────────────────────────────────

    async def record_snapshot(
        self, total_value_usdt: float, btc_price_usdt: float
    ) -> None:
        """Append a timestamped portfolio value snapshot."""
        snap = PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_value_usdt=total_value_usdt,
            btc_price_usdt=btc_price_usdt,
        )
        self.portfolio_snapshots.append(snap)
        self._prune_snapshots()
        await self._async_save_portfolio()

    def _prune_snapshots(self) -> None:
        """Remove snapshots older than retention window."""
        cutoff = datetime.utcnow() - timedelta(seconds=_SNAPSHOT_RETENTION_SECONDS)
        self.portfolio_snapshots = [
            s for s in self.portfolio_snapshots if s.timestamp >= cutoff
        ]
        # Hard cap to avoid unbounded memory growth
        if len(self.portfolio_snapshots) > _MAX_SNAPSHOTS:
            self.portfolio_snapshots = self.portfolio_snapshots[-_MAX_SNAPSHOTS:]

    def get_value_at(self, seconds_ago: float) -> float | None:
        """Return portfolio value from ``seconds_ago`` seconds in the past.

        Returns the snapshot closest to that timestamp, or None if insufficient
        history is available.
        """
        target = datetime.utcnow() - timedelta(seconds=seconds_ago)
        if not self.portfolio_snapshots:
            return None
        # Find snapshot nearest to target time
        closest = min(
            self.portfolio_snapshots,
            key=lambda s: abs((s.timestamp - target).total_seconds()),
        )
        # Only return if the snapshot is within 2x the requested window
        delta = abs((closest.timestamp - target).total_seconds())
        if delta > seconds_ago * 2:
            return None
        return closest.total_value_usdt

    # ─── Trade log ────────────────────────────────────────────────────────

    async def record_trade(self, record: TradeRecord) -> None:
        """Append a trade record to the persistent trade log."""
        self.trade_log.append(record)
        await self._async_save_trades()

    async def update_trade_pnl(
        self,
        trade_id: str,
        pnl: float,
        pnl_pct: float,
        haltefrist_exceeded: bool = False,
    ) -> None:
        """Retrospectively update P&L on a trade record."""
        for record in self.trade_log:
            if record.id == trade_id:
                record.pnl = pnl
                record.pnl_pct = pnl_pct
                record.close_timestamp = datetime.utcnow()
                record.haltefrist_exceeded = haltefrist_exceeded
                await self._async_save_trades()
                return
        _LOGGER.warning("Trade record %s not found for P&L update", trade_id)

    def create_trade_record(
        self,
        decision: TradingDecision,
        source_weights: dict[str, float],
        executed: bool,
        reject_reason: str | None = None,
    ) -> TradeRecord:
        """Factory: create a new TradeRecord with a fresh UUID."""
        return TradeRecord(
            id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            decision=decision,
            source_weights=source_weights,
            executed=executed,
            reject_reason=reject_reason,
        )

    # ─── Trade statistics ─────────────────────────────────────────────────

    def get_trade_count_last_30d(self) -> int:
        """Return number of executed trades in the last 30 days."""
        cutoff = datetime.utcnow() - timedelta(days=30)
        return sum(
            1
            for t in self.trade_log
            if t.executed and t.timestamp >= cutoff
        )

    def get_realized_pnl_current_year(self) -> float:
        """Return total realized P&L for the current tax year in USDT."""
        year_start = datetime(datetime.utcnow().year, 1, 1)
        return sum(
            t.pnl
            for t in self.trade_log
            if t.executed and t.pnl is not None and t.timestamp >= year_start
        )

    # ─── Persistence helpers ───────────────────────────────────────────────

    def _deserialise_portfolio(self, data: dict) -> None:
        self.cash_balance = data.get("cash_balance", 0.0)
        self.savings_balance = data.get("savings_balance", 0.0)
        self.positions = {
            pair: Position.from_dict(pos_data)
            for pair, pos_data in data.get("positions", {}).items()
        }
        self.portfolio_snapshots = [
            PortfolioSnapshot.from_dict(s)
            for s in data.get("portfolio_snapshots", [])
        ]

    async def _async_save_portfolio(self) -> None:
        """Persist portfolio state to HA storage."""
        try:
            await self._store_portfolio.async_save({
                "cash_balance": self.cash_balance,
                "savings_balance": self.savings_balance,
                "positions": {
                    pair: pos.to_dict() for pair, pos in self.positions.items()
                },
                "portfolio_snapshots": [
                    s.to_dict() for s in self.portfolio_snapshots
                ],
            })
        except Exception as exc:
            _LOGGER.error("Failed to persist portfolio state: %s", exc)

    async def _async_save_trades(self) -> None:
        """Persist trade log to HA storage."""
        try:
            await self._store_trades.async_save({
                "trades": [t.to_dict() for t in self.trade_log],
            })
        except Exception as exc:
            _LOGGER.error("Failed to persist trade log: %s", exc)
