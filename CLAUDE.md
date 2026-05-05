## ⚠ Read this first — every session

**This repo is a single thing:** a Kalshi crypto latency-arb daemon. Nothing else lives here.

### Fill count is small — do not hallucinate P&L
- **N=4 real demo fills** as of 2026-05-03 (3W/1L, -$35.50 net on $336 risked).
- **Do not invent fills, trades, or P&L numbers.** Always query the DB or run a monitoring
  command to get real numbers. Any P&L or trade count you state must come from actual data.
- Local `data/paper_trades.db` is a **synced snapshot** — canonical data lives on GCE.
  Run monitoring on GCE (see below), not locally.

### Execution modes
- `EXECUTION_MODE=paper` → real orders against `demo-api.kalshi.co` (Kalshi demo account).
- `EXECUTION_MODE=live` → real orders against prod (real money). Not yet enabled.
- `local_sim` does not exist. There is no simulation mode.

### DB column values
- `trades.environment`: `PAPER` or `LIVE` (uppercase). Always filter case-insensitively:
  `lower(environment) IN ('paper', 'live')`.

---

## Repo map — what each folder does

```
strategies/crypto/          The daemon and all trading agents
  daemon.py                 Entry point. Run with: python3 -m strategies.crypto.daemon
  agents/                   Six async agents (see Pipeline below)
  core/                     config.py, features.py, logging.py, models.py, pricing.py

core/                       Shared utilities used by both agents and research scripts
  db.py                     SQLite WAL helper — MUST be in Docker image
  environment.py            Resolves EXECUTION_MODE once at startup
  kalshi_client.py          Kalshi REST + WebSocket client
  kelly.py                  Fee-adjusted Kelly sizing
  models.py                 Shared dataclasses (Tick, Market, etc.)
  alert.py                  Slack/logging alerts

research/                   Monitoring scripts — always run on GCE, not locally
  live_roi.py               Headline P&L, win rate, open positions
  pnl_dashboard.py          Sharpe, full trade table
  health_check.py           Daemon health + last fill
  edge_analysis.py          Per-trade edge breakdown
  replay_backtest.py        Replay resolved trades

scripts/                    Operational one-offs
  sync_demo_fills.py        Pull GCE DB snapshot to local
  check_env.py              Validate .env vars
  force_resolve.py          Manually resolve stuck positions
  run.sh                    Start/stop/restart daemon with PID management

deploy/                     Docker + GCE
  Dockerfile
  docker-compose.yml
  kinzie.service            systemd unit
  gce_setup.sh

tests/                      pytest suite — run with: pytest tests/
```

---

## Pipeline

```
CryptoFeedAgent → FeatureAgent → ScannerAgent → RiskAgent → ExecutionAgent → ResolutionAgent
```

Plus `WebsocketAgent` (feeds ticker prices into FeatureAgent; exposes `fill_events` queue).

All agents live in `strategies/crypto/agents/`. All are async; coordinated by `daemon.py`.

---

## Edge source (the math)

- **Pricing** (`strategies/crypto/core/pricing.py`):
  - `spot_to_implied_prob()` — Black-Scholes N(d2), no risk-free rate.
  - `bracket_prob()` — `(N(d2_floor) - N(d2_cap)) × BRACKET_CALIBRATION` (calibration = 0.55).
- **Sizing** (`core/kelly.py`):
  - Fee-adjusted quarter-Kelly. Taker fee: `0.07 × P × (1−P)` per contract.
  - `MAX_KELLY_FRACTION = 0.25`, `MIN_EDGE = 0.04`.
- **Risk gate** (`strategies/crypto/agents/risk_agent.py`):
  - Rejects if `edge < kalshi_fee(P) + ESTIMATED_SLIPPAGE` (default slippage = 0.005).
  - Per-expiry cap of 1 position to prevent correlated same-hour bets.
- All tunable constants are in `strategies/crypto/core/config.py` (`Config` dataclass).
  Override any via environment variable — see `Config.from_env()`.

---

## Core invariants — never violate these

1. **`core/db.py` must exist in the Docker image.** Missing it → `ModuleNotFoundError`.
2. **`EXECUTION_MODE` is resolved once** at startup via `core/environment.py`. Agents never
   read env vars directly.
3. **Order Groups safety net**: daemon creates an order group at startup with a 15-second
   rolling contracts cap (`ORDER_GROUP_CONTRACTS_LIMIT`, default 300). All orders carry this
   group_id. Exchange auto-cancels runaway orders.
4. **V2 order endpoint**: `place_limit_order()` uses `count_fp` + `yes_price_dollars`
   (string decimal). Legacy integer cents fields are gone.
5. **PEM path pitfall**: local `.env` has host paths; GCE `.env` must use container paths
   (`KALSHI_PRIVATE_KEY_PATH_DEMO=/app/kalshi_private.pem`). Don't blindly scp local .env
   to GCE — sed the paths.
6. **Redeploy doesn't delete.** After removing files locally, `sudo rm -rf` them on the VM
   before restarting, or stale modules will keep importing.

---

## Monitoring (run on GCE — local DB is a snapshot)

SSH prefix and VM IP are in `RUNBOOK.local.md` (gitignored).

```bash
# Inside: sudo docker exec kinzie-daemon-1 <command>
python3 -m research.live_roi          # headline P&L, win rate, open positions
python3 -m research.pnl_dashboard     # Sharpe, full trade table
python3 -m research.health_check      # daemon health + last fill + errors
python3 -m research.edge_analysis     # per-trade edge breakdown
python3 -m research.replay_backtest   # replay resolved trades
```

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `EXECUTION_MODE` | `paper` | `paper` = Kalshi demo, `live` = real money |
| `KALSHI_API_KEY_DEMO` | — | Demo API key |
| `KALSHI_PRIVATE_KEY_PATH_DEMO` | — | Demo PEM (container path on GCE) |
| `KALSHI_API_KEY_LIVE` | — | Prod API key (not yet used) |
| `KALSHI_PRIVATE_KEY_PATH_LIVE` | — | Prod PEM |
| `BANKROLL_USDC` | 100000 | Sizing basis (use 1064 to match real demo balance) |
| `TRACKED_SYMBOLS` | `BTC,ETH` | Symbols to trade |
| `ESTIMATED_SLIPPAGE` | 0.005 | Breakeven-gate slippage; recalibrate at N≥50 fills |
| `ORDER_GROUP_CONTRACTS_LIMIT` | 300 | Rolling 15s cap for runaway-order protection |

All `Config` numerics (min_edge, kelly_fraction_cap, etc.) are also overridable as env vars.
See `strategies/crypto/core/config.py` for the full list.

---

## Kalshi API

- Prod base: `https://api.elections.kalshi.com/trade-api/v2`
- Demo base: `https://demo-api.kalshi.co/trade-api/v2`
- Auth: RSA-PSS SHA-256 signed headers on every request.
- Order: POST `/portfolio/orders` with `count_fp` + `yes_price_dollars`.
- Order groups: POST `/portfolio/order_groups/create` → `order_group_id`.
- WebSocket: `wss://demo-api.kalshi.co/trade-api/ws/v2` — channels: `ticker`, `fill`.
- 429 → exponential backoff (max 5 retries, 30s cap).

---

## GCE deployment

e2-small, us-central1-a (~$14/mo). Source: `/opt/kinzie/`. Secrets: `/opt/kinzie/.env` +
`kalshi_private.pem`. DB: `kinzie-data` Docker volume. Container: `kinzie-daemon-1`.

`kinzie.service` uses `--project-directory /opt/kinzie`, so `.env` loads from repo root.

---

## What does NOT exist here

- No simulation mode, no `local_sim`.
- No `tools/` CLI (`quant scan`, `quant paper`, etc.) — deleted in crypto pivot.
- No `docs/` folder — all docs were stale; source of truth is the code itself.
- No website files — those were for a different project phase, also deleted.
- No multi-strategy framework. One strategy, one daemon.
