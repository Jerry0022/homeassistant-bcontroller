"""Constants for the BController integration."""

from __future__ import annotations

# Integration domain
DOMAIN = "bcontroller"

# ─── Config entry keys ───────────────────────────────────────────────────────
CONF_BINANCE_API_KEY = "binance_api_key"
CONF_BINANCE_API_SECRET = "binance_api_secret"
CONF_CLAUDE_API_KEY = "claude_api_key"
CONF_POLL_INTERVAL = "poll_interval"
CONF_STOP_LOSS_L1 = "stop_loss_l1"
CONF_STOP_LOSS_L2 = "stop_loss_l2"
CONF_STOP_LOSS_L3 = "stop_loss_l3"
CONF_STOP_LOSS_L4 = "stop_loss_l4"
CONF_SAVINGS_RATE = "savings_rate"
CONF_CASH_RESERVE_PCT = "cash_reserve_pct"
CONF_PAIR_WHITELIST = "pair_whitelist"
CONF_MAX_TRADE_USDT = "max_trade_usdt"
CONF_CLAUDE_BUDGET_USD = "claude_budget_usd"

# ─── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL: int = 120          # seconds
DEFAULT_STOP_LOSS_L1: float = -3.0        # percent
DEFAULT_STOP_LOSS_L2: float = -5.0        # percent, portfolio in 1h
DEFAULT_STOP_LOSS_L3: float = -8.0        # percent, portfolio in 24h
DEFAULT_STOP_LOSS_L4: float = -15.0       # percent, total portfolio
DEFAULT_SAVINGS_RATE: float = 10.0        # percent of realized profit
DEFAULT_CASH_RESERVE_PCT: float = 20.0    # percent of total portfolio
DEFAULT_MAX_TRADE_USDT: float = 100.0     # USDT cap per single order
DEFAULT_CLAUDE_BUDGET_USD: float = 10.0   # USD per month
DEFAULT_PAIR_WHITELIST: list[str] = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

# ─── Hard limits (non-negotiable) ─────────────────────────────────────────────
MIN_STOP_LOSS_L1: float = -2.0            # cannot be looser than -2%
MIN_STOP_LOSS_L4: float = -10.0           # cannot be looser than -10%
MAX_SAVINGS_RATE: float = 20.0            # 0-20%
MAX_TRADE_SIZE_PCT: float = 10.0          # 10% of trading balance per order
MIN_CONFIDENCE_THRESHOLD: float = 0.6     # trades only executed if confidence >= 0.6
MIN_POLL_INTERVAL: int = 60               # seconds
MAX_POLL_INTERVAL: int = 14400            # 4 hours

# ─── Binance rate limiting ─────────────────────────────────────────────────────
BINANCE_WEIGHT_LIMIT: int = 6000          # per-minute limit
BINANCE_WEIGHT_SAFE: int = 3000           # 50% buffer — block if exceeded

# ─── Trade timing (R2) ────────────────────────────────────────────────────────
TRADE_MIN_INTERVAL_SEC: int = 900         # 15 minutes
TRADE_MAX_INTERVAL_SEC: int = 14400       # 4 hours
TRADE_MIN_GAP_SEC: int = 60              # no two trades within 60s
TRADE_MAX_PER_HOUR: int = 3              # burst limit

# ─── Stop-loss pause/halt durations ───────────────────────────────────────────
L2_PAUSE_SECONDS: int = 7200             # 2 hours
L3_HALT_SECONDS: int = 86400            # 24 hours

# ─── Post-stop-loss recovery ──────────────────────────────────────────────────
POST_L2L3_POSITION_REDUCTION_PCT: float = 50.0  # reduce sizes by 50% for 24h
POST_L2L3_RECOVERY_SECONDS: int = 86400         # 24h reduced size window

# ─── Claude / AI ──────────────────────────────────────────────────────────────
MODEL_ID: str = "claude-haiku-4-5-20251001"     # hardcoded — no upgrades without approval

# Claude budget allocation (R11)
BUDGET_ALLOCATION_RISK: float = 0.0      # unlimited for risk/emergency
BUDGET_ALLOCATION_TRADE: float = 0.60    # 60% for trade decisions
BUDGET_ALLOCATION_RESEARCH: float = 0.30 # 30% for market research
BUDGET_ALLOCATION_HISTORY: float = 0.10  # 10% for historical learning

# Source weight constraints (R10)
SOURCE_WEIGHT_MIN: float = 0.10
SOURCE_WEIGHT_MAX: float = 0.40

# ─── Slippage protection ──────────────────────────────────────────────────────
MAX_SLIPPAGE_PCT: float = 1.0            # above 1%: use limit order

# ─── Excess trade count warning (Gewerbesteuer risk) ─────────────────────────
GEWERBESTEUER_TRADE_THRESHOLD: int = 100  # trades per month

# ─── HA storage keys ──────────────────────────────────────────────────────────
STORAGE_KEY_PORTFOLIO = f"{DOMAIN}_portfolio"
STORAGE_KEY_RISK = f"{DOMAIN}_risk"
STORAGE_TRADE_LOG = f"{DOMAIN}_trade_log"
STORAGE_VERSION = 1

# ─── Coordinator / entry data keys ────────────────────────────────────────────
DATA_COORDINATOR = "coordinator"
DATA_PORTFOLIO = "portfolio"
DATA_RISK_MANAGER = "risk_manager"
DATA_BINANCE_CLIENT = "binance_client"

# ─── Sensor entity IDs ────────────────────────────────────────────────────────
SENSOR_BTC_PRICE = "btc_price"
SENSOR_PORTFOLIO_VALUE = "portfolio_value"
SENSOR_SAVINGS_BALANCE = "savings_balance"
SENSOR_SYSTEM_STATUS = "system_status"
SENSOR_LAST_DECISION = "last_decision"

# ─── System status values ─────────────────────────────────────────────────────
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"       # L2
STATUS_HALTED = "halted"       # L3
STATUS_STOPPED = "stopped"     # L4
STATUS_MONITORING = "monitoring"  # budget exhausted
STATUS_ERROR = "error"
