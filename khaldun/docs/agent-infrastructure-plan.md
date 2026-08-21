# Khaldun — Autonomous Prediction Market Agent: Infrastructure Plan

Status: draft, tied to task #2. Strategy (edge-detection vs arbitrage) is still
open — this plan is written so the infra doesn't depend on that decision.

## 1. Mental model

- **Brain** = Claude (Anthropic API), reasoning about market state and deciding actions.
- **Harness** = code we write ourselves: Claude API + **Tool Runner**, not the
  Claude Agent SDK. The Agent SDK is Claude Code repackaged (Read/Write/Bash,
  filesystem-shaped) — wrong tool surface for a trading agent. We define our
  own tools (`get_market_data`, `place_order`, `check_risk`, ...) and let the
  Tool Runner drive the loop.
- **Environment** = Polymarket (CLOB API for orders/orderbook, Gamma API for
  market metadata/resolution) on Polygon.

## 2. Layers

```
┌─────────────────────────────────────────────┐
│ Orchestration loop (scheduler)               │
│  → wakes agent every N minutes / on event    │
├─────────────────────────────────────────────┤
│ Agent (Claude + Tool Runner)                 │
│  → reads state, calls tools, decides         │
├───────────────┬───────────────┬─────────────┤
│ Data tools     │ Risk tools    │ Execution   │
│ get_market_    │ check_risk_   │ place_order │
│ data,          │ limits,       │ cancel_     │
│ get_orderbook, │ check_kill_   │ order       │
│ get_positions  │ switch        │             │
├───────────────┴───────────────┴─────────────┤
│ State store (SQLite → Postgres if needed)    │
│  positions, orders, decisions, P&L, logs     │
├───────────────────────────────────────────────┤
│ Polymarket (CLOB + Gamma API, py-clob-client)│
└─────────────────────────────────────────────┘
```

## 3. Components

### 3.1 Data layer
- Polymarket CLOB API (REST + WebSocket): live orderbook, prices, trades.
- Gamma API: market metadata, resolution criteria, categories.
- Historical data store (own DB — Polymarket has no official sandbox) for
  backtesting and for building the paper-trading simulator.

### 3.2 Strategy layer (pluggable — decided later, task #2)
- One module, one interface: `signal(market_state) -> decision`.
- Both candidates (edge detection, arbitrage) fit this interface, so infra
  below doesn't need to change once the choice is made.

### 3.3 Risk & guardrails layer — build this before anything trades real money
- Position size limits (per market, per category, total exposure).
- Max daily loss / drawdown circuit breaker → auto-pause.
- Kill switch: manual (one command) + automatic (drawdown threshold, error
  rate, stale-data detection).
- Dry-run/paper mode flag, checked on every order — must be explicit to
  disable.

### 3.4 Execution layer
- `py-clob-client` for order placement/cancellation (wallet-signed).
- Reconciliation: poll open orders/fills, compare against local state —
  never trust "the order call returned 200" as proof of a fill.

### 3.5 Agent loop
- Tool Runner (`client.beta.messages.tool_runner`), tools = data + risk +
  execution above.
- Every tool call and every model decision (including reasoning summary)
  logged — this is your audit trail when something goes wrong.
- Cadence: start with polling (every N minutes), not a continuous stream —
  cheaper, easier to reason about, easier to add a human checkpoint.

### 3.6 State / memory
- Reuse the SQLite pattern already proven in `.claude/state.db`, but as a
  **separate** `khaldun.db` — app data (positions, trades, decisions) must
  not mix with Claude-tooling metadata (sessions/handovers/tasks), so this
  repo's dev-collab infra stays copy-paste-able to other repos.
- Move to Postgres only if/when concurrent-write needs show up — not before.

### 3.7 Observability
- Structured (JSON) logs for every decision + trade.
- Alerting on: trade executed, risk-limit breach, kill switch triggered,
  tool/API error — push to Telegram/Discord webhook (cheapest to stand up).
- Minimal dashboard: open positions, P&L, last N decisions. Can be a flat
  script reading the DB before it's ever a real UI.

### 3.8 Deployment & ops
- Must run somewhere that isn't your laptop for real 24/7 operation — a
  small always-on VPS or a scheduled cloud job. Not a decision this plan
  needs to lock now; flag it before going live.
- Secrets (wallet private key, Anthropic API key) via env vars / secrets
  manager — never committed, never logged.

## 4. Build phases (in order — don't skip)

1. **Backtest** — strategy against historical Polymarket data, no live calls.
2. **Paper trade** — live market data, simulated fills, full agent loop
   running for real, zero real money at risk.
3. **Live, small, human-gated** — real capital (small), tight risk limits,
   agent proposes each trade, human approves before execution.
4. **Live, autonomous within guardrails** — human moves from approving each
   trade to monitoring alerts only.

Do not let the agent place a real order until phase 2 has run clean for a
meaningful stretch (days, not hours) — that's the actual bar for "not a toy."

## 5. Tech stack

- Python (already decided — matches `py-clob-client`).
- Anthropic Python SDK, `client.beta.messages.tool_runner`.
- SQLite (`khaldun.db`) to start.
- `py-clob-client` for Polymarket execution; Gamma API for market metadata.
- APScheduler or plain cron for loop cadence.
- Telegram/Discord webhook for alerting.

## 6. Open decisions (need you, not infra)

- Strategy: edge detection vs arbitrage (task #2, still discussing).
- Capital + risk tolerance for phase 3.
- Loop cadence (every 1 min? 15 min? event-driven later?).
- Hosting target for 24/7 operation.
- How long paper trading (phase 2) must run clean before phase 3.
