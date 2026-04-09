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
- Stay below 50% of rate limits (1200 req/min limit → max 600 req/min)
- Use request weight tracking to avoid hitting limits
- Implement exponential backoff on 429/418 responses

### R4 — Order Sizes
- Never trade more than 10% of portfolio in a single order
- Minimum trade: Binance MIN_NOTIONAL (usually 10 USDT)
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
L3: Portfolio -8%/d → Halt until next day
L4: Portfolio -15%  → FULL STOP (manual restart)
```
These levels are configurable but have hard minimums:
- L1 minimum: -2%
- L4 minimum: -10%
Users cannot disable stop-loss entirely.

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

### R12 — Decision Logging
- Every decision must be logged with:
  - Timestamp
  - Source inputs (raw data summary)
  - Source weights at time of decision
  - Claude analysis output
  - Confidence score (0-100)
  - Action taken (buy/sell/hold)
  - Outcome (tracked retrospectively)

---

## Operational Rules

### R13 — Binance API Key Security
- API keys stored in HA secrets (encrypted at rest)
- Keys must have spot trading permission only
- No withdrawal permission on API key (critical!)
- IP whitelist recommended (documented in setup)

### R14 — Error Handling
- Network errors: retry with exponential backoff (max 3 retries)
- Binance API errors: log + pause trading for 5 minutes
- Claude API errors: fall back to technical-indicators-only mode
- HA restart: gracefully resume, check open orders first

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
- Private spot trading: no license required
- Automated trading for personal use: permitted
- No third-party fund management (only own funds)

### Documentation
- Keep trade logs for 10 years (German retention period)
- Document all algorithmic decisions for tax audit trail
