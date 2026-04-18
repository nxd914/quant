# chiron

[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Kalshi](https://img.shields.io/badge/exchange-Kalshi-0a1628.svg)](https://kalshi.com)
[![Status](https://img.shields.io/badge/status-paper_trading-yellow.svg)](#usage)
[![Tests](https://img.shields.io/badge/tests-pytest-009688.svg?logo=pytest&logoColor=white)](./chiron/tests)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

Automated arbitrage between real-time crypto CEX feeds and Kalshi BTC/ETH prediction markets.

**Edge**: spot-price propagation latency between Binance.US / Coinbase and Kalshi BTC/ETH probability contracts. Deterministic Black-Scholes N(d2) pricing, Kelly-capped sizing, zero learned parameters in the execution path.

> Paper mode only. Not financial advice.

## Quick start

```bash
pip install git+https://github.com/nxd914/chiron.git

mkdir -p ~/.chiron
openssl genrsa -out ~/.chiron/private.pem 2048
openssl rsa -in ~/.chiron/private.pem -pubout -out ~/.chiron/public.pem
```

Set environment variables (see [Environment](#environment)), then:

```bash
chiron-daemon          # run the trading daemon
chiron paper           # paper trade via CLI
chiron scan            # scan current Kalshi markets
chiron history         # view trade history
```

## Architecture

```
CryptoFeedAgent (Binance.US + Coinbase WS)
        ↓ tick_queue
FeatureAgent (Welford rolling windows — 60s signal, 15min pricing)
        ↓ signal_queue
WebsocketAgent (Kalshi WS price cache)
        ↓
ScannerAgent (N(d2) / bracket_prob scoring, edge filter)
        ↓
RiskAgent (Kelly + position limits + proactive exposure gate)
        ↓
ExecutionAgent (paper mode, SQLite audit trail)
        ↓
ResolutionAgent (settlement polling, P&L close)
```

## Environment

| Var | Required | Notes |
|-----|----------|-------|
| `KALSHI_API_KEY` | yes | UUID from Kalshi dashboard |
| `KALSHI_PRIVATE_KEY_PATH` | yes | `~/.chiron/private.pem` |
| `BANKROLL_USDC` | no | default `5000` |

Create a `.env` file at the repo root or export these in your shell.

## Risk controls

| Control | Value |
|---------|-------|
| Kelly cap | 0.25× |
| Min edge | 4% |
| Max concurrent positions | 5 |
| Max single exposure | 10% of bankroll |
| Max positions per symbol | 2 |
| Daily loss gate | 20% of bankroll |
| Min NO fill price | 0.40 |
| Max hours to expiry | 4 |
| Min seconds between fills | 30 |
| Spread floor | 4% |

## Development

```bash
pip install -e ".[dev]"
pytest chiron/tests/
```

## License

MIT — see [LICENSE](./LICENSE).
