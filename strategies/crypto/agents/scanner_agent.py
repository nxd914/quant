"""
Market Scanner Agent

Continuously scans Kalshi for crypto probability markets (BTC/ETH),
evaluates each using closed-form Black-Scholes pricing against live
CEX spot data, and emits TradeOpportunity objects whenever the model
probability diverges from the market price by more than MIN_EDGE.

Two concurrent loops:
  1. Periodic scan — re-prices all crypto contracts every SCAN_INTERVAL_SECONDS
  2. Signal-triggered scan — when CryptoFeedAgent fires a momentum signal,
     immediately re-evaluates the matching contracts

The execution path is deterministic: spot_to_implied_prob (N(d2)) + Kelly.
No heuristics, no learned parameters.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from core.kalshi_client import KalshiClient
from core.kelly import MIN_EDGE, capped_kelly
from ..core.models import (
    FeatureVector,
    KalshiMarket,
    Side,
    Signal,
    SignalType,
    TradeOpportunity,
)
from ..core.pricing import bracket_prob, spot_to_implied_prob, up_down_15m_prob

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 120     # re-price crypto contracts every 2 minutes
SCAN_STARTUP_DELAY_SECONDS = 15  # brief pause for feeds to warm up
SCAN_CONCURRENCY = 8            # parallel market evaluations
SCAN_LIMIT = 200                # max markets to evaluate per cycle
MIN_TIME_TO_CLOSE_MINUTES = 5   # crypto contracts are short-lived (15m, 1h)
MAX_HOURS_TO_CLOSE = 4          # skip contracts expiring beyond this horizon (latency arb needs fast convergence)
SIGNAL_SCAN_CANDIDATE_LIMIT = 400
SIGNAL_COOLDOWN_SECONDS = 5     # tighter cooldown for sub-second crypto signals
# Conservative vol floor for BTC/ETH in low-vol regimes.  Cold-start (vol=0)
# is now handled by an explicit warmup guard in _score() rather than this floor.
MIN_CRYPTO_VOL = 0.30
MAX_BRACKET_YES_PRICE = 0.30    # don't buy YES on brackets above this — inverted risk/reward
MIN_BRACKET_DISTANCE_PCT = 0.005  # skip brackets where spot is within 0.5% of bracket midpoint — model unreliable near ATM

# Trading hours window (UTC). Outside this window, scan interval slows to 10 minutes.
# Kalshi crypto contracts are most active during US market hours.
TRADING_START_HOUR_UTC = 8    # 8 AM UTC = 4 AM ET
TRADING_END_HOUR_UTC = 1      # 1 AM UTC (next day) = 9 PM ET
IDLE_SCAN_INTERVAL_SECONDS = 600  # 10 minutes between scans outside trading hours
MIN_LIVE_LIQUIDITY_USD = 2500.0  # live orders are ~$2,500; skip markets too thin to absorb them

# Kalshi series tickers to fetch directly (not discoverable via /events)
CRYPTO_SERIES = ("KXBTC", "KXETH", "KXBTC15M", "KXETH15M", "KXSOL", "KXXRP")

# Crypto symbol -> Kalshi keyword matching
# Use full words only — short abbreviations like "btc"/"eth" are substrings of
# unrelated tickers (e.g. "releasethelastofus" contains "eth").
CRYPTO_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "kxbtc"),
    "ETH": ("ethereum", "kxeth"),
    "SOL": ("solana", "kxsol"),
    "XRP": ("ripple", "kxxrp"),
}

# Regex for 15-min Up/Down market detection (no strike suffix — directional)
_TICKER_15M_RE = re.compile(r"^KX(BTC|ETH)15M-", re.IGNORECASE)

# Regex patterns for strike extraction from ticker
# -T = threshold "above" (YES = spot > strike) or "below" (YES = spot < strike)
# -B = "between" bracket (YES = spot in range) — NOT tradeable with N(d2) model
_TICKER_THRESHOLD_RE = re.compile(r"-T(\d+(?:\.\d+)?)$")
_TICKER_BRACKET_RE = re.compile(r"-B(\d+(?:\.\d+)?)$")
_TITLE_STRIKE_RE = re.compile(r"\$([0-9,]+(?:\.\d+)?)")


class ScannerAgent:
    """
    Autonomous Kalshi crypto market scanner.

    Runs two concurrent loops:
      1. Periodic scan — fetches crypto markets and prices them against spot
      2. Signal-triggered scan — when a Signal arrives, re-evaluates matching markets

    Both paths score opportunities identically and emit to opportunity_queue.
    """

    def __init__(
        self,
        opportunity_queue: asyncio.Queue[TradeOpportunity],
        bankroll_usdc: float,
        signal_queue: Optional[asyncio.Queue[Signal]] = None,
        price_cache: Optional[dict] = None,
        crypto_features: Optional[dict] = None,
        scan_limit: int = SCAN_LIMIT,
        min_edge: float = MIN_EDGE,
    ) -> None:
        self._opportunities = opportunity_queue
        self._bankroll = bankroll_usdc
        self._signals = signal_queue
        self._scan_limit = scan_limit
        self._min_edge = min_edge
        self._price_cache = price_cache or {}
        self._crypto_features = crypto_features or {}
        self._client = KalshiClient()
        self._scan_lock: Optional[asyncio.Lock] = None
        self._market_cache: list[KalshiMarket] = []
        self._market_cache_ts: float = 0.0
        # Last-known spot/vol per symbol, updated from every signal that passes through.
        # Gives the periodic scan a reliable fallback when _crypto_features has a stale
        # or zero-price entry (e.g. a bad tick from Binance.US/Coinbase slipped through).
        self._spot_cache: dict[str, tuple[float, float]] = {}  # symbol -> (spot, vol)
        self._last_scan_ts: Optional[datetime] = None

    def set_bankroll(self, usdc: float) -> None:
        """Update bankroll from live account balance. Used by daemon's refresher."""
        if usdc > 0:
            self._bankroll = usdc

    @property
    def last_scan_ts(self) -> Optional[datetime]:
        return self._last_scan_ts

    async def run(self) -> None:
        """Start scanning. Runs until cancelled."""
        self._scan_lock = asyncio.Lock()
        await self._client.open()
        try:
            tasks = [asyncio.create_task(self._periodic_scan(), name="periodic_scan")]
            if self._signals is not None:
                tasks.append(
                    asyncio.create_task(self._signal_scan(), name="signal_scan")
                )
            await asyncio.gather(*tasks)
        finally:
            await self._client.close()

    # ------------------------------------------------------------------
    # Scan loops
    # ------------------------------------------------------------------

    async def _get_cached_markets(self, *, force_refresh: bool = False) -> list[KalshiMarket]:
        """Return cached market list, refreshing if stale or forced."""
        now = asyncio.get_event_loop().time()
        age = now - self._market_cache_ts
        if force_refresh or not self._market_cache or age >= SCAN_INTERVAL_SECONDS:
            async with self._scan_lock:  # type: ignore[union-attr]
                # Re-check inside lock to avoid double-fetch
                now = asyncio.get_event_loop().time()
                if force_refresh or not self._market_cache or (now - self._market_cache_ts) >= SCAN_INTERVAL_SECONDS:
                    markets = await self._client.get_top_markets(
                        limit=max(self._scan_limit, SIGNAL_SCAN_CANDIDATE_LIMIT),
                        min_volume_24h=0.0,
                        min_liquidity=0.0,
                    )
                    # KXBTC/KXETH series don't surface via /events discovery —
                    # fetch them directly by series ticker and merge in.
                    crypto_markets = await self._fetch_crypto_series_markets()
                    existing = {m.ticker for m in markets}
                    markets = markets + [m for m in crypto_markets if m.ticker not in existing]
                    self._market_cache = markets
                    self._market_cache_ts = asyncio.get_event_loop().time()
                    logger.info(
                        "Scanner: refreshed market cache (%d markets, %d direct crypto)",
                        len(self._market_cache),
                        len(crypto_markets),
                    )
        return self._market_cache

    async def _fetch_crypto_series_markets(self) -> list[KalshiMarket]:
        """Directly fetch KXBTC/KXETH markets — not discoverable via /events."""
        result: list[KalshiMarket] = []
        for series in CRYPTO_SERIES:
            try:
                # Fetch all contracts (intraday + daily) so near-expiry ones aren't missed
                markets = await self._client.get_markets_by_series(series, limit=200)
                result.extend(markets)
                logger.info("Scanner: fetched %d markets from %s series", len(markets), series)
            except Exception as exc:
                logger.warning("Scanner: failed to fetch %s series: %s", series, exc)
        return result

    async def _periodic_scan(self) -> None:
        """Fetch crypto markets and price them against live spot data."""
        logger.info("Scanner: waiting %ds for feeds to warm up...", SCAN_STARTUP_DELAY_SECONDS)
        await asyncio.sleep(SCAN_STARTUP_DELAY_SECONDS)
        while True:
            if not _is_trading_hours():
                logger.info("Scanner: outside trading hours, sleeping %ds", IDLE_SCAN_INTERVAL_SECONDS)
                await asyncio.sleep(IDLE_SCAN_INTERVAL_SECONDS)
                continue
            try:
                self._last_scan_ts = datetime.now(tz=timezone.utc)
                markets = await self._get_cached_markets(force_refresh=True)
                crypto_markets = [m for m in markets if _is_crypto_market(m)]
                logger.info(
                    "Scanner: fetched %d markets, %d crypto, evaluating...",
                    len(markets), len(crypto_markets),
                )
                await self._evaluate_batch(crypto_markets, signal=None)
            except Exception as exc:
                logger.warning("Scanner periodic scan error: %s", exc)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _signal_scan(self) -> None:
        """When a crypto signal fires, re-evaluate matching contracts from cache.

        Drains burst signals and groups by symbol so that simultaneous BTC + ETH
        signals are both processed (keeping only the latest per symbol).
        """
        assert self._signals is not None
        _last_scan: float = asyncio.get_event_loop().time()
        while True:
            first_signal = await self._signals.get()
            # Drain burst signals, keeping the latest per symbol
            latest_by_symbol: dict[str, Signal] = {
                first_signal.symbol.upper(): first_signal,
            }
            while not self._signals.empty():
                try:
                    sig = self._signals.get_nowait()
                    latest_by_symbol[sig.symbol.upper()] = sig
                except Exception:
                    break

            # Update spot cache for every symbol seen in this burst
            for sym, sig in latest_by_symbol.items():
                if sig.features.spot_price > 0:
                    self._spot_cache[sym] = (
                        sig.features.spot_price,
                        sig.features.realized_vol_long,
                    )

            # Outside trading hours: spot cache updated above, skip evaluation
            if not _is_trading_hours():
                continue

            # Enforce cooldown
            now = asyncio.get_event_loop().time()
            wait = SIGNAL_COOLDOWN_SECONDS - (now - _last_scan)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_scan = asyncio.get_event_loop().time()

            # Process each symbol's latest signal
            for sym, signal in latest_by_symbol.items():
                try:
                    markets = await self._get_cached_markets()
                    filtered = [
                        m for m in markets
                        if market_matches_crypto_signal(m, signal)
                    ]
                    logger.info(
                        "Scanner: signal %s symbol=%s matched %d / %d Kalshi markets",
                        signal.signal_type.value,
                        signal.symbol,
                        len(filtered),
                        len(markets),
                    )
                    if filtered:
                        await self._evaluate_batch(filtered, signal=signal)
                except Exception as exc:
                    logger.warning("Scanner signal scan error (%s): %s", sym, exc)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def _evaluate_batch(
        self,
        markets: list[KalshiMarket],
        signal: Optional[Signal],
    ) -> None:
        """Evaluate a list of markets concurrently."""
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def evaluate(market: KalshiMarket) -> None:
            async with sem:
                opp = self._score(market, signal)
                if opp is not None:
                    await self._opportunities.put(opp)

        await asyncio.gather(*[evaluate(m) for m in markets], return_exceptions=True)

    def _score(
        self,
        market: KalshiMarket,
        signal: Optional[Signal],
    ) -> Optional[TradeOpportunity]:
        """
        Evaluate one market using closed-form Black-Scholes pricing.

        Threshold contracts: model_prob = N(d2) via spot_to_implied_prob.
        Bracket contracts:   model_prob = N(d2_floor) - N(d2_cap) via bracket_prob.
        Emits TradeOpportunity if edge exceeds MIN_EDGE threshold.
        """
        is_bracket = _is_bracket_market(market)
        is_up_down = _is_up_down_15m_market(market)

        if not _has_enough_time(market.close_time):
            logger.debug("SCORE skip time: %s | close=%s", market.ticker, market.close_time)
            return None

        hours_to_close = _hours_until(market.close_time)
        # 15M markets have a much shorter max horizon than bracket/threshold
        max_hours = 0.5 if is_up_down else MAX_HOURS_TO_CLOSE
        if hours_to_close > max_hours:
            logger.debug(
                "SCORE skip too_far_out: %s | hours=%.2f > %.1f",
                market.ticker, hours_to_close, max_hours,
            )
            return None

        # Apply real-time Kalshi WS price override (immutable update)
        market = self._apply_price_cache(market)

        # Get spot price and realized vol
        spot_price, realized_vol = self._get_spot_data(market, signal)
        if spot_price <= 0:
            logger.info("SCORE skip no_spot: %s | symbol=%s", market.ticker, _market_symbol(market))
            return None

        # Reuse hours_to_close computed above
        hours_to_expiry = hours_to_close
        if hours_to_expiry <= 0:
            return None

        # Skip if vol data isn't warmed up yet (prevents trading with fake vol)
        if realized_vol <= 0.0:
            logger.info("SCORE skip vol_warmup: %s", market.ticker)
            return None
        vol = max(realized_vol, MIN_CRYPTO_VOL)

        if is_up_down:
            # 15-min directional: YES = price higher at expiry than current spot.
            # Model is ~0.50 without drift; edge comes from Kalshi market drifting from 50%.
            model_prob = up_down_15m_prob(spot_price, hours_to_expiry, vol)
            strike_repr = f"atm@{spot_price:.0f}"
        elif is_bracket:
            # Bracket: YES resolves if floor < spot < cap at expiry
            floor = market.floor_strike
            cap   = market.cap_strike
            if floor is None or cap is None:
                logger.info("SCORE skip bracket_no_strikes: %s", market.ticker)
                return None

            # ATM proximity guard — model is unreliable when spot is near bracket range
            bracket_mid = (floor + cap) / 2.0
            distance_pct = abs(spot_price - bracket_mid) / spot_price if spot_price > 0 else 0.0
            if distance_pct < MIN_BRACKET_DISTANCE_PCT:
                logger.info(
                    "SCORE skip atm_bracket: %s | spot=%.0f mid=%.0f dist=%.4f < %.4f",
                    market.ticker, spot_price, bracket_mid, distance_pct, MIN_BRACKET_DISTANCE_PCT,
                )
                return None

            model_prob = bracket_prob(spot_price, floor, cap, hours_to_expiry, vol)
            strike_repr = f"[{floor:.0f},{cap:.0f}]"
        else:
            # Threshold: YES resolves if spot > strike (or < strike for "less")
            strike = parse_strike(market)
            if strike is None:
                logger.info("SCORE skip no_strike: %s | title=%s", market.ticker, market.title[:60])
                return None
            prob_above = spot_to_implied_prob(spot_price, strike, hours_to_expiry, vol)
            model_prob = (1.0 - prob_above) if _is_less_market(market) else prob_above
            strike_repr = f"{strike:.0f}"

        # Block bracket YES bets above price cap (inverted risk/reward)
        if is_bracket and model_prob > market.implied_prob and market.yes_ask > MAX_BRACKET_YES_PRICE:
            logger.info(
                "SCORE skip bracket_yes_too_expensive: %s | yes_ask=%.2f > %.2f",
                market.ticker, market.yes_ask, MAX_BRACKET_YES_PRICE,
            )
            return None

        # Compute edge
        edge = abs(model_prob - market.implied_prob)
        if edge < self._min_edge:
            logger.info(
                "SCORE skip low_edge: %s | model=%.3f market=%.3f edge=%.3f < %.2f | spot=%.0f strike=%s",
                market.ticker, model_prob, market.implied_prob, edge, self._min_edge,
                spot_price, strike_repr,
            )
            return None

        if (
            os.environ.get("EXECUTION_MODE", "paper").lower() == "live"
            and market.liquidity < MIN_LIVE_LIQUIDITY_USD
        ):
            logger.info(
                "SCORE skip thin_book: %s | liquidity=%.0f < %.0f (live mode)",
                market.ticker, market.liquidity, MIN_LIVE_LIQUIDITY_USD,
            )
            return None

        side = Side.YES if model_prob > market.implied_prob else Side.NO
        market_price = market.yes_ask if side == Side.YES else market.no_ask

        kelly_f = capped_kelly(model_prob, market_price)
        if kelly_f <= 0:
            return None

        effective_signal = signal or _synthetic_signal(market, model_prob, spot_price)

        return TradeOpportunity(
            signal=effective_signal,
            market=market,
            side=side,
            model_prob=model_prob,
            market_prob=market.implied_prob,
            edge=edge,
            kelly_fraction=kelly_f / 0.25,
            capped_fraction=kelly_f,
        )

    def _apply_price_cache(self, market: KalshiMarket) -> KalshiMarket:
        """Apply real-time Kalshi WS prices to market snapshot (immutable)."""
        cache = self._price_cache.get(market.ticker)
        if not cache:
            return market

        yes_bid = cache.get("yes_bid", market.yes_bid)
        yes_ask = cache.get("yes_ask", market.yes_ask)
        no_bid = cache.get("no_bid", market.no_bid)
        no_ask = cache.get("no_ask", market.no_ask)

        if yes_bid > 0 and yes_ask > 0:
            implied_prob = (yes_bid + yes_ask) / 2.0
            spread_pct = (yes_ask - yes_bid) / implied_prob if implied_prob > 0 else 0.0
        elif yes_ask > 0:
            implied_prob = yes_ask
            spread_pct = 0.0
        elif yes_bid > 0:
            implied_prob = yes_bid
            spread_pct = 0.0
        else:
            return market

        return replace(
            market,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            implied_prob=implied_prob,
            spread_pct=spread_pct,
            volume_24h=cache.get("volume_24h", market.volume_24h),
            liquidity=cache.get("liquidity", market.liquidity),
        )

    def _get_spot_data(
        self,
        market: KalshiMarket,
        signal: Optional[Signal],
    ) -> tuple[float, float]:
        """Resolve current spot price and realized vol for a market's symbol."""
        symbol = _market_symbol(market)
        if symbol is None:
            return (0.0, 0.0)

        # Prefer signal's features (freshest data, from the tick that triggered)
        # Use realized_vol_long (15-min window) for pricing — more stable for 1-4h contracts
        if signal is not None and signal.features.spot_price > 0:
            return (signal.features.spot_price, signal.features.realized_vol_long)

        # Fall back to FeatureAgent's latest features
        features = self._crypto_features.get(symbol)
        if features is not None and features.spot_price > 0:
            return (features.spot_price, features.realized_vol_long)

        # Last resort: spot cached from the most recent signal for this symbol.
        # Guards against _crypto_features having a zero-price entry from a bad tick.
        cached = self._spot_cache.get(symbol)
        if cached:
            return cached

        return (0.0, 0.0)


# ------------------------------------------------------------------
# Matching & parsing helpers
# ------------------------------------------------------------------

def market_matches_crypto_signal(market: KalshiMarket, signal: Signal) -> bool:
    """Match a crypto signal to a Kalshi market by symbol keywords."""
    keywords = CRYPTO_KEYWORDS.get(signal.symbol.upper(), ())
    if not keywords:
        return False
    blob = _market_text_blob(market)
    return any(kw in blob for kw in keywords)


def parse_strike(market: KalshiMarket) -> Optional[float]:
    """
    Extract strike price from a Kalshi threshold contract.

    Ticker pattern:
      KXBTC-26APR13-T67000  → 67000.0  (threshold contract: "above" or "below")
    Title fallback:
      'Will Bitcoin be above $67,000 at 4pm ET?' → 67000.0

    Bracket (-B) contracts are filtered upstream and should never reach here.
    """
    # Threshold contracts use -T suffix
    m = _TICKER_THRESHOLD_RE.search(market.ticker)
    if m:
        return float(m.group(1))

    # Fall back to title
    title_match = _TITLE_STRIKE_RE.search(market.title)
    if title_match:
        return float(title_match.group(1).replace(",", ""))

    return None


def _is_up_down_15m_market(market: KalshiMarket) -> bool:
    """Return True if this is a 15-minute Up/Down directional market (KXETH15M/KXBTC15M)."""
    return bool(_TICKER_15M_RE.match(market.ticker))


def _is_bracket_market(market: KalshiMarket) -> bool:
    """Return True if this is a bracket/range contract (strike_type 'between').

    These require different pricing math (P(floor < spot < cap)) that we
    don't support yet.  Fall back to ticker suffix if strike_type is empty.
    """
    if market.strike_type:
        return market.strike_type == "between"
    # Fallback: -B suffix historically meant bracket on Kalshi crypto series
    return bool(_TICKER_BRACKET_RE.search(market.ticker))


def _is_less_market(market: KalshiMarket) -> bool:
    """Return True if this is a 'spot below strike' threshold contract.

    YES resolves when spot < strike, so model_prob = 1 - P(spot > strike).
    """
    return market.strike_type == "less"


def _is_crypto_market(market: KalshiMarket) -> bool:
    """Return True if this market is a crypto price contract."""
    blob = _market_text_blob(market)
    return any(
        kw in blob
        for keywords in CRYPTO_KEYWORDS.values()
        for kw in keywords
    )


def _market_symbol(market: KalshiMarket) -> Optional[str]:
    """Extract the normalized crypto symbol (BTC/ETH) from a market."""
    blob = _market_text_blob(market)
    for symbol, keywords in CRYPTO_KEYWORDS.items():
        if any(kw in blob for kw in keywords):
            return symbol
    return None


def _market_text_blob(market: KalshiMarket) -> str:
    return f"{market.ticker} {market.title} {market.event_ticker}".casefold()


def _is_trading_hours() -> bool:
    """Return True if current UTC hour is within the active trading window.

    Window wraps midnight: TRADING_START_HOUR_UTC=8 to TRADING_END_HOUR_UTC=1
    means 08:00-23:59 and 00:00-00:59 UTC are active.
    """
    hour = datetime.now(tz=timezone.utc).hour
    if TRADING_START_HOUR_UTC <= TRADING_END_HOUR_UTC:
        return TRADING_START_HOUR_UTC <= hour < TRADING_END_HOUR_UTC
    # Wraps midnight: e.g. start=8, end=1 means 8-23 OR 0
    return hour >= TRADING_START_HOUR_UTC or hour < TRADING_END_HOUR_UTC


def _has_enough_time(close_time: str) -> bool:
    """Returns True if market has at least MIN_TIME_TO_CLOSE_MINUTES remaining."""
    if not close_time:
        return True
    try:
        expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        minutes_left = (expiry - datetime.now(tz=timezone.utc)).total_seconds() / 60
        return minutes_left >= MIN_TIME_TO_CLOSE_MINUTES
    except (ValueError, TypeError):
        return True


def _hours_until(close_time: str) -> float:
    """Hours from now until market close. Returns 0.0 if unparseable."""
    if not close_time:
        return 0.0
    try:
        expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        delta = (expiry - datetime.now(tz=timezone.utc)).total_seconds()
        return max(0.0, delta / 3600.0)
    except (ValueError, TypeError):
        return 0.0


def _synthetic_signal(
    market: KalshiMarket,
    model_prob: float,
    spot_price: float,
) -> Signal:
    """Construct a synthetic Signal for periodic scan opportunities."""
    direction = (
        SignalType.MOMENTUM_UP
        if model_prob > market.implied_prob
        else SignalType.MOMENTUM_DOWN
    )
    symbol = _market_symbol(market) or market.event_ticker or market.ticker
    # Use current time — market.timestamp can be up to SCAN_INTERVAL_SECONDS old,
    # which would cause the risk agent's MAX_SIGNAL_AGE_SECONDS gate to reject all
    # periodic-scan opportunities as stale signals.
    now = datetime.now(timezone.utc)
    fv = FeatureVector(
        symbol=symbol,
        timestamp=now,
        spot_price=spot_price,
        short_return=0.0,
        realized_vol=0.0,
        jump_detected=False,
        momentum_z=0.0,
    )
    return Signal(
        signal_type=direction,
        symbol=symbol,
        timestamp=now,
        features=fv,
        implied_prob_shift=abs(model_prob - market.implied_prob),
        confidence=0.5,
    )
