"""
Stooq adapter — cloud-safe OHLCV source for stocks, indices, and commodities.

Stooq (stooq.com) serves historical daily OHLCV data via a simple CSV endpoint
that is NOT blocked on cloud IPs (unlike Yahoo Finance). No API key required.

Accessed via pandas_datareader which handles the CSV parsing.

Symbol mapping (ARIA → Stooq ticker):
  Stocks      : AAPL, NVDA, MSFT, TSLA, META → aapl.us, nvda.us, etc.
  Indices     : SPX → ^spx  | NDX → ^ndx  | DJI → ^dji
  Commodities : WTI → cl.f  | Copper → hg.f | XAU/USD → gc.f | XAG/USD → si.f
"""
from __future__ import annotations
import logging
from datetime import date, timedelta

from models import AssetData

logger = logging.getLogger("aria.data_fetcher")

# Map ARIA symbols → Stooq tickers
_TICKER_MAP: dict[str, str] = {
    # Stocks (US exchange suffix required)
    "AAPL":    "aapl.us",
    "NVDA":    "nvda.us",
    "MSFT":    "msft.us",
    "TSLA":    "tsla.us",
    "META":    "meta.us",
    # Indices
    "SPX":     "^spx",
    "NDX":     "^ndx",
    "DJI":     "^dji",
    # Commodities (futures)
    "WTI":     "cl.f",
    "Copper":  "hg.f",
    "XAU/USD": "gc.f",
    "XAG/USD": "si.f",
}


def _to_ticker(symbol: str) -> str | None:
    return _TICKER_MAP.get(symbol)


def fetch_stooq(symbol: str) -> AssetData | None:
    """Fetch 60 days of daily OHLCV from Stooq via pandas_datareader."""
    ticker = _to_ticker(symbol)
    if not ticker:
        return None
    try:
        import pandas_datareader.data as web
        import pandas as pd

        end   = date.today()
        start = end - timedelta(days=90)  # extra buffer for weekends / holidays

        df = web.DataReader(ticker, "stooq", start=start, end=end)
        if df is None or df.empty:
            logger.debug("%s: stooq returned empty data for ticker %s", symbol, ticker)
            return None

        # Stooq returns newest-first; sort descending explicitly
        df = df.sort_index(ascending=False)
        df = df.head(50)

        price = float(df["Close"].iloc[0])
        if price <= 0:
            return None

        if len(df) >= 2:
            prev = float(df["Close"].iloc[1])
            change_val = ((price - prev) / prev) * 100 if prev else 0.0
            change_24h = f"{change_val:+.2f}%"
        else:
            change_24h = "0%"

        ohlcv_50 = []
        for ts, row in df.iterrows():
            ohlcv_50.append({
                "t": str(ts.date()) if hasattr(ts, "date") else str(ts),
                "o": round(float(row["Open"]),  6),
                "h": round(float(row["High"]),  6),
                "l": round(float(row["Low"]),   6),
                "c": round(float(row["Close"]), 6),
                "v": int(row.get("Volume", 0) or 0),
            })
        ohlcv_14 = ohlcv_50[:14]

        vols = [x["v"] for x in ohlcv_50[:20] if x["v"] > 0]
        vol_avg   = sum(vols) / len(vols) if vols else 1
        last_vol  = ohlcv_50[0]["v"] if ohlcv_50 else 1
        vol_ratio = round(last_vol / vol_avg, 2) if vol_avg else 1.0

        highs = [x["h"] for x in ohlcv_50]
        lows  = [x["l"] for x in ohlcv_50]

        return AssetData(
            asset=symbol,
            price=round(price, 6),
            change_1h="",
            change_24h=change_24h,
            volume_ratio=vol_ratio,
            ohlcv_14=ohlcv_14,
            ohlcv_50=ohlcv_50,
            high_52w=round(max(highs), 6) if highs else price,
            low_52w=round(min(lows),  6) if lows  else price,
        )
    except Exception as e:
        logger.debug("%s: stooq error — %s: %s", symbol, type(e).__name__, e)
        return None