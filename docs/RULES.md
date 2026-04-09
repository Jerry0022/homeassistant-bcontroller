# BController — Rules & Constraints

## Trading Rules

### R1 — Spot Only
No margin trading, no futures, no leverage. Only spot buy/sell.
Violation = system shutdown.

### R2 — Trade Timing
- Minimum interval between trades: 15 minutes
- Maximum interval: 4 hours
- Interval is randomized (uniform distribution + jitter)
- No two trades within 60 seconds of each other
- No trading bursts (max 3 trades per hour)

### R3 — Binance Compliance
- Respect all Binance trading rules (LOT_SIZE, MIN_NOTIONAL, PRICE_FILTER)
- Fetch and cache exchange info daily; refresh on 400 errors
- Stay below 50% of rate limits (6,000 weight/min → max 3,000 weight/min)
- Use request weight tracking to avoid hitting limits
- Implement exponential backoff on 429/418 responses

### R4 — Order Sizes
- Never trade more than 10% of portfolio in a single order
- Minimum trade: Binance NOTIONAL filter (usually 5 USDT)
- Maximum trade: configurable, default 100 USDT per order

### R5 — Pair Restrictions
- Only trade pairs from user-configured whitelist
- Default whitelist: BTC/USDT, ETH/USDT, BNB/USDT
- No meme coins, no newly listed tokens (must be listed >90 days)

---

## Risk Management Rules

### R6 — Stop-Loss Cascade (Non-Negotiable)
```
L1: Trade -3%      → Close position
L2: Portfolio -5%/h → Pause 2h
L3: Portfolio -8%/d → Halt for 24h from trigger time
L4: Portfolio -15%  → FULL STOP (manual restart required)
```
These levels are configurable but have hard minimums:
- L1 minimum: -2%
- L4 minimum: -10%
Users cannot disable stop-loss entirely.

**L1 Reference Price:** Weighted average entry price (VWAP) across all buys
in the current position. For DCA positions, each buy updates the VWAP.

**State Persistence:** L2/L3/L4 states MUST be stored in HA persistent storage
(not in-memory). After HA restart, the system reads persisted state and
respects active pauses/halts. L4 state persists across restarts — no
automatic reset.

**Cascade Escalation:** If L2 is active and L3 condition is met during the
pause window, L3 takes over immediately (escalation, not replacement).

**Slippage Protection:** Market orders include a maximum slippage parameter.
If estimated slippage exceeds 1%, use limit order instead of market order.

**All limits are percentage-based** — no absolute EUR caps. This scales
naturally with any portfolio size. The 10% per-trade and 20% cash-reserve
rules protect proportionally regardless of total capital.

### R7 — Savings Rate
- Range: 0-20% of realized profit
- Applied immediately when profit is realized
- Savings balance is read-only for the trading engine
- Savings displayed in EUR (converted via Binance ticker)
- Withdrawal only via manual user action in dashboard

### R8 — Capital Protection
- Never invest more than 80% of available balance
- Keep 20% as cash reserve (USDT) at all times
- If cash reserve drops below 20%: reduce positions, don't open new ones

### R9 — Drawdown Recovery
- After L2/L3 trigger: reduce position sizes by 50% for next 24h
- After L4 trigger: require manual restart + review period
- Track maximum drawdown; if exceeded 3x in 30 days → suggest manual review

---

## Intelligence Rules

### R10 — Source Diversity
- Minimum 3 independent data sources per decision
- No single source weight > 40%
- No single source weight < 10%
- Source weights must be logged with every decision
- Historical learning weight increases with track record (min 10%, max 40%)

### R11 — Claude Haiku as Decision Engine (Strictly Enforced)
- Model: **claude-haiku-4-5-20251001** — hardcoded, no automatic upgrades
- Claude is not just an analyzer — it makes the **final trading decision**
- Input: cached data bundle from all sources (news, trends, recommendations, history)
- Output: structured decision (action, pair, amount, confidence, reasoning)
- Data is cached locally; Claude receives the bundle, not raw API calls
- Default budget: 5h equivalent per month (~$10 on Haiku)
- Budget allocation priority:
  1. Emergency risk checks + stop-loss decisions (unlimited, bypasses budget)
  2. Active trade decisions — Claude's core function (60% of budget)
  3. Market research/news analysis (30% of budget)
  4. Historical pattern learning (10% of budget)
- When budget exhausted: monitoring-only mode (no new trades)
- Risk/stop-loss checks ALWAYS get budget, even when limit exceeded

### R12 — Decision Schema & Logging

Claude returns structured JSON for every decision:
```json
{
  "action": "BUY|SELL|HOLD",
  "pair": "BTCUSDT",
  "size_pct": 5.0,
  "confidence": 0.85,
  "reason_code": "TREND_UP|TREND_DOWN|OVERSOLD|OVERBOUGHT|BREAKOUT|NEWS_POSITIVE|NEWS_NEGATIVE|NEUTRAL",
  "reasoning": "Short explanation of the decision"
}
```
- `size_pct`: percentage of available trading balance (0-10%, respecting R4+R8)
- `confidence`: 0.0-1.0, trades only executed if >= 0.6
- `reason_code`: machine-readable classification for dashboard analytics
- `reasoning`: human-readable explanation, logged for audit trail

Every decision is logged with:
  - Timestamp (UTC)
  - Full Claude response (structured JSON above)
  - Source inputs (raw data summary)
  - Source weights at time of decision
  - Outcome (tracked retrospectively: realized P&L)

---

## Operational Rules

### R13 — Binance API Key Security
- API keys stored in HA config_entries (.storage/ — plaintext, known HA limitation)
- Machine must be physically secured; no remote .storage/ access
- Keys must have spot trading permission only
- No withdrawal permission on API key (critical!)
- IP whitelist on Binance restricted to HA server IP (recommended)
- Startup check: warn user if API key has withdrawal permission enabled

### R14 — Error Handling & Recovery
- Network errors: retry with exponential backoff (max 3 retries)
- Binance API errors: log + pause trading for 5 minutes
- Binance 429: immediate backoff, read Retry-After header
- Binance 418: auto-ban detected, halt trading, alert user
- Claude API errors: **Conservative Fallback** — sell all open positions,
  hold cash (USDT), stop-loss remains active, no new buys until Claude returns
- HA restart recovery sequence (MANDATORY, runs before any trading):
  1. Load persisted L2/L3/L4 state — respect active pauses/halts
  2. Query Binance for ALL open orders (GET /api/v3/openOrders)
  3. Reconcile open orders with local state
  4. Check account balances
  5. Only then: resume normal decision cycle
- Order confirmation: after placing an order, poll order status if no
  immediate confirmation (handles network timeout mid-execution)
- Mutex: portfolio state changes are locked — no concurrent decision
  cycles can modify positions simultaneously

### R15 — Auditability & Tax Export
- All trades logged with full context
- Decision history retained for 10 years (German retention period)
- Performance metrics calculated honestly (no survivorship bias)
- **Tax Export (Finanzamt-ready):**
  - Downloadable CSV from dashboard with one click
  - Format: Anlage SO compatible (§ 23 EStG, private Veräußerungsgeschäfte)
  - Fields: Datum, Coin, Kauf/Verkauf, Menge, Kurs EUR, Gebühren EUR, G/V EUR
  - FIFO method for cost basis calculation (Anschaffungskosten)
  - Annual summary: Gesamtgewinn, Gesamtverlust, Saldo
  - Freigrenze-check: alert when gains < 1000 EUR (since 2024)
  - Instructions in dashboard: exactly where to enter in ELSTER
    (Sonstige Einkünfte → Private Veräußerungsgeschäfte, Zeile 41-46)

---

## Regulatory Notes (Germany/EU)

### Tax
- Crypto held < 1 year: taxable as private sale (§ 23 EStG)
- Crypto held > 1 year: tax-free
- Since BController trades frequently: all gains are taxable
- Export function must provide data for Anlage SO

### MiCA
- Private spot trading: no license required (MiCA Art. 2(4))
- Automated trading for personal use: permitted
- No third-party fund management (only own funds)
- Open-source distribution: permitted (no service character)

### Gewerbesteuer Risk
- High trading frequency MAY be classified as gewerblicher Handel by
  the Finanzamt (different tax rules, no Haltefrist benefit, Gewerbesteuer)
- Dashboard displays a notice when trade count exceeds 100/month
- User should consult Steuerberater if trading is their primary activity

### Haltefrist Tracking
- Each SELL records whether the sold units exceeded the 1-year Haltefrist
- FIFO is calculated per-exchange (Binance only — BFH IX R 3/22)
- Tax export CSV includes column: "Haltefrist überschritten: ja/nein"
- Disclaimer in export: "Nur BController/Binance-Transaktionen. Andere
  Plattformen separat erfassen."

### Stablecoin Risk Disclaimer
- Savings held as USDT/USDC carry counterparty risk (see FTX, USDC de-peg 2023)
- Dashboard displays: "Savings verbleiben auf Binance, nicht in eigener Custody"
- EUR display shows approximate value with note: "Richtwert, kein garantierter Kurs"

### Documentation
- Keep trade logs for 10 years (German retention period)
- Document all algorithmic decisions for tax audit trail

### Binance Germany Notice
- Binance closed its BaFin-regulated German entity in 2023
- Access for German users may be restricted or in regulatory grey area
- Dashboard displays prominent notice about this risk at first setup
- User must acknowledge the regulatory situation before activation
