# Kalshi Crypto Latency Arbitrage

Spot-price propagation latency arbitrage on Kalshi crypto binary contracts.

When BTC or ETH moves sharply on Binance or Coinbase, Kalshi's order book reprices seconds-to-minutes behind. The system measures that divergence with closed-form Black-Scholes N(d2) against Welford-estimated realized volatility, enters when edge exceeds the fee-adjusted threshold, and sizes positions with Kelly criterion.

No learned parameters. No heuristics. Every decision in the execution path is a deterministic function of spot price, realized vol, and the pricing model.

## Architecture

```
Binance.US WS ──┐
                ├──► CryptoFeedAgent ──► FeatureAgent ──► ScannerAgent
Coinbase WS ────┘         Tick         Welford O(1)      N(d2) vs ask
                                                              │
Kalshi WS ──────────── WebsocketAgent                        │
                       (price cache)  ───────────────────────┘
                                                              │
                                                         RiskAgent
                                                     Kelly sizing, circuit breakers
                                                              │
                                                      ExecutionAgent
                                                      fill → SQLite audit trail
                                                              │
                                                     ResolutionAgent
                                                     settlement poll, P&L
```

Seven async agents. No shared mutable state between agents — all coordination through typed queues and a read-only price cache. All models are frozen dataclasses.

## Pricing model

**Threshold contracts** (YES resolves if spot > K at expiry):

```
d2 = (ln(S/K) − 0.5σ²t) / (σ√t)
model_prob = N(d2)
```

No risk-free rate — prediction markets carry no financing cost. `σ` is 15-minute Welford realized vol, annualized. `t` is hours to expiry / 8760.

**Bracket contracts** ("ETH in [$L, $H]?"):

```
model_prob = [N(d2_floor) − N(d2_cap)] × BRACKET_CALIBRATION
```

`BRACKET_CALIBRATION = 0.55` — multiplicative haircut for TWAP settlement and discrete jump dynamics. See `docs/CALIBRATION.md`.

**Kelly sizing with fee adjustment:**

```
effective_price = ask + 0.07 × P × (1−P)   ← Kalshi parabolic taker fee
b = (1 / effective_price) − 1
f* = (p·b − (1−p)) / b
position = min(f* × 0.25, 0.10) × bankroll
```

## Risk controls

Every threshold is defined with its derivation in `core/config.py` and `docs/RISK_MODEL.md`.

| Control | Value | Rationale |
|---------|-------|-----------|
| Kelly fraction cap | 0.25× | Absorbs model prob estimation error without ruin |
| Min edge | 4% | Covers Kalshi taker fee at worst-case spread |
| Max concurrent positions | 5 | 50% max capital deployed; leaves margin buffer |
| Max single exposure | 10% bankroll | Per-position concentration limit |
| Daily loss circuit breaker | 20% bankroll | Halt on correlated loss scenario |
| Consecutive-loss halt | 3 losses → 24h pause | Catches edge decay below percentage threshold |
| Max signal age | 2s | Stale signal = Kalshi already repriced |
| Max hours to expiry | 4h | Far-dated contracts don't face convergence pressure |
| Spread floor | 4% | No maker rebates on Kalshi; tight spread = edge gone |
| Burst cooldown | 30s between fills | Prevents correlated fill cascade from single signal |
| NO fill range | [0.40, 0.95] | Risk/reward bounds on NO-side positions |
| Symbol concentration | 2 per symbol | Caps correlated BTC/ETH exposure |

## Performance

Hot-path benchmarks on Apple M-series (n=100,000 calls each):

| Component | Latency |
|-----------|---------|
| `RollingWindow.push()` — Welford O(1) update | 1.5 µs |
| `spot_to_implied_prob()` — closed-form N(d2) | 0.66 µs |
| `bracket_prob()` — two N(d2) calls + calibration | 1.6 µs |
| `capped_kelly()` — fee-adjusted sizing | 0.49 µs |

At 500 ticks/sec × 2 symbols, the feature + pricing budget is ~1 ms/sec total. Measured usage: <0.5%. No C extensions required.

## Repository

```
core/             Pure math — pricing.py, kelly.py, features.py, models.py, config.py
agents/           Async execution layer — seven concurrent agents
tests/            Pytest suite — 11 test modules, AAA pattern, 133 passing
benchmarks/       Hot-path profiling — RollingWindow, N(d2), Kelly
research/         Data capture, P&L analysis, market taxonomy tools
docs/
  STRATEGY.md     Edge thesis, pricing model derivation, execution flow
  RISK_MODEL.md   Every risk control with derivation and motivation
  CALIBRATION.md  BRACKET_CALIBRATION derivation and statistical validation plan
deploy/           Docker, docker-compose, Railway configuration
```

## Setup

See `docs/SETUP.md` for RSA key generation and Kalshi API registration.

```bash
git clone https://github.com/nxd914/latency.git && cd latency
pip install -e ".[dev]"
```

`.env` at repo root:

```
KALSHI_API_KEY=<uuid-from-kalshi-dashboard>
KALSHI_PRIVATE_KEY_PATH=~/.latency/private.pem
BANKROLL_USDC=<your_capital>
EXECUTION_MODE=paper
```

```bash
PYTHONPATH=. python3 daemon.py              # start all agents
pytest tests/                              # run test suite
python3 -m benchmarks.hot_path             # hot-path latency profile
python3 -m research.health_check           # P&L + process health
```

## Design notes

**Why N(d2) not N(d1)?** Prediction markets pay $1 on binary resolution — no delta-hedging is possible. N(d2) is the risk-neutral probability that S_T > K, which is exactly what the contract resolves on.

**Why Welford for vol?** O(1) amortized update with bounded memory. Rolling window with exact tick expiry, no full-scan recompute per tick. At 500 Hz this matters.

**Why 0.25× Kelly cap?** Standard conservative multiplier for systems with unverified edge. Full Kelly requires confidence in both the probability estimate and the model — fractional Kelly absorbs estimation error without ruin.

**Why `BRACKET_CALIBRATION = 0.55`?** Log-normal model overestimates narrow bracket probabilities under TWAP settlement and discrete jump dynamics. Derived in `docs/CALIBRATION.md`.

## License

MIT
