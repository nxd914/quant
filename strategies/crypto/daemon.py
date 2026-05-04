"""
Main orchestrator — wires all agents together and runs the event loop.

Usage:
  EXECUTION_MODE=paper python3 -m strategies.crypto.daemon

Environment variables:
  EXECUTION_MODE    paper (default) | live
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable

from core.environment import log_environment_banner, resolve_environment
from core.kalshi_client import KalshiClient
from .core.logging import configure_logging


def _load_project_dotenv() -> None:
    """Load `.env` from repo root so the daemon sees API keys without manual export."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    load_dotenv(override=False)


_load_project_dotenv()

from .agents import (  # noqa: E402
    CryptoFeedAgent,
    ExecutionAgent,
    FeatureAgent,
    ResolutionAgent,
    RiskAgent,
    ScannerAgent,
    WebsocketAgent,
)
from .core.config import Config  # noqa: E402
from .core.models import Signal, Tick, TradeOpportunity  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

TRACKED_SYMBOLS: list[str] = os.environ.get("TRACKED_SYMBOLS", "BTC,ETH").split(",")
ORDER_GROUP_LIMIT = int(os.environ.get("ORDER_GROUP_CONTRACTS_LIMIT", "300"))
_FALLBACK_BANKROLL = 1000.0  # used only if balance fetch fails at startup
_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_PID_PATH = Path(__file__).resolve().parents[2] / "data" / "paper_fund.pid"
_WATCHDOG_CHECK_SECONDS = 300    # check scanner health every 5 min
_SCAN_STALE_THRESHOLD_SECONDS = 1800  # alert if no scan in 30 min during trading hours
_TRADING_START_UTC = 8
_TRADING_END_UTC = 1
_BANKROLL_REFRESH_SECONDS = 60


def _is_trading_hours() -> bool:
    hour = datetime.now(tz=timezone.utc).hour
    if _TRADING_START_UTC <= _TRADING_END_UTC:
        return _TRADING_START_UTC <= hour < _TRADING_END_UTC
    return hour >= _TRADING_START_UTC or hour < _TRADING_END_UTC


async def _guarded(coro: Awaitable, name: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Agent %s crashed: %s", name, exc, exc_info=True)
        raise


async def _bankroll_refresher(
    risk: RiskAgent,
    scanner: ScannerAgent,
    api_key: str,
    private_key_path: str,
    base_url: str,
) -> None:
    """Poll Kalshi for the live account balance every minute and update RiskAgent."""
    client = KalshiClient(
        api_key=api_key,
        private_key_path=private_key_path,
        base_url=base_url,
    )
    await client.open()
    try:
        while True:
            try:
                balance = await client.get_balance()
                risk.set_bankroll(balance)
                scanner.set_bankroll(balance)
            except Exception as exc:
                logger.warning("Bankroll refresh failed: %s", exc)
            await asyncio.sleep(_BANKROLL_REFRESH_SECONDS)
    finally:
        await client.close()


async def _watchdog(scanner: ScannerAgent) -> None:
    await asyncio.sleep(120)  # let scanner warm up before first check
    while True:
        await asyncio.sleep(_WATCHDOG_CHECK_SECONDS)
        if not _is_trading_hours():
            continue
        last_ts = scanner.last_scan_ts
        if last_ts is None:
            logger.warning("[kinzie] Scanner has not completed a scan since startup")
            continue
        age = (datetime.now(tz=timezone.utc) - last_ts).total_seconds()
        if age > _SCAN_STALE_THRESHOLD_SECONDS:
            logger.warning(
                "[kinzie] No scanner activity for %.0f min during trading hours", age / 60
            )


async def main() -> None:
    config = Config.from_env()
    env = resolve_environment()
    log_environment_banner(env)

    _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PID_PATH.write_text(str(os.getpid()))

    # Queues
    tick_queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=5000)
    signal_queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=200)
    scanner_out_queue: asyncio.Queue[TradeOpportunity] = asyncio.Queue(maxsize=100)
    approved_queue: asyncio.Queue[tuple[TradeOpportunity, float]] = asyncio.Queue(maxsize=50)

    # Bootstrap: fetch live balance + create order group before constructing agents.
    # Balance is always pulled from the account — no static config value.
    order_group_id: str | None = None
    initial_bankroll = _FALLBACK_BANKROLL
    try:
        async with KalshiClient(
            api_key=env.api_key,
            private_key_path=env.private_key_path,
            base_url=env.rest_base_url,
        ) as bootstrap_client:
            try:
                initial_bankroll = await bootstrap_client.get_balance()
            except Exception as exc:
                logger.warning("Balance fetch failed at startup: %s — using fallback $%.2f", exc, _FALLBACK_BANKROLL)
            order_group_id = await bootstrap_client.create_order_group(ORDER_GROUP_LIMIT)
        if order_group_id:
            logger.info(
                "Order group created: id=%s | rolling 15s cap=%d contracts",
                order_group_id,
                ORDER_GROUP_LIMIT,
            )
        else:
            logger.warning("Order group creation returned no id — orders will not be group-bound.")
    except Exception as exc:
        logger.warning("Order group creation failed: %s — proceeding without group binding.", exc)

    logger.info(
        "Starting | mode=%s | bankroll=%.2f USDC | min_edge=%.2f | max_positions=%d",
        env.label,
        initial_bankroll,
        config.min_edge,
        config.max_concurrent_positions,
    )

    # Agents
    crypto_feed = CryptoFeedAgent(tick_queue=tick_queue, symbols=TRACKED_SYMBOLS)
    feature_agent = FeatureAgent(tick_queue=tick_queue, signal_queue=signal_queue)

    ws_agent = WebsocketAgent(
        api_key=env.api_key,
        private_key_path=env.private_key_path,
        ws_url=env.ws_base_url,
    )
    scanner = ScannerAgent(
        signal_queue=signal_queue,
        opportunity_queue=scanner_out_queue,
        bankroll_usdc=initial_bankroll,
        price_cache=ws_agent.price_cache,
        crypto_features=feature_agent.latest_features,
        min_edge=config.min_edge,
    )
    risk = RiskAgent(
        opportunity_queue=scanner_out_queue,
        approved_queue=approved_queue,
        bankroll_usdc=initial_bankroll,
        config=config,
    )

    execution = ExecutionAgent(
        approved_queue=approved_queue,
        risk_agent=risk,
        environment=env,
    )
    if order_group_id:
        execution._order_group_id = order_group_id  # type: ignore[attr-defined]
    resolver = ResolutionAgent(risk_agent=risk)

    tasks = [
        asyncio.create_task(_guarded(crypto_feed.run(), "crypto_feed"), name="crypto_feed"),
        asyncio.create_task(_guarded(feature_agent.run(), "features"), name="features"),
        asyncio.create_task(_guarded(ws_agent.run(), "kalshi_ws"), name="kalshi_ws"),
        asyncio.create_task(_guarded(scanner.run(), "scanner"), name="scanner"),
        asyncio.create_task(_guarded(risk.run(), "risk"), name="risk"),
        asyncio.create_task(_guarded(execution.run(), "execution"), name="execution"),
        asyncio.create_task(_guarded(resolver.run(), "resolver"), name="resolver"),
        asyncio.create_task(_watchdog(scanner), name="watchdog"),
    ]

    tasks.append(
        asyncio.create_task(
            _guarded(
                _bankroll_refresher(
                    risk=risk,
                    scanner=scanner,
                    api_key=env.api_key,
                    private_key_path=env.private_key_path,
                    base_url=env.rest_base_url,
                ),
                "bankroll_refresher",
            ),
            name="bankroll_refresher",
        )
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in tasks])

    logger.info("All agents running (%d tasks). Press Ctrl+C to stop.", len(tasks))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown signal received — stopping agents.")
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            still_running = [t.get_name() for t in tasks if not t.done()]
            logger.warning("Shutdown timed out after %.0fs — tasks still running: %s", _SHUTDOWN_TIMEOUT_SECONDS, still_running)
    finally:
        _PID_PATH.unlink(missing_ok=True)
        logger.info("[kinzie] Daemon stopped")


if __name__ == "__main__":
    asyncio.run(main())
