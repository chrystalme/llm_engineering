"""
Forex source — all pairs via open.er-api.com (free, no key, cloud-safe).

open.er-api.com provides:
  - Latest rates (no key, no rate limit, works on cloud IPs)
  - Historical daily rates via /v6/history/{base}/{year}/{month}/{day}

Supported pairs: EUR/USD, USD/JPY, USD/NGN, USD/CNY, GBP/USD
"""
from __future__ import annotations
import logging
from datetime import date, timedelta

import requests

from models import AssetData

logger = logging.getLogger("aria.data_fetcher")

# All pairs and their (base, quote) for open.er-api
_PAIRS: dict[str, tuple[str, str]] = {
    "EUR/USD": ("EUR", "USD"),
    "USD/JPY": ("USD", "JPY"),
    "USD/NGN": ("USD", "NGN"),
    "USD/CNY": ("USD", "CNY"),
    "GBP/USD": ("GBP", "USD"),
}

SUPPORTED_PAIRS = set(_PAIRS)

_BASE_URL = "https://open.er-api.com/v6"
_TIMEOUT  = 10


def _get_latest(base: str, quote: str) -> float | None:
    """Fetch latest rate for base→quote."""
    try:
        r = requests.get(f"{_BASE_URL}/latest/{base}", timeout=_TIMEOUT)
        r.raise_for_status()
        return float(r.json()["rates"][quote])
    except Exception as e:
        logger.debug("open.er-api latest %s/%s failed: %s", base, quote, e)
        return None


def _get_historical(base: str, quote: str, days: int = 60) -> list[tuple[str, float]]:
    """
    Fetch up to *days* daily closing rates by iterating backwards from today.
    open.er-api history endpoint: GET /v6/history/{base}/{year}/{month}/{day}
    Returns list of (date_str, rate) sorted newest-first.
    Stops early if two consecutive days fail (weekend / holiday tolerance built in).
    """
    results: list[tuple[str, float]] = []
    consecutive_failures = 0
    today = date.today()

    for i in range(days + 20):  # overshoot to account for weekends
        d = today - timedelta(days=i)
        try:
            r = requests.get(
                f"{_BASE_URL}/history/{base}/{d.year}/{d.month:02d}/{d.day:02d}",
                timeout=_TIMEOUT,
            )
            if r.status_code == 404:
                # No data for this day (weekend / holiday) — skip silently
                consecutive_failures += 1
                if consecutive_failures > 5:
                    break
                continue
            r.raise_for_status()
            rate = float(r.json()["rates"][quote])
            results.append((d.isoformat(), rate))
            consecutive_failures = 0
            if len(results) >= days:
                break
        except Exception as e:
            logger.debug("open.er-api history %s/%s %s failed: %s", base, quote, d, e)
            consecutive_failures += 1
            if consecutive_failures > 5:
                break

    return results  # newest-first


def fetch_forex(symbol: str) -> AssetData | None:
    """Fetch a forex pair using open.er-api — works on Modal / cloud IPs."""
    if symbol not in _PAIRS:
        return None

    base, quote = _PAIRS[symbol]

    # Try to get historical data first (gives proper OHLCV for tech analysis)
    history = _get_historical(base, quote, days=55)

    if history:
        # history is newest-first; keep top 50
        history = history[:50]
        price = history[0][1]
        change_val = ((history[0][1] - history[1][1]) / history[1][1]) * 100 if len(history) >= 2 else 0.0
        change_24h = f"{change_val:+.2f}%"

        # Build OHLCV — daily close-only (o=h=l=c), no volume in forex
        ohlcv_50 = [
            {"t": d, "o": round(r, 6), "h": round(r, 6),
             "l": round(r, 6), "c": round(r, 6), "v": 0}
            for d, r in history
        ]
        ohlcv_14 = ohlcv_50[:14]
        closes = [x["c"] for x in ohlcv_50]

        return AssetData(
            asset=symbol,
            price=round(price, 6),
            change_1h="",
            change_24h=change_24h,
            volume_ratio=1.0,
            ohlcv_14=ohlcv_14,
            ohlcv_50=ohlcv_50,
            high_52w=round(max(closes), 6),
            low_52w=round(min(closes),  6),
        )

    # History failed — fall back to latest rate only
    logger.debug("%s: historical fetch failed, falling back to latest rate", symbol)
    price = _get_latest(base, quote)
    if not price:
        return None

    flat = {"t": date.today().isoformat(), "o": price, "h": price, "l": price, "c": price, "v": 0}
    return AssetData(
        asset=symbol,
        price=round(price, 6),
        change_1h="",
        change_24h="",
        volume_ratio=1.0,
        ohlcv_14=[flat] * 14,
        ohlcv_50=[flat] * 50,
        high_52w=price,
        low_52w=price,
    )