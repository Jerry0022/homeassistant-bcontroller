"""Data models for BController trading decisions and records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ─── Trading decision (Claude output schema, R12) ─────────────────────────────

@dataclass
class TradingDecision:
    """Structured trading decision produced by Claude Haiku.

    Matches the JSON schema defined in R12.  All trades are blocked unless
    confidence >= MIN_CONFIDENCE_THRESHOLD (0.6).
    """

    action: Literal["BUY", "SELL", "HOLD"]
    pair: str                    # e.g. "BTCUSDT"
    size_pct: float              # 0-10% of available trading balance
    confidence: float            # 0.0-1.0
    reason_code: Literal[
        "TREND_UP",
        "TREND_DOWN",
        "OVERSOLD",
        "OVERBOUGHT",
        "BREAKOUT",
        "NEWS_POSITIVE",
        "NEWS_NEGATIVE",
        "NEUTRAL",
    ]
    reasoning: str               # human-readable, logged for audit trail
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def from_dict(cls, data: dict) -> "TradingDecision":
        """Parse Claude's JSON response into a TradingDecision."""
        return cls(
            action=data["action"],
            pair=data["pair"],
            size_pct=float(data["size_pct"]),
            confidence=float(data["confidence"]),
            reason_code=data["reason_code"],
            reasoning=data["reasoning"],
        )

    def is_executable(self, min_confidence: float = 0.6) -> bool:
        """Return True if the decision meets the confidence threshold."""
        return self.action != "HOLD" and self.confidence >= min_confidence

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "pair": self.pair,
            "size_pct": self.size_pct,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp.isoformat(),
        }


# ─── Trade record (persisted log entry) ──────────────────────────────────────

@dataclass
class TradeRecord:
    """Full record of a single trading cycle, including outcome.

    The ``pnl`` field is filled retrospectively once a position is closed.
    """

    id: str                         # UUID
    timestamp: datetime
    decision: TradingDecision
    source_weights: dict[str, float]  # {"news": 0.25, "trend": 0.30, ...}
    executed: bool
    reject_reason: str | None = None  # why not executed (risk block, confidence, etc.)

    # Filled when the order lands on Binance
    order_id: str | None = None
    fill_price: float | None = None
    fill_quantity: float | None = None
    commission: float | None = None
    commission_asset: str | None = None

    # Filled retrospectively on close
    pnl: float | None = None         # realized P&L in USDT
    pnl_pct: float | None = None     # P&L as percentage of invested capital
    close_timestamp: datetime | None = None
    haltefrist_exceeded: bool | None = None  # >= 1 year hold → tax-free (DE)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "decision": self.decision.to_dict(),
            "source_weights": self.source_weights,
            "executed": self.executed,
            "reject_reason": self.reject_reason,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "commission": self.commission,
            "commission_asset": self.commission_asset,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "close_timestamp": self.close_timestamp.isoformat() if self.close_timestamp else None,
            "haltefrist_exceeded": self.haltefrist_exceeded,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TradeRecord":
        decision_raw = data["decision"]
        decision = TradingDecision(
            action=decision_raw["action"],
            pair=decision_raw["pair"],
            size_pct=decision_raw["size_pct"],
            confidence=decision_raw["confidence"],
            reason_code=decision_raw["reason_code"],
            reasoning=decision_raw["reasoning"],
            timestamp=datetime.fromisoformat(decision_raw["timestamp"]),
        )
        close_ts = data.get("close_timestamp")
        return cls(
            id=data["id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            decision=decision,
            source_weights=data["source_weights"],
            executed=data["executed"],
            reject_reason=data.get("reject_reason"),
            order_id=data.get("order_id"),
            fill_price=data.get("fill_price"),
            fill_quantity=data.get("fill_quantity"),
            commission=data.get("commission"),
            commission_asset=data.get("commission_asset"),
            pnl=data.get("pnl"),
            pnl_pct=data.get("pnl_pct"),
            close_timestamp=datetime.fromisoformat(close_ts) if close_ts else None,
            haltefrist_exceeded=data.get("haltefrist_exceeded"),
        )


# ─── Open position ────────────────────────────────────────────────────────────

@dataclass
class Position:
    """Tracks an open spot position with VWAP entry price for L1 stop-loss.

    VWAP is updated on each DCA buy in the same pair (R6).
    """

    pair: str
    quantity: float                       # total quantity held
    vwap_entry_price: float               # weighted average entry price
    open_timestamp: datetime
    last_buy_timestamp: datetime
    cost_basis_usdt: float                # total USDT spent (for P&L calc)
    buy_records: list[dict] = field(default_factory=list)  # individual lot records for FIFO

    def update_vwap(self, new_quantity: float, new_price: float) -> None:
        """Add a DCA buy and recalculate VWAP."""
        total_cost = self.cost_basis_usdt + (new_quantity * new_price)
        self.quantity += new_quantity
        self.cost_basis_usdt = total_cost
        self.vwap_entry_price = total_cost / self.quantity if self.quantity > 0 else 0.0
        self.last_buy_timestamp = datetime.utcnow()

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Return unrealized P&L as a percentage of VWAP entry price."""
        if self.vwap_entry_price == 0:
            return 0.0
        return ((current_price - self.vwap_entry_price) / self.vwap_entry_price) * 100.0

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "quantity": self.quantity,
            "vwap_entry_price": self.vwap_entry_price,
            "open_timestamp": self.open_timestamp.isoformat(),
            "last_buy_timestamp": self.last_buy_timestamp.isoformat(),
            "cost_basis_usdt": self.cost_basis_usdt,
            "buy_records": self.buy_records,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        return cls(
            pair=data["pair"],
            quantity=data["quantity"],
            vwap_entry_price=data["vwap_entry_price"],
            open_timestamp=datetime.fromisoformat(data["open_timestamp"]),
            last_buy_timestamp=datetime.fromisoformat(data["last_buy_timestamp"]),
            cost_basis_usdt=data["cost_basis_usdt"],
            buy_records=data.get("buy_records", []),
        )


# ─── Portfolio snapshot (for drawdown calc) ───────────────────────────────────

@dataclass
class PortfolioSnapshot:
    """Timestamped total portfolio value, used for L2/L3 drawdown checks."""

    timestamp: datetime
    total_value_usdt: float
    btc_price_usdt: float

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "total_value_usdt": self.total_value_usdt,
            "btc_price_usdt": self.btc_price_usdt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PortfolioSnapshot":
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            total_value_usdt=data["total_value_usdt"],
            btc_price_usdt=data["btc_price_usdt"],
        )


# ─── Risk state ───────────────────────────────────────────────────────────────

@dataclass
class RiskState:
    """Persisted risk state that survives HA restarts (R6)."""

    level: Literal["active", "paused", "halted", "stopped", "monitoring", "error"] = "active"

    # L2 — session pause (2h)
    l2_triggered_at: datetime | None = None
    l2_expires_at: datetime | None = None

    # L3 — daily halt (24h from trigger time)
    l3_triggered_at: datetime | None = None
    l3_expires_at: datetime | None = None

    # L4 — full stop (manual restart required)
    l4_triggered_at: datetime | None = None

    # Post-recovery reduced size window
    reduced_size_until: datetime | None = None

    # Monthly drawdown tracking (R9)
    l2l3_triggers_last_30d: list[str] = field(default_factory=list)  # ISO timestamps

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "l2_triggered_at": self.l2_triggered_at.isoformat() if self.l2_triggered_at else None,
            "l2_expires_at": self.l2_expires_at.isoformat() if self.l2_expires_at else None,
            "l3_triggered_at": self.l3_triggered_at.isoformat() if self.l3_triggered_at else None,
            "l3_expires_at": self.l3_expires_at.isoformat() if self.l3_expires_at else None,
            "l4_triggered_at": self.l4_triggered_at.isoformat() if self.l4_triggered_at else None,
            "reduced_size_until": self.reduced_size_until.isoformat() if self.reduced_size_until else None,
            "l2l3_triggers_last_30d": self.l2l3_triggers_last_30d,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RiskState":
        def _parse(val: str | None) -> datetime | None:
            return datetime.fromisoformat(val) if val else None

        return cls(
            level=data.get("level", "active"),
            l2_triggered_at=_parse(data.get("l2_triggered_at")),
            l2_expires_at=_parse(data.get("l2_expires_at")),
            l3_triggered_at=_parse(data.get("l3_triggered_at")),
            l3_expires_at=_parse(data.get("l3_expires_at")),
            l4_triggered_at=_parse(data.get("l4_triggered_at")),
            reduced_size_until=_parse(data.get("reduced_size_until")),
            l2l3_triggers_last_30d=data.get("l2l3_triggers_last_30d", []),
        )


# ─── Exchange filter info (cached from Binance) ───────────────────────────────

@dataclass
class ExchangeFilters:
    """Binance symbol filter values needed for order validation."""

    symbol: str
    min_qty: float
    max_qty: float
    step_size: float        # LOT_SIZE stepSize
    min_price: float
    max_price: float
    tick_size: float        # PRICE_FILTER tickSize
    min_notional: float     # MIN_NOTIONAL or NOTIONAL minNotional
    cached_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "min_qty": self.min_qty,
            "max_qty": self.max_qty,
            "step_size": self.step_size,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "tick_size": self.tick_size,
            "min_notional": self.min_notional,
            "cached_at": self.cached_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExchangeFilters":
        return cls(
            symbol=data["symbol"],
            min_qty=data["min_qty"],
            max_qty=data["max_qty"],
            step_size=data["step_size"],
            min_price=data["min_price"],
            max_price=data["max_price"],
            tick_size=data["tick_size"],
            min_notional=data["min_notional"],
            cached_at=datetime.fromisoformat(data["cached_at"]),
        )
