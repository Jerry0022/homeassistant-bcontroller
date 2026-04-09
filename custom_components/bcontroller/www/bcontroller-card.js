/**
 * BController Trading Dashboard — Custom Lovelace Card
 *
 * Panels:
 *   A — Status & Config Overview
 *   B — Portfolio Performance
 *   C — Decision Log
 *
 * Single vanilla-JS file, no build step, no external dependencies.
 * Requires Home Assistant with sensor entities provided by the BController integration.
 */

// ─── Card registration ─────────────────────────────────────────────────────────
window.customCards = window.customCards || [];
window.customCards.push({
  type: "bcontroller-card",
  name: "BController Trading Dashboard",
  description:
    "Autonomous crypto trading dashboard with status, portfolio, and decision log",
});

// ─── Utility helpers ───────────────────────────────────────────────────────────
function fmt(value, decimals = 2) {
  if (value === null || value === undefined || value === "unavailable" || value === "unknown") {
    return "—";
  }
  const num = parseFloat(value);
  if (isNaN(num)) return String(value);
  return num.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtPct(value) {
  if (value === null || value === undefined) return "—";
  const num = parseFloat(value);
  if (isNaN(num)) return "—";
  const sign = num >= 0 ? "+" : "";
  return `${sign}${num.toFixed(2)}%`;
}

function fmtTime(isoString) {
  if (!isoString) return "—";
  try {
    const d = new Date(isoString);
    return d.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return isoString;
  }
}

function getState(hass, entityId) {
  if (!hass || !hass.states) return null;
  return hass.states[entityId] || null;
}

function getStateValue(hass, entityId) {
  const s = getState(hass, entityId);
  return s ? s.state : null;
}

function getAttr(hass, entityId, attr) {
  const s = getState(hass, entityId);
  return s && s.attributes ? s.attributes[attr] : null;
}

// ─── Sparkline renderer (pure SVG, no deps) ────────────────────────────────────
function renderSparkline(snapshots, width = 280, height = 56) {
  if (!snapshots || snapshots.length < 2) {
    return `<svg width="${width}" height="${height}" class="sparkline-empty">
      <text x="${width / 2}" y="${height / 2}" text-anchor="middle" fill="var(--secondary-text-color)" font-size="12">No data</text>
    </svg>`;
  }

  const values = snapshots.map((s) =>
    typeof s === "object" ? s.total_value_usdt || 0 : parseFloat(s) || 0
  );
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pad = 4;

  const pts = values.map((v, i) => {
    const x = pad + ((i / (values.length - 1)) * (width - pad * 2));
    const y = pad + ((1 - (v - min) / range) * (height - pad * 2));
    return `${x},${y}`;
  });

  const polyline = pts.join(" ");
  // gradient fill area
  const areaFirst = pts[0];
  const areaLast = pts[pts.length - 1];
  const area = `${pts.join(" ")} ${areaLast.split(",")[0]},${height - pad} ${areaFirst.split(",")[0]},${height - pad}`;

  const lastVal = values[values.length - 1];
  const firstVal = values[0];
  const lineColor = lastVal >= firstVal ? "#3fb950" : "#f85149";

  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" class="sparkline">
    <defs>
      <linearGradient id="spark-grad-${Math.random().toString(36).slice(2)}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.3"/>
        <stop offset="100%" stop-color="${lineColor}" stop-opacity="0.02"/>
      </linearGradient>
    </defs>
    <polygon points="${area}" fill="${lineColor}" fill-opacity="0.15"/>
    <polyline points="${polyline}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

// ─── Source weights stacked bar ────────────────────────────────────────────────
function renderSourceWeightsBar(weights) {
  if (!weights || typeof weights !== "object") {
    return `<div class="weights-empty">No weight data</div>`;
  }

  const colorMap = {
    news: "#58a6ff",
    trend: "#3fb950",
    trends: "#3fb950",
    sentiment: "#a855f7",
    history: "#f97316",
    historical: "#f97316",
  };

  const entries = Object.entries(weights);
  if (entries.length === 0) return `<div class="weights-empty">No weight data</div>`;

  const total = entries.reduce((s, [, v]) => s + parseFloat(v || 0), 0) || 1;
  const segments = entries
    .map(([key, val]) => {
      const pct = ((parseFloat(val || 0) / total) * 100).toFixed(1);
      const color = colorMap[key.toLowerCase()] || "#888";
      return `<div class="weight-segment" style="width:${pct}%;background:${color}" title="${key}: ${pct}%">
        <span class="weight-label">${key.charAt(0).toUpperCase() + key.slice(1)}</span>
      </div>`;
    })
    .join("");

  const legend = entries
    .map(([key, val]) => {
      const pct = ((parseFloat(val || 0) / total) * 100).toFixed(0);
      const color = colorMap[key.toLowerCase()] || "#888";
      return `<span class="weight-legend-item"><span class="weight-dot" style="background:${color}"></span>${key} ${pct}%</span>`;
    })
    .join("");

  return `<div class="weights-bar">${segments}</div>
  <div class="weights-legend">${legend}</div>`;
}

// ─── Confidence bar ────────────────────────────────────────────────────────────
function renderConfidenceBar(confidence) {
  const pct = Math.round((parseFloat(confidence) || 0) * 100);
  const color = pct >= 80 ? "#3fb950" : pct >= 60 ? "#d29922" : "#f85149";
  return `<div class="confidence-bar-wrap" title="Confidence: ${pct}%">
    <div class="confidence-bar" style="width:${pct}%;background:${color}"></div>
    <span class="confidence-label">${pct}%</span>
  </div>`;
}

// ─── Panel A — Status & Config Overview ────────────────────────────────────────
function renderPanelA(hass) {
  const status = getStateValue(hass, "sensor.bcontroller_system_status") || "unknown";
  const btcPrice = getStateValue(hass, "sensor.bcontroller_btc_price");
  const change24h = getAttr(hass, "sensor.bcontroller_btc_price", "change_24h_pct");
  const high24h = getAttr(hass, "sensor.bcontroller_btc_price", "high_24h");
  const low24h = getAttr(hass, "sensor.bcontroller_btc_price", "low_24h");
  const portfolioValue = getStateValue(hass, "sensor.bcontroller_portfolio_value");
  const savingsBalance = getStateValue(hass, "sensor.bcontroller_savings_balance");
  const binanceWeight = getAttr(hass, "sensor.bcontroller_portfolio_value", "binance_weight_1m");

  // Claude budget: read from attributes if available, fallback placeholder
  const claudeBudgetUsed = getAttr(hass, "sensor.bcontroller_system_status", "claude_budget_used_usd");
  const claudeBudgetTotal = getAttr(hass, "sensor.bcontroller_system_status", "claude_budget_total_usd") || 10;

  const statusColors = {
    active: "#3fb950",
    paused: "#d29922",
    halted: "#f85149",
    stopped: "#f85149",
    monitoring: "#58a6ff",
    error: "#f85149",
    unknown: "#888",
  };
  const statusColor = statusColors[status] || "#888";

  const changeNum = parseFloat(change24h);
  const changeColor = isNaN(changeNum) ? "var(--secondary-text-color)" : changeNum >= 0 ? "#3fb950" : "#f85149";

  const binanceWeightNum = parseFloat(binanceWeight) || 0;
  const weightPct = Math.min((binanceWeightNum / 3000) * 100, 100).toFixed(1);
  const weightColor = binanceWeightNum > 2400 ? "#f85149" : binanceWeightNum > 1800 ? "#d29922" : "#3fb950";

  const budgetUsedNum = parseFloat(claudeBudgetUsed) || 0;
  const budgetPct = Math.min((budgetUsedNum / parseFloat(claudeBudgetTotal)) * 100, 100).toFixed(1);
  const budgetColor = budgetPct > 90 ? "#f85149" : budgetPct > 70 ? "#d29922" : "#3fb950";

  // Emergency stop banner
  const emergencyBanner =
    status === "stopped"
      ? `<div class="emergency-banner">
          <div class="emergency-icon">&#9888;</div>
          <div class="emergency-text">
            <strong>EMERGENCY STOP (L4)</strong>
            <span>Maximum drawdown threshold exceeded. All trading halted.</span>
          </div>
          <button class="restart-btn" data-action="reset-l4">
            Reset &amp; Restart
          </button>
        </div>`
      : "";

  // Binance setup guide (shown when status is "error" or first setup likely)
  const setupGuide =
    status === "error"
      ? `<div class="setup-guide">
          <div class="setup-guide-title">Binance API Setup Guide</div>
          <ol class="setup-steps">
            <li>Go to <strong>binance.com</strong> &rarr; Profile &rarr; API Management</li>
            <li>Create a new API key named <strong>BController</strong></li>
            <li>Enable <strong>ONLY</strong> "Spot &amp; Margin Trading"</li>
            <li class="critical-step"><strong>CRITICAL:</strong> Disable "Enable Withdrawals" &#8212; this is mandatory</li>
            <li>Set IP whitelist to your Home Assistant server IP address</li>
            <li>Copy API Key and Secret into the HA integration configuration</li>
          </ol>
        </div>`
      : "";

  return `
    ${emergencyBanner}
    <div class="section-title">System Status</div>
    <div class="status-row">
      <span class="status-dot" style="background:${statusColor}"></span>
      <span class="status-label">${status.charAt(0).toUpperCase() + status.slice(1)}</span>
    </div>

    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-label">BTC Price</div>
        <div class="metric-value">${fmt(btcPrice)} <span class="metric-unit">USDT</span></div>
        <div class="metric-sub" style="color:${changeColor}">${fmtPct(change24h)} today</div>
        <div class="metric-range">H: ${fmt(high24h)} &nbsp; L: ${fmt(low24h)}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Portfolio Value</div>
        <div class="metric-value">${fmt(portfolioValue)} <span class="metric-unit">USDT</span></div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Savings Balance</div>
        <div class="metric-value">${fmt(savingsBalance)} <span class="metric-unit">USDT</span></div>
        <div class="metric-sub savings-note">Locked &#x1F512;</div>
      </div>
    </div>

    <div class="section-title">Resource Usage</div>
    <div class="usage-row">
      <span class="usage-label">Claude API Budget</span>
      <span class="usage-value">${claudeBudgetUsed !== null ? `$${fmt(claudeBudgetUsed, 3)}` : "—"} / $${fmt(claudeBudgetTotal, 2)}</span>
    </div>
    <div class="progress-bar-wrap">
      <div class="progress-bar" style="width:${claudeBudgetUsed !== null ? budgetPct : 0}%;background:${budgetColor};transition:width 0.6s ease"></div>
    </div>

    <div class="usage-row" style="margin-top:10px">
      <span class="usage-label">Binance Weight (1m)</span>
      <span class="usage-value">${binanceWeightNum} / 3000</span>
    </div>
    <div class="progress-bar-wrap">
      <div class="progress-bar" style="width:${weightPct}%;background:${weightColor};transition:width 0.6s ease"></div>
    </div>

    ${setupGuide}
  `;
}

// ─── Panel B — Portfolio Performance ──────────────────────────────────────────
function renderPanelB(hass) {
  const portfolioValue = getStateValue(hass, "sensor.bcontroller_portfolio_value");
  const savingsBalance = getStateValue(hass, "sensor.bcontroller_savings_balance");
  const openPositions = getAttr(hass, "sensor.bcontroller_portfolio_value", "open_positions");
  const snapshots = getAttr(hass, "sensor.bcontroller_portfolio_value", "portfolio_snapshots");
  const perf24h = getAttr(hass, "sensor.bcontroller_portfolio_value", "perf_24h_pct");
  const perf7d = getAttr(hass, "sensor.bcontroller_portfolio_value", "perf_7d_pct");

  const perf24hNum = parseFloat(perf24h);
  const perf7dNum = parseFloat(perf7d);
  const perf24hColor = isNaN(perf24hNum) ? "#888" : perf24hNum >= 0 ? "#3fb950" : "#f85149";
  const perf7dColor = isNaN(perf7dNum) ? "#888" : perf7dNum >= 0 ? "#3fb950" : "#f85149";

  // Open positions list — from attribute if available
  const positions = getAttr(hass, "sensor.bcontroller_portfolio_value", "positions") || [];
  const positionRows = Array.isArray(positions) && positions.length > 0
    ? positions
        .map((p) => {
          const currentPrice = getStateValue(hass, "sensor.bcontroller_btc_price");
          const entryPrice = p.vwap_entry_price || p.entry_price;
          const qty = p.quantity;
          let pnlPct = p.pnl_pct;
          if (pnlPct === undefined && entryPrice && currentPrice) {
            pnlPct = ((parseFloat(currentPrice) - parseFloat(entryPrice)) / parseFloat(entryPrice)) * 100;
          }
          const pnlColor = pnlPct >= 0 ? "#3fb950" : "#f85149";
          return `<tr class="position-row">
            <td class="pos-pair">${p.pair || "—"}</td>
            <td class="pos-price">${fmt(entryPrice)}</td>
            <td class="pos-price">${fmt(p.current_price || currentPrice)}</td>
            <td class="pos-pnl" style="color:${pnlColor}">${fmtPct(pnlPct)}</td>
            <td class="pos-qty">${qty !== undefined ? parseFloat(qty).toFixed(6) : "—"}</td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="5" class="no-data">No open positions</td></tr>`;

  return `
    <div class="portfolio-hero">
      <div class="hero-label">Total Portfolio Value</div>
      <div class="hero-value">${fmt(portfolioValue)} <span class="hero-unit">USDT</span></div>
      <div class="perf-badges">
        ${perf24h !== null ? `<span class="perf-badge" style="color:${perf24hColor};border-color:${perf24hColor}">24h ${fmtPct(perf24h)}</span>` : ""}
        ${perf7d !== null ? `<span class="perf-badge" style="color:${perf7dColor};border-color:${perf7dColor}">7d ${fmtPct(perf7d)}</span>` : ""}
      </div>
    </div>

    <div class="savings-block">
      <span class="savings-block-label">Savings (Locked)</span>
      <span class="savings-block-value">${fmt(savingsBalance)} USDT &#x1F512;</span>
    </div>

    <div class="sparkline-wrap">
      <div class="section-title">Portfolio History</div>
      ${renderSparkline(snapshots)}
    </div>

    <div class="section-title">Open Positions <span class="count-badge">${Array.isArray(positions) ? positions.length : openPositions || 0}</span></div>
    <table class="positions-table">
      <thead>
        <tr>
          <th>Pair</th>
          <th>Entry (USDT)</th>
          <th>Current (USDT)</th>
          <th>P&amp;L</th>
          <th>Qty</th>
        </tr>
      </thead>
      <tbody>${positionRows}</tbody>
    </table>
  `;
}

// ─── Panel C — Decision Log ────────────────────────────────────────────────────
function renderPanelC(hass, expandedIndex) {
  const decisionLog = getAttr(hass, "sensor.bcontroller_last_decision", "decision_log") || [];
  const lastDecision = getStateValue(hass, "sensor.bcontroller_last_decision");

  // Build a synthetic single-entry log from the latest state if no full log available
  const logEntries = Array.isArray(decisionLog) && decisionLog.length > 0
    ? decisionLog.slice(-20).reverse()
    : [];

  // Also show the last decision from the sensor state if log is empty
  const latestAction = getAttr(hass, "sensor.bcontroller_last_decision", "action");
  const latestPair = getAttr(hass, "sensor.bcontroller_last_decision", "pair");
  const latestConfidence = getAttr(hass, "sensor.bcontroller_last_decision", "confidence");
  const latestReasonCode = getAttr(hass, "sensor.bcontroller_last_decision", "reason_code");
  const latestReasoning = getAttr(hass, "sensor.bcontroller_last_decision", "reasoning");
  const latestSourceWeights = getAttr(hass, "sensor.bcontroller_last_decision", "source_weights");
  const latestTimestamp = getAttr(hass, "sensor.bcontroller_last_decision", "timestamp");

  const hasLatest = latestAction && latestPair;

  const actionColors = {
    BUY: "#3fb950",
    SELL: "#f85149",
    HOLD: "#d29922",
  };

  const renderEntry = (entry, index) => {
    const action = (entry.action || entry.decision?.action || "HOLD").toUpperCase();
    const pair = entry.pair || entry.decision?.pair || "—";
    const confidence = entry.confidence || entry.decision?.confidence || 0;
    const reasonCode = entry.reason_code || entry.decision?.reason_code || "—";
    const reasoning = entry.reasoning || entry.decision?.reasoning || "";
    const sourceWeights = entry.source_weights || {};
    const timestamp = entry.timestamp || entry.decision?.timestamp || "";
    const actionColor = actionColors[action] || "#888";
    const isExpanded = expandedIndex === index;

    return `<div class="decision-entry ${isExpanded ? "expanded" : ""}" data-index="${index}">
      <div class="decision-header">
        <div class="decision-timestamp">${fmtTime(timestamp)}</div>
        <div class="decision-action-badge" style="color:${actionColor};border-color:${actionColor}">${action}</div>
        <div class="decision-pair">${pair}</div>
        <div class="decision-confidence">${renderConfidenceBar(confidence)}</div>
        <div class="decision-reason-code">${reasonCode}</div>
        <div class="decision-chevron">${isExpanded ? "&#x25B2;" : "&#x25BC;"}</div>
      </div>
      ${isExpanded ? `<div class="decision-detail">
        <div class="detail-section">
          <div class="detail-label">Reasoning</div>
          <div class="detail-text">${reasoning || "No reasoning recorded."}</div>
        </div>
        <div class="detail-section">
          <div class="detail-label">Source Weights</div>
          ${renderSourceWeightsBar(sourceWeights)}
        </div>
      </div>` : ""}
    </div>`;
  };

  const entries =
    logEntries.length > 0
      ? logEntries.map(renderEntry).join("")
      : hasLatest
      ? renderEntry(
          {
            action: latestAction,
            pair: latestPair,
            confidence: latestConfidence,
            reason_code: latestReasonCode,
            reasoning: latestReasoning,
            source_weights: latestSourceWeights,
            timestamp: latestTimestamp,
          },
          0
        )
      : `<div class="no-data">No decisions recorded yet.</div>`;

  return `
    <div class="section-title">
      Decision Log
      <span class="count-badge">${logEntries.length || (hasLatest ? 1 : 0)}</span>
    </div>
    <div class="decision-timeline">${entries}</div>
  `;
}

// ─── Regulatory footer ─────────────────────────────────────────────────────────
function renderFooter() {
  return `<div class="regulatory-footer">
    <div class="regulatory-icon">&#x2139;</div>
    <div class="regulatory-text">
      <p>Binance closed its BaFin-regulated partner (Binance Germany GmbH) in 2023.
         Access for German users may be restricted.</p>
      <p>Savings remain on Binance — not in your own custody.</p>
    </div>
  </div>`;
}

// ─── Full card CSS ─────────────────────────────────────────────────────────────
const CARD_STYLES = `
  :host {
    display: block;
    font-family: var(--primary-font-family, 'Roboto', sans-serif);
    color: var(--primary-text-color, #e6edf3);
  }

  .card-root {
    background: var(--ha-card-background, #0d1117);
    border-radius: var(--ha-card-border-radius, 12px);
    border: 1px solid var(--divider-color, #30363d);
    overflow: hidden;
  }

  /* ── Header ─── */
  .card-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 16px 10px;
    border-bottom: 1px solid var(--divider-color, #30363d);
    background: var(--secondary-background-color, #161b22);
  }
  .card-header-icon {
    font-size: 20px;
  }
  .card-header-title {
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.5px;
    flex: 1;
  }
  .card-header-status {
    font-size: 12px;
    color: var(--secondary-text-color, #8b949e);
  }

  /* ── Tab bar ─── */
  .tab-bar {
    display: flex;
    background: var(--secondary-background-color, #161b22);
    border-bottom: 1px solid var(--divider-color, #30363d);
  }
  .tab-btn {
    flex: 1;
    padding: 10px 0;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--secondary-text-color, #8b949e);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: color 0.2s, border-color 0.2s;
    font-family: inherit;
  }
  .tab-btn:hover {
    color: var(--primary-text-color, #e6edf3);
  }
  .tab-btn.active {
    color: var(--primary-color, #58a6ff);
    border-bottom-color: var(--primary-color, #58a6ff);
  }

  /* ── Panel content ─── */
  .panel-content {
    padding: 16px;
    min-height: 200px;
    animation: fadeIn 0.2s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  /* ── Section titles ─── */
  .section-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--secondary-text-color, #8b949e);
    margin: 14px 0 8px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .section-title:first-child {
    margin-top: 0;
  }

  /* ── Count badge ─── */
  .count-badge {
    background: var(--divider-color, #30363d);
    color: var(--primary-text-color, #e6edf3);
    border-radius: 10px;
    padding: 1px 7px;
    font-size: 11px;
    font-weight: 700;
    text-transform: none;
    letter-spacing: 0;
  }

  /* ── Emergency banner ─── */
  .emergency-banner {
    display: flex;
    align-items: center;
    gap: 12px;
    background: rgba(248, 81, 73, 0.15);
    border: 1px solid #f85149;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 16px;
  }
  .emergency-icon {
    font-size: 24px;
    color: #f85149;
    flex-shrink: 0;
  }
  .emergency-text {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .emergency-text strong {
    font-size: 14px;
    color: #f85149;
    font-weight: 700;
  }
  .emergency-text span {
    font-size: 12px;
    color: var(--secondary-text-color, #8b949e);
  }
  .restart-btn {
    background: #f85149;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
    white-space: nowrap;
    font-family: inherit;
  }
  .restart-btn:hover {
    background: #ff6b63;
  }

  /* ── Status indicator ─── */
  .status-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
  }
  .status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    box-shadow: 0 0 6px currentColor;
  }
  .status-label {
    font-size: 14px;
    font-weight: 500;
  }

  /* ── Metrics grid ─── */
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px;
    margin-bottom: 4px;
  }
  .metric-card {
    background: var(--secondary-background-color, #161b22);
    border: 1px solid var(--divider-color, #30363d);
    border-radius: 8px;
    padding: 10px 12px;
  }
  .metric-label {
    font-size: 11px;
    color: var(--secondary-text-color, #8b949e);
    margin-bottom: 4px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .metric-value {
    font-size: 18px;
    font-weight: 700;
    color: var(--primary-text-color, #e6edf3);
    line-height: 1.2;
  }
  .metric-unit {
    font-size: 11px;
    font-weight: 400;
    color: var(--secondary-text-color, #8b949e);
  }
  .metric-sub {
    font-size: 12px;
    margin-top: 3px;
    font-weight: 500;
  }
  .metric-range {
    font-size: 11px;
    color: var(--secondary-text-color, #8b949e);
    margin-top: 2px;
  }
  .savings-note {
    color: #58a6ff !important;
    font-size: 11px;
  }

  /* ── Progress / usage bars ─── */
  .usage-row {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: var(--secondary-text-color, #8b949e);
    margin-bottom: 4px;
  }
  .usage-label { font-weight: 500; }
  .usage-value { font-variant-numeric: tabular-nums; }
  .progress-bar-wrap {
    height: 6px;
    background: var(--divider-color, #30363d);
    border-radius: 3px;
    overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    border-radius: 3px;
    min-width: 2px;
  }

  /* ── Binance setup guide ─── */
  .setup-guide {
    margin-top: 16px;
    background: rgba(88, 166, 255, 0.08);
    border: 1px solid rgba(88, 166, 255, 0.3);
    border-radius: 8px;
    padding: 12px 14px;
  }
  .setup-guide-title {
    font-size: 13px;
    font-weight: 700;
    color: #58a6ff;
    margin-bottom: 10px;
  }
  .setup-steps {
    margin: 0;
    padding-left: 20px;
    color: var(--secondary-text-color, #8b949e);
    font-size: 13px;
    line-height: 1.8;
  }
  .setup-steps li { margin-bottom: 2px; }
  .critical-step {
    color: #f85149 !important;
    font-weight: 600;
  }

  /* ── Portfolio hero ─── */
  .portfolio-hero {
    text-align: center;
    padding: 14px 0 16px;
    border-bottom: 1px solid var(--divider-color, #30363d);
    margin-bottom: 14px;
  }
  .hero-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--secondary-text-color, #8b949e);
    margin-bottom: 4px;
  }
  .hero-value {
    font-size: 32px;
    font-weight: 800;
    color: var(--primary-text-color, #e6edf3);
    line-height: 1.1;
  }
  .hero-unit {
    font-size: 16px;
    font-weight: 400;
    color: var(--secondary-text-color, #8b949e);
  }
  .perf-badges {
    display: flex;
    justify-content: center;
    gap: 8px;
    margin-top: 8px;
    flex-wrap: wrap;
  }
  .perf-badge {
    border: 1px solid;
    border-radius: 20px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
  }

  /* ── Savings block ─── */
  .savings-block {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: rgba(88, 166, 255, 0.06);
    border: 1px solid rgba(88, 166, 255, 0.2);
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 14px;
    font-size: 13px;
  }
  .savings-block-label {
    color: var(--secondary-text-color, #8b949e);
    font-weight: 500;
  }
  .savings-block-value {
    color: #58a6ff;
    font-weight: 700;
  }

  /* ── Sparkline ─── */
  .sparkline-wrap {
    margin-bottom: 14px;
  }
  .sparkline, .sparkline-empty {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }

  /* ── Positions table ─── */
  .positions-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .positions-table th {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid var(--divider-color, #30363d);
    color: var(--secondary-text-color, #8b949e);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .position-row td {
    padding: 8px 8px;
    border-bottom: 1px solid rgba(48, 54, 61, 0.5);
    vertical-align: middle;
  }
  .pos-pair { font-weight: 700; }
  .pos-price { font-variant-numeric: tabular-nums; color: var(--secondary-text-color, #8b949e); }
  .pos-pnl { font-weight: 600; font-variant-numeric: tabular-nums; }
  .pos-qty { font-size: 11px; color: var(--secondary-text-color, #8b949e); }
  .no-data {
    text-align: center;
    padding: 20px;
    color: var(--secondary-text-color, #8b949e);
    font-size: 13px;
  }

  /* ── Decision timeline ─── */
  .decision-timeline {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .decision-entry {
    background: var(--secondary-background-color, #161b22);
    border: 1px solid var(--divider-color, #30363d);
    border-radius: 8px;
    overflow: hidden;
    transition: border-color 0.2s;
  }
  .decision-entry.expanded {
    border-color: var(--primary-color, #58a6ff);
  }
  .decision-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    cursor: pointer;
    flex-wrap: wrap;
  }
  .decision-header:hover {
    background: rgba(88, 166, 255, 0.05);
  }
  .decision-timestamp {
    font-size: 11px;
    color: var(--secondary-text-color, #8b949e);
    min-width: 90px;
    flex-shrink: 0;
  }
  .decision-action-badge {
    border: 1px solid;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 700;
    flex-shrink: 0;
  }
  .decision-pair {
    font-size: 13px;
    font-weight: 600;
    flex-shrink: 0;
    min-width: 70px;
  }
  .decision-confidence {
    flex: 1;
    min-width: 80px;
  }
  .decision-reason-code {
    font-size: 11px;
    color: var(--secondary-text-color, #8b949e);
    background: var(--divider-color, #30363d);
    border-radius: 4px;
    padding: 2px 6px;
    flex-shrink: 0;
  }
  .decision-chevron {
    font-size: 10px;
    color: var(--secondary-text-color, #8b949e);
    flex-shrink: 0;
  }

  /* ── Confidence bar ─── */
  .confidence-bar-wrap {
    height: 6px;
    background: var(--divider-color, #30363d);
    border-radius: 3px;
    overflow: visible;
    position: relative;
    display: flex;
    align-items: center;
  }
  .confidence-bar {
    height: 6px;
    border-radius: 3px;
    transition: width 0.6s ease;
    min-width: 2px;
  }
  .confidence-label {
    font-size: 10px;
    color: var(--secondary-text-color, #8b949e);
    margin-left: 6px;
    position: absolute;
    right: -26px;
    white-space: nowrap;
  }

  /* ── Decision detail ─── */
  .decision-detail {
    padding: 10px 12px 12px;
    border-top: 1px solid var(--divider-color, #30363d);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .detail-section { display: flex; flex-direction: column; gap: 6px; }
  .detail-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--secondary-text-color, #8b949e);
  }
  .detail-text {
    font-size: 13px;
    line-height: 1.6;
    color: var(--primary-text-color, #e6edf3);
    background: rgba(0,0,0,0.2);
    border-radius: 6px;
    padding: 8px 10px;
  }

  /* ── Source weights bar ─── */
  .weights-bar {
    display: flex;
    height: 20px;
    border-radius: 4px;
    overflow: hidden;
    gap: 1px;
  }
  .weight-segment {
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 24px;
    overflow: hidden;
    transition: flex 0.4s ease;
  }
  .weight-label {
    font-size: 10px;
    color: rgba(255,255,255,0.85);
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: clip;
  }
  .weights-legend {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 6px;
  }
  .weight-legend-item {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--secondary-text-color, #8b949e);
  }
  .weight-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .weights-empty {
    font-size: 12px;
    color: var(--secondary-text-color, #8b949e);
  }

  /* ── Regulatory footer ─── */
  .regulatory-footer {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    background: rgba(248, 81, 73, 0.05);
    border-top: 1px solid var(--divider-color, #30363d);
    padding: 10px 16px;
  }
  .regulatory-icon {
    font-size: 16px;
    color: #d29922;
    flex-shrink: 0;
    margin-top: 1px;
  }
  .regulatory-text {
    font-size: 11px;
    color: var(--secondary-text-color, #8b949e);
    line-height: 1.5;
  }
  .regulatory-text p {
    margin: 0 0 4px;
  }
  .regulatory-text p:last-child { margin-bottom: 0; }

  /* ── Responsive adjustments ─── */
  @media (max-width: 480px) {
    .metrics-grid {
      grid-template-columns: 1fr 1fr;
    }
    .hero-value { font-size: 24px; }
    .decision-header { gap: 6px; }
    .decision-timestamp { min-width: 70px; font-size: 10px; }
    .positions-table th,
    .position-row td { padding: 5px 4px; font-size: 11px; }
  }
`;

// ─── Custom Element Definition ─────────────────────────────────────────────────
class BControllerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._activeTab = 0;
    this._expandedDecision = null;
    this._initialized = false;
  }

  // HA card API
  setConfig(config) {
    this._config = config || {};
  }

  static getConfigElement() {
    // No visual editor for now — config via YAML only
    return null;
  }

  getCardSize() {
    return 8;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  get hass() {
    return this._hass;
  }

  // ─── Internal methods ──────────────────────────────────────────────────────
  _switchTab(index) {
    this._activeTab = index;
    this._expandedDecision = null;
    this._render();
  }

  _toggleDecision(index) {
    this._expandedDecision = this._expandedDecision === index ? null : index;
    this._render();
  }

  _resetL4() {
    if (!this._hass) return;
    this._hass.callService("bcontroller", "reset_l4", {}).catch((err) => {
      console.error("[BControllerCard] reset_l4 service call failed:", err);
    });
  }

  _buildTabContent() {
    const hass = this._hass;
    if (!hass) {
      return `<div class="panel-content no-data">Connecting to Home Assistant…</div>`;
    }
    switch (this._activeTab) {
      case 0:
        return `<div class="panel-content">${renderPanelA(hass)}</div>`;
      case 1:
        return `<div class="panel-content">${renderPanelB(hass)}</div>`;
      case 2:
        return `<div class="panel-content">${renderPanelC(hass, this._expandedDecision)}</div>`;
      default:
        return `<div class="panel-content no-data">Unknown tab.</div>`;
    }
  }

  _render() {
    const status = this._hass
      ? getStateValue(this._hass, "sensor.bcontroller_system_status") || "unknown"
      : "unknown";

    const statusDotColors = {
      active: "#3fb950",
      paused: "#d29922",
      halted: "#f85149",
      stopped: "#f85149",
      monitoring: "#58a6ff",
      error: "#f85149",
      unknown: "#888",
    };
    const dotColor = statusDotColors[status] || "#888";

    const tabs = [
      { label: "Status" },
      { label: "Portfolio" },
      { label: "Decisions" },
    ];

    const tabButtons = tabs
      .map(
        (t, i) =>
          `<button class="tab-btn ${i === this._activeTab ? "active" : ""}" data-tab="${i}">${t.label}</button>`
      )
      .join("");

    const html = `
      <style>${CARD_STYLES}</style>
      <div class="card-root">
        <div class="card-header">
          <span class="card-header-icon">&#x20BF;</span>
          <span class="card-header-title">BController</span>
          <span class="card-header-status">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor};margin-right:4px;vertical-align:middle"></span>
            ${status}
          </span>
        </div>
        <div class="tab-bar">${tabButtons}</div>
        ${this._buildTabContent()}
        ${renderFooter()}
      </div>
    `;

    this.shadowRoot.innerHTML = html;
    this._bindEvents();
  }

  _bindEvents() {
    // Tab buttons
    this.shadowRoot.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._switchTab(parseInt(btn.dataset.tab, 10));
      });
    });

    // Decision headers — bound via onclick data attributes on the header divs
    this.shadowRoot.querySelectorAll(".decision-header").forEach((header) => {
      header.addEventListener("click", () => {
        const index = parseInt(header.closest(".decision-entry").dataset.index, 10);
        this._toggleDecision(index);
      });
    });

    // Restart L4 button
    const restartBtn = this.shadowRoot.querySelector("[data-action='reset-l4']");
    if (restartBtn) {
      restartBtn.addEventListener("click", () => this._resetL4());
    }
  }

  connectedCallback() {
    if (!this._initialized) {
      this._initialized = true;
      this._render();
    }
  }
}

customElements.define("bcontroller-card", BControllerCard);
