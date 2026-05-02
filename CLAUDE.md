## ⚠ Session orientation — read before doing anything

- **GCE is canonical.** The local `data/paper_trades.db` is a stale copy of old `local_sim` fills.
  Never run `research/` scripts locally to assess system state — always run on GCE:
  `<SSH prefix> sudo docker exec kinzie-daemon-1 python3 -m research.<script>`
  SSH prefix and VM details live in `RUNBOOK.local.md` (gitignored).
- **`EXECUTION_MODE=paper`** means real orders against `demo-api.kalshi.co` (Kalshi demo, not sim).
  "Paper" in the strategies table below means *deployed, 0 fills yet* — not a different execution mode.
- **`environment` column** in the crypto `trades` table stores uppercase: `PAPER`, `LIVE`, `LOCAL_SIM`.
  Any filtering code must use case-insensitive comparison (e.g. `env.lower() in ("paper", "live")`).
- **`core/db.py`** is a required shared module (SQLite WAL helper). It must be present in the Docker
  image. `ModuleNotFoundError: No module named 'core.db'` → the image is stale; rebuild and redeploy.

## System overview

Multi-strategy quant fund targeting Kalshi prediction market inefficiencies.
Each strategy is a self-contained pipeline in `strategies/<name>/` — adding one never touches existing ones.
Strategies may import from `core/` but must never modify other strategies. Each has its own daemon, config, and DB table.

## Strategies

| # | Name | Path | Edge source | Status |
|---|------|------|-------------|--------|
| 1 | Crypto Latency Arb | `strategies/crypto/` | Black-Scholes N(d2) vs Welford realized vol on Kalshi crypto binaries; fee-adjusted Kelly sizing | **Demo-book testing** since 2026-05-01 |
| 2 | Econ Data Arb | `strategies/econ/` | BLS/FRED post-release vs Kalshi MM reprice lag (CPI/NFP/PPI/FOMC) | Paper |
| 3 | Sports Latency Arb | `strategies/sports/` | ESPN final scores vs Kalshi winner markets (NFL/NBA/MLB) | Paper |
| 4 | Weather Data Arb | `strategies/weather/` | NOAA NWS readings vs Kalshi temp/precip thresholds (10 US cities) | Paper |

Each strategy: `strategies/<name>/daemon.py` is the entry point; Docker service is `daemon` (crypto), `<name>-daemon` (others).

**Crypto pipeline:** `CryptoFeedAgent → FeatureAgent → ScannerAgent → RiskAgent → ExecutionAgent → ResolutionAgent` (+ `WebsocketAgent` feeding FeatureAgent).
Currently in demo-book testing against `demo-api.kalshi.co`. No published baseline — re-baselining from scratch on real demo fills. Earlier `local_sim` synth-fill numbers are not representative and should not be cited.

Econ calendar hardcoded in `strategies/econ/core/calendar.py` — update annually. FOMC fetch needs `FRED_API_KEY`.

## Core invariants

- `strategies/crypto/core/pricing.py` and `core/kelly.py` are pure math — frozen.
- `strategies/<name>/core/config.py` is the single source of truth for that strategy's thresholds.
- `EXECUTION_MODE` (resolved in `core/environment.py`):
  - `local_sim` — no network, synthesizes fills locally.
  - `paper` (default) — real orders against **Kalshi demo** (`demo-api.kalshi.co`).
  - `live` — real orders against **Kalshi prod** (real money).
  Demo/prod creds live in separate env vars (`KALSHI_API_KEY_{DEMO,LIVE}` + matching `_PATH_*`). Resolver fails fast on missing creds and refuses obvious mismatches (e.g. `demo` in PEM filename when mode=live).
- All trades persisted to SQLite at `data/paper_trades.db` (WAL mode — `core/db.connect()` sets `PRAGMA journal_mode=WAL` on every open). `RiskAgent` owns crypto position state. **Canonical DB is the `kinzie-data` Docker volume on GCE** — the local `data/paper_trades.db` is a stale copy of old `local_sim` fills and should not be used for P&L analysis.
- Crypto `trades` table has an `environment` column (values: `PAPER`, `LIVE`, `LOCAL_SIM`) so sim rows don't mix with demo/prod rows in P&L.
- `RiskAgent` has a **breakeven gate**: rejects if `edge < kalshi_fee(P) + ESTIMATED_SLIPPAGE`. Parabolic fee peaks at P=0.5; gate is tighter ATM than at extremes. Recalibrate `ESTIMATED_SLIPPAGE` from demo fills once N≥50.
- Flip-to-live: see `RUNBOOK.local.md`. Requires `_LIVE` creds set.

## Monitoring

> Always run monitoring on GCE — local DB is stale. SSH prefix in `RUNBOOK.local.md`.

| What | Command (run on GCE via `docker exec kinzie-daemon-1`) |
|------|--------------------------------------------------------|
| Multi-strategy P&L | `python3 -m research.multi_pnl_dashboard` |
| Crypto P&L (Sharpe, age, open table) | `python3 -m research.pnl_dashboard` |
| Health / last fill / errors | `python3 -m research.health_check` |
| Replay backtest (crypto) | `python3 -m research.replay_backtest` |
| Per-trade edge analysis | `python3 -m research.edge_analysis` |

`multi_pnl_dashboard` reads all four strategy tables; only `paper`/`live` crypto rows count toward portfolio total.

## Production deployment — GCE

e2-small VM, us-central1-a, ~$14/mo. Source `/opt/kinzie/` · Secrets `/opt/kinzie/.env` + `kalshi_private.pem` · DB Docker volume `kinzie-data`.
Gotcha: `kinzie.service` passes `--project-directory /opt/kinzie` so `.env` loads from repo root, not `deploy/`.
Docker services: `daemon` (crypto), `econ-daemon`, `sports-daemon`, `weather-daemon`.
Container names for `docker exec`: `kinzie-daemon-1`, `kinzie-econ-daemon-1`, `kinzie-sports-daemon-1`, `kinzie-weather-daemon-1`.

SSH prefix, redeploy commands, VM IP, and project ID are in **`RUNBOOK.local.md`** (gitignored).

## Environment variables

**Shared:** `EXECUTION_MODE` (`local_sim`/`paper`/`live`); `KALSHI_API_KEY_{DEMO,LIVE}` + `KALSHI_PRIVATE_KEY_PATH_{DEMO,LIVE}`; legacy `KALSHI_API_KEY`/`KALSHI_PRIVATE_KEY_PATH` (research scripts + paper fallback); `BANKROLL_USDC` (default 100000; use 10000 for $10k).
**Crypto:** `TRACKED_SYMBOLS` (default `BTC,ETH`); `ESTIMATED_SLIPPAGE` (default 0.005 — recalibrate from fills); all other `strategies/crypto/core/config.py` numerics overridable via `Config.from_env()`.
**Econ:** `BLS_API_KEY` (optional), `FRED_API_KEY` (required for FOMC), `ECON_MIN_EDGE` (0.08), `ECON_TIMEOUT_MINUTES` (5), `ECON_POLL_SECONDS` (3).
**Sports:** `SPORTS_LEAGUES` (default `NFL,NBA,MLB`; also `NHL`), `SPORTS_MIN_EDGE` (0.08), `SPORTS_POLL_SECONDS` (30).
**Weather:** `WEATHER_MIN_EDGE` (0.08), `WEATHER_POLL_MINUTES` (15).

## Kalshi API

Base `https://api.elections.kalshi.com/trade-api/v2` · Auth RSA-PSS SHA-256. `yes_ask`/`yes_bid` are integer cents (1–99); `_parse_market()` divides by 100. 429 → exponential backoff (max 5 retries, cap 30s).
