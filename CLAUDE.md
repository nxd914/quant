## Session orientation

- **Crypto-only.** Only the Kalshi crypto latency-arb bot exists in this repo.
- **GCE is canonical.** Local `data/paper_trades.db` is a stale snapshot. All live state lives in the GCE `kinzie-data` Docker volume. Always run monitoring on GCE.
- `EXECUTION_MODE=paper` → real orders against `demo-api.kalshi.co`. `EXECUTION_MODE=live` → real money.
- `trades.environment` stores `PAPER` or `LIVE` (uppercase). Filter with `lower(environment) IN (...)`.
- `core/db.py` must be in the Docker image. `ModuleNotFoundError: No module named 'core.db'` → image is stale, rebuild.
- **PEM path pitfall.** GCE `.env` must use container path `KALSHI_PRIVATE_KEY_PATH_DEMO=/app/kalshi_private.pem`. Never blindly `scp .env` from local — `sed` the paths after copying.
- **Redeploy doesn't delete.** After removing files locally, `sudo rm -rf` those paths on the VM before restarting or stale code persists.

## What this is

Kalshi crypto binary markets bot (BTC, ETH + KXBTC15M/KXETH15M Up/Down). Black-Scholes N(d2) pricing vs Welford realized vol → fee-adjusted quarter-Kelly sizing. Currently demo-book testing (N=4 fills as of 2026-05-03).

**Pipeline:** `CryptoFeedAgent → FeatureAgent → ScannerAgent → RiskAgent → ExecutionAgent → ResolutionAgent` + `WebsocketAgent` (ticker prices + fill events).

Entry point: `strategies/crypto/daemon.py`. Container: `kinzie-daemon-1`.

## Edge / pricing

- `strategies/crypto/core/pricing.py` — `spot_to_implied_prob()` BS N(d2); `bracket_prob()` = N(d2_floor)−N(d2_cap) × 0.55; `up_down_15m_prob()` for directional 15-min markets (~0.50 without drift).
- `core/kelly.py` — fee `0.07×P×(1−P)` folded into breakeven gate.
- `RiskAgent` rejects if `edge < kalshi_fee(P) + ESTIMATED_SLIPPAGE`. Per-expiry cap (1) prevents correlated same-hour bets.
- Math knobs unfrozen — tune as fills accumulate. Tests in `tests/test_pricing.py` lock invariants.

## Recent changes (2026-05-03)

- V2 order endpoint live (`/portfolio/events/orders`, `bid`/`ask` sides, `count_fp` string)
- 15-min Up/Down markets supported: `KXBTC15M`, `KXETH15M` now scanned
- Signal age bug fixed: `max_signal_age_seconds` 2s→30s (scanner had 5s cooldown, making signal-triggered fills impossible)
- `_post()` now logs non-200 HTTP responses instead of silently returning `{}`
- `_ticker_to_symbol` prefix order fixed for 15M tickers (KXBTC15M before KXBTC)
- SOL/XRP added to `risk_agent._ticker_to_symbol`; feed config still needed

## Core invariants

- `EXECUTION_MODE` resolved once at startup via `core/environment.py`. Agents never read env vars directly.
- Order Groups safety net: 15s rolling matched-contracts cap, exchange-side runaway protection.
- V2 response uses `fill_count` (fixed-point string); `filled_count` (int) is legacy fallback.

## Monitoring (run on GCE)

```
sudo docker exec kinzie-daemon-1 python3 -m research.live_roi        # headline ROI
sudo docker exec kinzie-daemon-1 python3 -m research.health_check    # health / last fill
sudo docker exec kinzie-daemon-1 python3 -m research.pnl_dashboard   # Sharpe, full table
```

SSH prefix and VM details in `RUNBOOK.local.md` (gitignored).

## Environment variables

- `EXECUTION_MODE` — `paper` | `live`
- `KALSHI_API_KEY_DEMO` + `KALSHI_PRIVATE_KEY_PATH_DEMO` — demo creds
- `TRACKED_SYMBOLS` — default `BTC,ETH`; SOL/XRP ready in risk layer, needs feed configs
- `ESTIMATED_SLIPPAGE` — default 0.005; recalibrate at N≥50 fills
- `ORDER_GROUP_CONTRACTS_LIMIT` — default 300

## Next thresholds

| N fills | Action |
|---------|--------|
| 50 | Recalibrate `BRACKET_CALIBRATION` (0.55) and `ESTIMATED_SLIPPAGE` (0.005) |
| 50 | Wire `drift` from FeatureAgent momentum EWMA into pricing |
| 100 + Sharpe ≥ 1.0 | Flip to live (`EXECUTION_MODE=live`) |

## Kalshi API

- Demo base: `https://demo-api.kalshi.co/trade-api/v2`
- Order placement: `POST /portfolio/events/orders` (V2)
- Order groups: `POST /portfolio/order_groups/create`
- WS: `wss://demo-api.kalshi.co/trade-api/ws/v2` — subscribe `["ticker", "fill"]`
- 429 → exponential backoff (max 5 retries, cap 30s)
