"""
Kalshi API client with RSA-PSS authentication.

Handles:
  - Signed request generation (RSA-PSS SHA-256)
  - Authenticated market browsing by volume / series / event
  - Order placement (live via authenticated endpoint)

Auth flow (one-time setup):
  1. Generate RSA key pair:
       openssl genrsa -out ~/.quant/kalshi_private.pem 2048
       openssl rsa -in ~/.quant/kalshi_private.pem -pubout -out ~/.quant/kalshi_public.pem
  2. Upload the public key to https://kalshi.com/account/api → get your key ID
  3. Set env vars in .env:
       KALSHI_API_KEY=<key-id-from-dashboard>
       KALSHI_PRIVATE_KEY_PATH=~/.quant/kalshi_private.pem

  KalshiClient then loads the key on init and signs every request automatically.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .models import KalshiMarket

logger = logging.getLogger(__name__)

DEFAULT_KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_KALSHI_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
REQUEST_TIMEOUT = 10.0
GET_429_MAX_RETRIES = 5
POST_429_MAX_RETRIES = 3
POST_429_MAX_DELAY = 5.0  # cap at 5s — skip trade rather than stall 30s
_HTTP_BODY_LOG_MAX = 800


def _http_body_preview(text: str, max_len: int = _HTTP_BODY_LOG_MAX) -> str:
    t = text.strip().replace("\n", " ")
    if len(t) <= max_len:
        return t
    return t[: max_len - 3] + "..."

# Market quality filters — tune as data accumulates
MIN_VOLUME_24H = 50          # $50 24h volume floor
MIN_LIQUIDITY  = 10          # $10 liquidity floor
MIN_YES_PRICE  = 0.03        # exclude near-certain NO markets
MAX_YES_PRICE  = 0.97        # exclude near-certain YES markets
MAX_SPREAD_PCT = 0.20        # exclude illiquid wide-spread markets


class KalshiClient:
    """
    Async Kalshi API client with RSA-PSS request signing.

    Use as an async context manager:
        async with KalshiClient() as client:
            markets = await client.get_top_markets()

    Or call open() / close() manually in long-running agents.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key_path: Optional[str | Path] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KALSHI_API_KEY", "")
        raw_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        expanded = Path(os.path.expanduser(str(raw_path))) if raw_path else None
        self._private_key: Optional[RSAPrivateKey] = (
            _load_rsa_key(expanded) if expanded else None
        )
        self._base_url = (base_url or DEFAULT_KALSHI_BASE).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self.authenticated = bool(self._api_key and self._private_key)

        if not self.authenticated:
            logger.warning(
                "KalshiClient: unauthenticated mode — set KALSHI_API_KEY and "
                "KALSHI_PRIVATE_KEY_PATH for full market access and order placement."
            )

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "KalshiClient":
        await self.open()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def open(self) -> None:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Market browsing
    # ------------------------------------------------------------------

    async def get_top_markets(
        self,
        limit: int = 100,
        min_volume_24h: float = MIN_VOLUME_24H,
        min_liquidity: float = MIN_LIQUIDITY,
    ) -> list[KalshiMarket]:
        """
        Fetch the most actively traded Kalshi markets.

        Uses cursor-based pagination. Filters out:
          - Markets below volume/liquidity floors
          - Near-certain markets (price < 3% or > 97%)
          - Wide-spread illiquid markets
        """
        collected: list[KalshiMarket] = []
        
        # Strategy: Get active events (themes) first, then get markets for those events.
        # This bypasses the ~10,000 "combo" markets that flood the generic /markets endpoint.
        events_data = await self._get("/events", params={"limit": 50, "status": "open"})
        events = events_data.get("events") or []
        
        for event in events:
            if len(collected) >= limit:
                break
                
            event_ticker = event.get("event_ticker")
            if not event_ticker:
                continue
                
            # Fetch markets for this specific event
            event_markets = await self.get_markets_by_event(event_ticker)
            
            for m in event_markets:
                if m.volume_24h < min_volume_24h:
                    continue
                if m.liquidity < min_liquidity:
                    continue
                if m.spread_pct > MAX_SPREAD_PCT:
                    continue
                collected.append(m)
                
            # Pace requests to avoid 429s during discovery
            await asyncio.sleep(0.1)

        # Sort by 24h volume descending — most active markets first
        collected.sort(key=lambda m: m.volume_24h, reverse=True)
        return collected[:limit]

    async def list_open_markets_raw(
        self,
        *,
        max_pages: int = 100,
        per_page: int = 200,
    ) -> list[dict]:
        """
        Paginate GET /markets (open) and return raw API dicts — no volume/spread filters.
        For one-off diagnostics and taxonomy scripts.
        """
        out: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params: dict = {"limit": per_page, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            batch = data.get("markets") or []
            if not batch:
                break
            out.extend(batch)
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def get_events(
        self,
        limit: int = 50,
        status: str = "open",
    ) -> list[dict]:
        """Fetch active events (themes) from Kalshi."""
        data = await self._get("/events", params={"limit": limit, "status": status})
        return data.get("events") or []

    async def get_markets_by_event(
        self,
        event_ticker: str,
        limit: int = 50,
    ) -> list[KalshiMarket]:
        """Fetch all active markets under a specific Kalshi event."""
        data = await self._get("/markets", params={
            "event_ticker": event_ticker,
            "limit": limit,
            "status": "open",
        })
        raw_markets = data.get("markets") or []
        return [m for raw in raw_markets if (m := _parse_market(raw)) is not None]

    async def get_markets_by_series(
        self,
        series_ticker: str,
        limit: int = 50,
    ) -> list[KalshiMarket]:
        """
        Fetch active markets for a given series (e.g. 'KXBTC', 'KXETH').
        Uses /markets?series_ticker= directly — the series/events traversal
        returns an empty events list for crypto series.
        """
        data = await self._get("/markets", params={
            "series_ticker": series_ticker,
            "limit": limit,
            "status": "open",
        })
        raw_markets = data.get("markets") or []
        return [m for raw in raw_markets if (m := _parse_market(raw)) is not None]

    async def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """Fetch a single market by ticker and return current state."""
        data = await self._get(f"/markets/{ticker}")
        raw = data.get("market")
        return _parse_market(raw) if raw else None

    async def get_market_for_resolution(self, ticker: str) -> Optional[dict]:
        """Fetch raw market data by ticker (no price filter). For resolution only."""
        data = await self._get(f"/markets/{ticker}")
        return data.get("market")

    async def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        """
        Fetch the order book for a specific market.
        Uses the V2 orderbook endpoint which returns lists of [price, quantity] pairs.
        Returns a dict with 'yes' and 'no' sides, each containing 'bids' and 'asks'.
        """
        data = await self._get(f"/markets/{ticker}/orderbook2", params={"depth": depth})
        orderbook = data.get("orderbook", {})
        
        # Structure matches Kalshi API:
        # { "yes": [[price, quantity], ...], "no": [[price, quantity], ...] }
        return {
            "yes": orderbook.get("yes", []),
            "no": orderbook.get("no", [])
        }

    # ------------------------------------------------------------------
    # Portfolio / Account (authenticated)
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Return available balance in USD. Requires auth."""
        if not self.authenticated:
            raise RuntimeError("KalshiClient: auth required for get_balance()")
        data = await self._get("/portfolio/balance")
        # Balance is returned in cents
        cents = data.get("balance", 0)
        return cents / 100.0

    async def get_positions(self) -> list[dict]:
        """Return open positions. Requires auth."""
        if not self.authenticated:
            raise RuntimeError("KalshiClient: auth required for get_positions()")
        data = await self._get("/portfolio/positions")
        return data.get("market_positions") or []

    # ------------------------------------------------------------------
    # Order placement (authenticated)
    # ------------------------------------------------------------------

    async def place_limit_order(
        self,
        ticker: str,
        side: str,                   # "yes" or "no"
        count: int,                  # number of contracts
        yes_price_dollars: float,    # YES limit price in dollars (e.g. 0.65)
        order_group_id: Optional[str] = None,
        time_in_force: str = "good_till_canceled",
    ) -> dict:
        """
        Place a limit buy order on Kalshi via the V2 event-market order endpoint.

        V2 uses "bid"/"ask" side semantics (single-book model):
          - "yes" → "bid"  (buying YES contracts)
          - "no"  → "ask"  (selling YES = buying NO)

        Optional order_group_id ties the order to a rolling matched-contracts
        cap created at daemon startup — exchange-side runaway protection.
        """
        if not self.authenticated:
            raise RuntimeError(
                "KalshiClient: auth not configured. "
                "Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH."
            )
        v2_side = "bid" if side.lower() == "yes" else "ask"
        price = max(0.01, min(0.99, float(yes_price_dollars)))
        payload: dict = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": v2_side,
            "count": f"{int(count)}.00",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if order_group_id:
            payload["order_group_id"] = order_group_id
        return await self._post("/portfolio/events/orders", payload)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting order by ID."""
        if not self.authenticated:
            raise RuntimeError("KalshiClient: auth required for cancel_order()")
        return await self._delete(f"/portfolio/events/orders/{order_id}")

    async def create_order_group(self, contracts_limit: int) -> Optional[str]:
        """
        Create an order group with a rolling 15-second matched-contracts cap.

        Exchange-side runaway protection: if the daemon fires too many
        contracts in 15s (e.g. a pricing bug), Kalshi auto-cancels every
        order in the group and refuses new ones until reset.

        Returns the new order_group_id, or None on failure (caller should
        proceed without group binding rather than refuse to start).
        """
        if not self.authenticated:
            raise RuntimeError("KalshiClient: auth required for create_order_group()")
        resp = await self._post(
            "/portfolio/order_groups/create",
            {"subaccount": 0, "contracts_limit": int(contracts_limit)},
        )
        gid = resp.get("order_group_id")
        if not gid:
            logger.warning("create_order_group: no id in response: %s", resp)
            return None
        return gid

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if self._session is None:
            raise RuntimeError("KalshiClient not opened. Use 'async with' or call open().")

        url = self._base_url + path
        headers = (
            self._signed_headers("GET", path)
            if self.authenticated
            else {"User-Agent": "quant/0.3"}
        )

        attempt = 0
        while True:
            try:
                async with self._session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 429:
                        await resp.read()
                        if attempt >= GET_429_MAX_RETRIES - 1:
                            logger.warning(
                                "Kalshi rate limit (GET %s) — max retries (%d) exceeded",
                                path,
                                GET_429_MAX_RETRIES,
                            )
                            return {}
                        delay = min(30.0, 2.0**attempt)
                        logger.warning(
                            "Kalshi rate limit (GET %s) — sleeping %.1fs then retry %d/%d",
                            path,
                            delay,
                            attempt + 1,
                            GET_429_MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    if resp.status == 401:
                        body = _http_body_preview(await resp.text())
                        logger.error(
                            "Kalshi GET %s → 401 auth failed — body: %s",
                            path,
                            body or "(empty)",
                        )
                        return {}
                    if resp.status not in (200, 201):
                        body = _http_body_preview(await resp.text())
                        logger.warning(
                            "Kalshi GET %s → HTTP %s — body: %s",
                            path,
                            resp.status,
                            body or "(empty)",
                        )
                        return {}
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Kalshi GET %s transport error: %s", path, exc)
                return {}

    async def _post(self, path: str, payload: dict) -> dict:
        if self._session is None:
            raise RuntimeError("KalshiClient not opened.")

        url = self._base_url + path
        headers = self._signed_headers("POST", path)
        headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            try:
                async with self._session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 429:
                        await resp.read()
                        if attempt >= POST_429_MAX_RETRIES - 1:
                            logger.warning(
                                "Kalshi rate limit (POST %s) — max retries (%d) exceeded, skipping",
                                path,
                                POST_429_MAX_RETRIES,
                            )
                            return {}
                        delay = min(POST_429_MAX_DELAY, 2.0**attempt)
                        logger.warning(
                            "Kalshi rate limit (POST %s) — sleeping %.1fs then retry %d/%d",
                            path,
                            delay,
                            attempt + 1,
                            POST_429_MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    if resp.status not in (200, 201):
                        body = _http_body_preview(await resp.text())
                        logger.warning(
                            "Kalshi POST %s → HTTP %s — body: %s",
                            path,
                            resp.status,
                            body or "(empty)",
                        )
                        return {}
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Kalshi POST %s transport error: %s", path, exc)
                return {}

    async def _delete(self, path: str) -> dict:
        if self._session is None:
            raise RuntimeError("KalshiClient not opened.")

        url = self._base_url + path
        headers = self._signed_headers("DELETE", path)

        try:
            async with self._session.delete(url, headers=headers) as resp:
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("Kalshi DELETE error %s: %s", path, exc)
            return {}

    def _signed_headers(self, method: str, path: str) -> dict:
        """Generate RSA-PSS signed headers for REST or WebSocket handshake."""
        # For WebSocket handshakes, the path usually prefix is different
        if path.startswith("/ws"):
            full_path = f"/trade-api{path}"
        else:
            full_path = f"/trade-api/v2{path}"
        return _make_signed_headers(self._api_key, self._private_key, method, full_path)


class KalshiWebsocketClient:
    """
    Async Kalshi WebSocket client.
    Handles handshake, subscriptions, and message streaming.
    """
    def __init__(
        self,
        api_key: str,
        private_key: RSAPrivateKey,
        ws_url: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        self.private_key = private_key
        self.ws_url = ws_url or DEFAULT_KALSHI_WS
        self._ws = None

    async def connect(self):
        import websockets
        # WS handshake is a GET request to the WS URL path
        headers = _make_signed_headers(self.api_key, self.private_key, "GET", "/trade-api/ws/v2")
        self._ws = await websockets.connect(self.ws_url, additional_headers=headers)
        return self._ws

    async def subscribe(self, channels: list[str], tickers: Optional[list[str]] = None):
        if not self._ws:
            raise RuntimeError("WS not connected")
        msg = {
            "id": int(time.time()),
            "cmd": "subscribe",
            "params": {"channels": channels}
        }
        if tickers:
            msg["params"]["tickers"] = tickers
        await self._ws.send(json.dumps(msg))

    async def recv(self):
        if not self._ws:
            return None
        raw = await self._ws.recv()
        return json.loads(raw)


def _make_signed_headers(api_key: str, private_key: RSAPrivateKey, method: str, full_path: str) -> dict:
    """Generic RSA-PSS signing for Kalshi headers."""
    timestamp_ms = int(time.time() * 1000)
    message = f"{timestamp_ms}{method}{full_path}".encode()

    signature = private_key.sign(
        message,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "User-Agent": "quant/0.3",
    }


# ------------------------------------------------------------------
# Market parsing
# ------------------------------------------------------------------

def _parse_market(raw: dict) -> Optional[KalshiMarket]:
    """Parse a raw Kalshi API market dict into a typed KalshiMarket.

    Kalshi v2 returns prices as integer cents (1–99).  Earlier code used
    non-existent ``*_dollars`` field names which always resolved to 0 and
    caused every market to be silently rejected.  We divide by 100 to
    normalise to the 0–1 probability range used everywhere else.
    """
    if not raw:
        return None
    try:
        ticker = raw.get("ticker", "")
        if not ticker or ticker.startswith("KXMVE"):
            # skip non-binary multivariate combo markets
            return None

        # Prices come in two flavours depending on the field:
        #   - Primary key (e.g. "yes_ask"): integer cents 1–99, divide by 100
        #   - Fallback "*_dollars" key: decimal dollars 0.0–1.0, use as-is
        # IMPORTANT: do NOT apply the /100 heuristic to *_dollars fields —
        # no_ask_dollars="1.0000" means $1.00 (100%), not 1 cent.
        def _to_prob(key: str, fallback_key: str) -> float:
            v = raw.get(key)
            if v is not None:
                val = float(v)
                return val / 100.0 if val >= 1.0 else val
            v = raw.get(fallback_key)
            if v is None:
                return 0.0
            # *_dollars field: already a decimal probability in [0.0, 1.0]
            return min(float(v), 1.0)

        yes_ask = _to_prob("yes_ask", "yes_ask_dollars")
        yes_bid = _to_prob("yes_bid", "yes_bid_dollars")
        no_ask  = _to_prob("no_ask",  "no_ask_dollars")
        no_bid  = _to_prob("no_bid",  "no_bid_dollars")

        # Require at least some pricing
        if yes_ask <= 0 and yes_bid <= 0:
            return None

        # Implied probability = mid of yes bid/ask
        if yes_bid > 0 and yes_ask > 0 and yes_ask >= yes_bid:
            implied_prob = (yes_bid + yes_ask) / 2.0
        elif yes_ask > 0:
            implied_prob = yes_ask
        elif yes_bid > 0:
            implied_prob = yes_bid
        else:
            return None

        # Filter extreme near-certain markets
        if not (MIN_YES_PRICE <= implied_prob <= MAX_YES_PRICE):
            return None

        # Spread as fraction of mid
        spread_pct = (
            (yes_ask - yes_bid) / implied_prob
            if yes_ask > yes_bid and implied_prob > 0
            else 0.0
        )

        # Volume: v2 uses volume_24h_fp (fixed point / cents); fall back to legacy name
        # We assume _fp fields are in cents and convert to USD.
        raw_vol = raw.get("volume_24h_fp") or raw.get("volume_24h") or 0
        volume_24h = float(raw_vol) / 100.0 if str(raw_vol).isdigit() else float(raw_vol)

        # Liquidity: liquidity_dollars or open_interest
        # Note: open_interest is usually in contracts (cents), liquidity_dollars is USD
        raw_liq = raw.get("liquidity_dollars") or raw.get("liquidity") or 0
        if raw_liq:
            liquidity = float(raw_liq)
        else:
            # Fallback to open_interest if liquidity is missing
            raw_oi = raw.get("open_interest") or 0
            liquidity = float(raw_oi) / 100.0 if str(raw_oi).isdigit() else float(raw_oi)

        raw_floor = raw.get("floor_strike")
        raw_cap   = raw.get("cap_strike")

        return KalshiMarket(
            ticker=ticker,
            title=raw.get("title", "")[:200],
            event_ticker=raw.get("event_ticker", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            implied_prob=implied_prob,
            spread_pct=spread_pct,
            volume_24h=volume_24h,
            liquidity=liquidity,
            close_time=raw.get("close_time", ""),
            timestamp=datetime.now(tz=timezone.utc),
            strike_type=raw.get("strike_type", ""),
            floor_strike=float(raw_floor) if raw_floor is not None else None,
            cap_strike=float(raw_cap)     if raw_cap   is not None else None,
            status=raw.get("status", ""),
            result=raw.get("result", ""),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.debug("Market parse error: %s", exc)
        return None


def market_from_api_dict(raw: dict) -> Optional[KalshiMarket]:
    """Parse a raw ``/markets`` row into ``KalshiMarket`` (research / capture tooling)."""
    return _parse_market(raw)


def _load_rsa_key(path: Path) -> Optional[RSAPrivateKey]:
    """Load an RSA private key from a PEM file."""
    try:
        pem_bytes = path.read_bytes()
        key = serialization.load_pem_private_key(pem_bytes, password=None)
        logger.info("Kalshi RSA private key loaded from %s", path)
        return key  # type: ignore[return-value]
    except FileNotFoundError:
        logger.warning("Kalshi private key not found at %s", path)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Failed to load Kalshi private key from %s: %s", path, exc)
        return None
