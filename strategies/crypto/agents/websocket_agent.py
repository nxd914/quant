"""
Kalshi WebSocket Agent

Maintains a real-time in-memory cache of market prices by subscribing to
Kalshi's global 'ticker' channel. Reduces price-discovery latency from
REST-polling cadence (seconds-to-minutes) to sub-second.

The cache is read-only from the perspective of other agents — ScannerAgent
calls get_price() or reads price_cache directly but never writes to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, TypedDict

from core.kalshi_client import KalshiWebsocketClient, _load_rsa_key

logger = logging.getLogger(__name__)

_INITIAL_RECONNECT_DELAY: float = 1.0
_MAX_RECONNECT_DELAY: float = 60.0


class PriceSnapshot(TypedDict):
    """Normalized real-time price snapshot for a single Kalshi market."""
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume_24h: float
    liquidity: float
    last_price: float
    ts: Optional[int]


class WebsocketAgent:
    """
    Manages the authenticated real-time WebSocket connection to Kalshi.

    Subscribes to the global ticker channel on connect and updates
    price_cache on every incoming message. Reconnects with exponential
    backoff on disconnect or error.

    If no private key is available, run() exits immediately — the scanner
    falls back to REST-polled prices from KalshiClient.
    """

    def __init__(
        self,
        api_key: str,
        private_key_path: str,
        ws_url: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        expanded_path = Path(os.path.expanduser(str(private_key_path))) if private_key_path else None
        self.private_key = _load_rsa_key(expanded_path) if expanded_path else None
        self.ws_url = ws_url
        self.client: Optional[KalshiWebsocketClient] = None
        self.price_cache: dict[str, PriceSnapshot] = {}
        # Account-level fill events delivered sub-second. PortfolioAgent
        # drains this queue and registers fills with RiskAgent.
        self.fill_events: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._is_running = False

    async def run(self) -> None:
        """Main loop with exponential-backoff reconnection. Runs until cancelled."""
        if not self.private_key:
            logger.warning("WebsocketAgent: no private key — real-time price cache disabled.")
            return

        self.client = KalshiWebsocketClient(self.api_key, self.private_key, ws_url=self.ws_url)
        self._is_running = True
        retry_delay = _INITIAL_RECONNECT_DELAY

        while self._is_running:
            try:
                logger.info("WebsocketAgent: connecting to Kalshi...")
                await self.client.connect()
                await self.client.subscribe(channels=["ticker", "fill"])
                logger.info("WebsocketAgent: subscribed to channels: ticker, fill")
                retry_delay = _INITIAL_RECONNECT_DELAY

                while self._is_running:
                    msg = await self.client.recv()
                    if not msg:
                        break
                    self._handle_message(msg)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "WebsocketAgent error: %s. Retrying in %.1fs...", exc, retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(_MAX_RECONNECT_DELAY, retry_delay * 2)

    def _handle_message(self, msg: dict) -> None:
        """Parse incoming WS messages: 'ticker' updates price_cache, 'fill' enqueues."""
        msg_type = msg.get("type")

        if msg_type == "fill":
            payload = msg.get("msg") or {}
            logger.info(
                "WS fill | %s | %s @ $%s × %s | order=%s",
                payload.get("market_ticker"),
                (payload.get("side") or "").upper(),
                payload.get("yes_price_dollars"),
                payload.get("count_fp"),
                payload.get("order_id"),
            )
            try:
                self.fill_events.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("WS fill_events queue full — dropping event")
            return

        if msg_type != "ticker":
            return

        ticker = msg.get("ticker")
        if not ticker:
            return

        self.price_cache[ticker] = PriceSnapshot(
            yes_bid=_cents_to_prob(msg.get("yes_bid", 0)),
            yes_ask=_cents_to_prob(msg.get("yes_ask", 0)),
            no_bid=_cents_to_prob(msg.get("no_bid", 0)),
            no_ask=_cents_to_prob(msg.get("no_ask", 0)),
            volume_24h=float(msg.get("volume_24h", 0)),
            liquidity=float(msg.get("liquidity", 0)),
            last_price=_cents_to_prob(msg.get("last_price", 0)),
            ts=msg.get("ts"),
        )

    def get_price(self, ticker: str) -> Optional[PriceSnapshot]:
        """Return the latest cached price snapshot for a market, or None if unseen."""
        return self.price_cache.get(ticker)


def _cents_to_prob(value: object) -> float:
    """Convert an integer-cents price (1–99) to a probability in [0.0, 1.0]."""
    try:
        v = float(value)  # type: ignore[arg-type]
        return v / 100.0 if v >= 1.0 else v
    except (TypeError, ValueError):
        return 0.0
