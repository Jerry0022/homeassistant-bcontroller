# BController — Concept Document

## Vision

A Home Assistant custom integration that autonomously trades cryptocurrencies
on Binance (spot only) using AI-driven market intelligence. Runs 24/7 on the
user's HA instance with full dashboard visibility and strict risk management.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Home Assistant                     │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  Dashboard    │  │  Integration │  │  Sensors   │ │
│  │  (Lovelace)  │◄─┤  (Core)      │──┤  & Events  │ │
│  └──────────────┘  └──────┬───────┘  └────────────┘ │
│                           │                          │
└───────────────────────────┼──────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
        ┌─────▼─────┐ ┌────▼────┐ ┌──────▼──────┐
        │  Binance   │ │ Claude  │ │  News/Data  │
        │  API       │ │ API     │ │  Sources    │
        │  (Spot)    │ │ (Haiku) │ │  (RSS/Web)  │
        └───────────┘ └─────────┘ └─────────────┘
```

## Core Components

### 1. Intelligence Engine (`intelligence/`)

Gathers and weighs market intelligence from multiple sources:

| Source Category | Examples | Min Weight | Max Weight |
|----------------|----------|------------|------------|
| General News | Reuters, Bloomberg RSS, CoinDesk | 10% | 40% |
| Trend Analysis | Technical indicators, volume, momentum | 10% | 40% |
| Crypto Recommendations | Analyst ratings, social sentiment | 10% | 40% |
| Historical Learning | Own trade history, pattern recognition | 10% | 40% |

**Constraint:** No source may have 0% or 100% weight. All sources must
contribute to every decision. Weight distribution is visible in the dashboard.

The Claude API (Haiku model — strictly enforced, no model upgrades without
explicit user approval) serves as the **decision engine**. It does not merely
analyze — it makes the final buy/sell/hold decision based on all weighted inputs.

Data from all sources is cached locally to minimize API calls. Claude receives
the cached data bundle, evaluates it holistically, and returns a structured
trading decision with confidence score and reasoning.

Each decision call is logged with source weights, Claude's reasoning, and the
resulting action.

### 2. Trading Engine (`trading/`)

Executes spot trades on Binance with the following constraints:

- **Spot only** — no margin, no futures, no leverage
- **Spread trades throughout the day** — no burst trading
- **Randomized timing** — trades happen at non-predictable intervals
  (e.g., 15min to 4h jitter) to avoid pattern detection and bans
- **Respect Binance rate limits** — operate at <50% of allowed rate
- **Minimum order sizes** — respect Binance LOT_SIZE and MIN_NOTIONAL filters
- **Pair whitelist** — configurable list of allowed trading pairs

### 3. Risk Management (`risk/`)

The most critical component. Protects capital at all costs.

#### Stop-Loss Cascade

| Level | Trigger | Action |
|-------|---------|--------|
| L1 — Trade Stop | Single trade -3% | Close position immediately |
| L2 — Session Pause | Portfolio -5% in 1h | Pause all trading for 2h |
| L3 — Daily Halt | Portfolio -8% in 24h | Stop trading until next day |
| L4 — Emergency Stop | Portfolio -15% total | **Full stop.** Manual restart required |

#### Savings Rate (Sparquote)

- Configurable 0-20% of realized profit is moved to savings
- Savings are **never touched** by the trading engine
- Savings accumulate until manual withdrawal (displayed in EUR)
- Savings are held as USDT/USDC (stablecoin) on Binance

#### Claude API Usage Budget

- Configurable limit (default: 5h equivalent / ~$10/month on Haiku)
- Risk management and stop-loss checks get **priority budget allocation**
- If budget is exhausted: trading pauses, only monitoring continues
- Emergency stop-loss checks bypass the budget limit entirely

### 4. Dashboard (`frontend/`)

Home Assistant Lovelace dashboard with:

#### Panel A — Configuration
- Binance API Key + Secret (stored in HA secrets)
- Claude API Key (stored in HA secrets)
- Claude usage limit (hours/month)
- Trading pair whitelist
- Risk parameters (stop-loss levels, savings rate)

##### Binance API Key Setup Guide (displayed in dashboard)
Step-by-step instructions directly in the config panel:
1. Log in to binance.com → API Management
2. Create new API key with label "BController"
3. Enable "Spot Trading" permission ONLY
4. Disable "Enable Withdrawals" (critical!)
5. Restrict to HA server IP address (recommended)
6. Copy API Key and Secret into the fields below

##### Tax Compliance Panel
- Current tax year summary (realized gains, losses, fees)
- Download button: "Steuerexport herunterladen" → CSV file containing:
  - All trades with: Datum, Coin, Kauf/Verkauf, Menge, Kurs (EUR),
    Gebühren (EUR), Gewinn/Verlust (EUR)
  - Format compatible with Anlage SO (Zeile 41-46)
  - Includes FIFO calculation for Anschaffungskosten
  - Separate summary sheet: Gesamtgewinn, Gesamtverlust, Saldo
  - Freigrenze-Hinweis: if total gains < 1000 EUR (since 2024: Freigrenze
    für private Veräußerungsgeschäfte), display notice
- Instructions text: "Diese Datei enthält alle Daten für die Anlage SO
  deiner Einkommensteuererklärung. Übergib sie deinem Steuerberater oder
  trage die Summen in ELSTER unter 'Sonstige Einkünfte → Private
  Veräußerungsgeschäfte' ein."

#### Panel B — Portfolio Overview
- Current total value (EUR)
- Savings balance (EUR)
- Active positions
- Performance: 1h / 24h / 7d / 30d / all-time
- P&L chart

#### Panel C — Decision Log
- Timeline of all trade decisions
- For each decision: source weights visualization
- Which sources contributed what signal
- Confidence score
- Outcome tracking (was the decision profitable?)

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Integration | Python 3.12+ (HA custom component) |
| Binance API | `python-binance` or raw REST/WebSocket |
| Claude API | `anthropic` Python SDK |
| News Data | RSS feeds, free crypto APIs |
| Dashboard | Lovelace YAML + custom cards (JS) |
| Data Storage | HA recorder (SQLite/PostgreSQL) |

## Data Flow

```
Every 15min-4h (randomized):
  1. Intelligence Engine gathers + caches fresh data from all sources
  2. Risk Manager pre-check: is trading allowed? (stop-loss, budget, cooldown)
  3. If allowed: Claude Haiku receives cached data bundle
  4. Claude makes the FINAL trade decision (buy/sell/hold + pair + amount)
  5. Trading Engine validates decision against Binance rules and executes
  6. All decisions logged with full source attribution + Claude reasoning
  7. Dashboard sensors updated
  8. If profit realized: savings rate applied to savings balance
```
