[![CI](https://img.shields.io/github/actions/workflow/status/nxd914/kinzie/ci.yml?branch=main&label=CI)](https://github.com/nxd914/kinzie/actions)
[![License](https://img.shields.io/github/license/nxd914/kinzie)](https://github.com/nxd914/kinzie/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org)
[![GitHub last commit](https://img.shields.io/github/last-commit/nxd914/kinzie)](https://github.com/nxd914/kinzie/commits/main)

# Kinzie

Async prediction market trading daemon for Kalshi crypto markets. Deterministic execution pipeline — no ML, no heuristics. Every decision is a closed-form function of market price and realized volatility (Black-Scholes N(d2)), sized by fee-adjusted quarter-Kelly, gated behind hard risk controls.

## Pipeline

```
CryptoFeedAgent ──► FeatureAgent ──► ScannerAgent ──► RiskAgent ──► ExecutionAgent ──► ResolutionAgent
                                           ▲
                                     WebsocketAgent
                                    (real-time price cache)
```

Seven async agents coordinated through `asyncio.Queue` instances. No shared mutable state between agents.

## Stack

- **Runtime**: Python 3.11+, `asyncio`, frozen dataclasses throughout
- **Persistence**: SQLite (WAL mode) — every fill records market state at signal time
- **Testing**: pytest + [Hypothesis](https://hypothesis.readthedocs.io/) property-based tests
- **Quality**: ruff (lint + format), mypy (strict), GitHub Actions on every push and PR across Python 3.11/3.12
- **Deployment**: Docker + GCE (e2-small, us-central1-a); structured JSON logging via `LOG_FORMAT=json`

## Repository

```
strategies/crypto/          Daemon entry point and all trading agents
  daemon.py                 Entry point: python3 -m strategies.crypto.daemon
  agents/                   Seven async agents
  core/                     Config, features, logging, models, pricing

core/                       Shared utilities (db, kelly, kalshi_client, alert, environment)
research/                   Monitoring scripts: live_roi, pnl_dashboard, health_check, edge_analysis
scripts/                    Operational one-offs (sync_demo_fills, force_resolve, run.sh)
deploy/                     Dockerfile, docker-compose.yml, kinzie.service, gce_setup.sh
tests/                      Pytest suite + Hypothesis property tests
```

## Quick start

```bash
pip install -e ".[dev]"
pytest tests/ -q --tb=short
```

Requires credentials in a `.env` at the repo root. See `CLAUDE.md` for all environment variables and the full ops runbook.

## License

Proprietary. All rights reserved.
