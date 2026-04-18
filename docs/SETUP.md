# Setup

## Kalshi API credentials

Generate an RSA-2048 key pair and register the public key with Kalshi:

```bash
mkdir -p ~/.latency
openssl genrsa -out ~/.latency/private.pem 2048
openssl rsa -in ~/.latency/private.pem -pubout -out ~/.latency/public.pem
```

Upload `~/.latency/public.pem` at [kalshi.com/account/api](https://kalshi.com/account/api) and copy the resulting key UUID into your `.env`:

```
KALSHI_API_KEY=<uuid-from-kalshi-dashboard>
KALSHI_PRIVATE_KEY_PATH=~/.latency/private.pem
```

Auth uses RSA-PSS SHA-256. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KALSHI_API_KEY` | Yes | — | UUID from Kalshi dashboard |
| `KALSHI_PRIVATE_KEY_PATH` | Yes | — | Path to RSA-2048 PEM file |
| `BANKROLL_USDC` | No | 100000 | Starting capital |
| `EXECUTION_MODE` | No | paper | `paper` or `live` |
| `TRACKED_SYMBOLS` | No | BTC,ETH | Comma-separated symbols |

All `core/config.py` fields are overridable via env var — see `Config.from_env()`.

## Rate limits

Kalshi enforces per-key rate limits. 429 responses trigger exponential backoff (max 5 retries, cap 30s). Never run two processes against the same API key simultaneously.
