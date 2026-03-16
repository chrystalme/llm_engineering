"""
yfinance adapter — primary source for stocks, indices, and commodities.

Yahoo Finance blocks plain yfinance requests from cloud IP ranges.
We work around this by injecting a browser-like User-Agent and Accept headers
into a custom requests.Session, which yfinance passes through to Yahoo.

Symbol mapping (ARIA → yfinance ticker):
  Commodities : XAU/USD → GC=F  | XAG/USD → SI=F | WTI → CL=F | Copper → HG=F
  Indices     : SPX → ^GSPC     | NDX → ^NDX      | DJI → ^DJI
  Stocks      : AAPL, NVDA, MSFT, TSLA, META (passed through unchanged)
  Crypto      : BTC/USD → BTC-USD | ETH/USD → ETH-USD (CoinGecko preferred)
"""
from __future__ import annotations
import logging

from models import AssetData

logger = logging.getLogger("aria.data_fetcher")

_TICKER_MAP: dict[str, str] = {
    # Commodities
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "WTI":     "CL=F",
    "Copper":  "HG=F",
    # Indices
    "SPX":     "^GSPC",
    "NDX":     "^NDX",
    "DJI":     "^DJI",
    # Crypto (fallback — CoinGecko preferred)
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
}

# Browser-like headers to avoid Yahoo Finance IP blocking on cloud containers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _make_session():
    import requests
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _to_ticker(symbol: str) -> str:
    return _TICKER_MAP.get(symbol, symbol)


def fetch_yfinance(symbol: str) -> AssetData | None:
    """Fetch price + 60 days of daily OHLCV for *symbol* via yfinance."""
    try:
        import yfinance as yf

        ticker = _to_ticker(symbol)
        session = _make_session()
        t = yf.Ticker(ticker, session=session)

        hist = t.history(period="60d", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            logger.debug("%s: yfinance returned empty history for ticker %s", symbol, ticker)
            return None

        price = float(hist["Close"].iloc[-1])
        if price <= 0:
            return None

        if len(hist) >= 2:
            prev_close = float(hist["Close"].iloc[-2])
            change_val = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
            change_24h = f"{change_val:+.2f}%"
        else:
            change_24h = "0%"

        rows = hist.tail(50).iloc[::-1]
        ohlcv_50 = []
        for ts, row in rows.iterrows():
            ohlcv_50.append({
                "t": str(ts.date()),
                "o": round(float(row["Open"]),  6),
                "h": round(float(row["High"]),  6),
                "l": round(float(row["Low"]),   6),
                "c": round(float(row["Close"]), 6),
                "v": int(row.get("Volume", 0) or 0),
            })
        ohlcv_14 = ohlcv_50[:14]

        vols = [x["v"] for x in ohlcv_50[:20] if x["v"] > 0]
        vol_avg = sum(vols) / len(vols) if vols else 1
        last_vol = ohlcv_50[0]["v"] if ohlcv_50 else 1
        volume_ratio = round(last_vol / vol_avg, 2) if vol_avg else 1.0

        highs = [x["h"] for x in ohlcv_50]
        lows  = [x["l"] for x in ohlcv_50]

        return AssetData(
            asset=symbol,
            price=round(price, 6),
            change_1h="",
            change_24h=change_24h,
            volume_ratio=volume_ratio,
            ohlcv_14=ohlcv_14,
            ohlcv_50=ohlcv_50,
            high_52w=round(max(highs), 6) if highs else price,
            low_52w=round(min(lows),  6) if lows  else price,
        )
    except Exception as e:
        # Log the actual error so we can see if it's a 429, SSL, or import issue
        logger.debug("%s: yfinance error — %s: %s", symbol, type(e).__name__, e)
        return None