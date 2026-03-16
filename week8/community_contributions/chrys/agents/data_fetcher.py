"""DATA_FETCHER agent: retrieves live and historical price data.

Fetch priority per asset type:

  Crypto      CoinGecko → yfinance fallback
  Metals      CommodityPriceAPI spot + Stooq OHLCV enrichment → Stooq only
  Forex       open.er-api.com  (free, no key, proven cloud-safe on Modal)
  Stocks      Stooq → Alpha Vantage fallback (if key set)
  Indices     Stooq  (^spx, ^ndx, ^dji)
  Commodities Stooq  (cl.f for WTI, hg.f for Copper)

Why Stooq instead of yfinance for cloud runs:
  Yahoo Finance blocks cloud provider IP ranges aggressively.
  Stooq serves the same daily OHLCV data and is not cloud-blocked.
  pandas_datareader handles the Stooq CSV endpoint.

Why open.er-api for forex:
  frankfurter.app is blocked on Modal. open.er-api confirmed working.
"""
import logging
import time
from typing import List

from agents.base import AgentBase
from models import AssetData

logger = logging.getLogger("aria.data_fetcher")


def _get_sources():
    from sources.alpha_vantage import fetch_alpha_vantage
    from sources.metals_api import fetch_metals
    from sources.coingecko import fetch_coingecko
    from sources.stooq_source import fetch_stooq
    from sources.forex_source import fetch_forex, SUPPORTED_PAIRS
    return fetch_alpha_vantage, fetch_metals, fetch_coingecko, fetch_stooq, fetch_forex, SUPPORTED_PAIRS


_CRYPTO  = {"BTC/USD", "ETH/USD"}
_METALS  = {"XAU/USD", "XAG/USD"}
_INDICES = {"SPX", "NDX", "DJI"}


class DataFetcherAgent(AgentBase):
    name = "DATA_FETCHER"
    logger_name = "aria.data_fetcher"

    def __init__(self, alpha_key: str = "", metals_key: str = ""):
        super().__init__()
        self.alpha_key = alpha_key
        self.metals_key = metals_key

    def fetch_one(self, symbol: str) -> AssetData | None:
        fetch_av, fetch_metals, fetch_cg, fetch_stooq, fetch_forex, forex_pairs = _get_sources()

        # --- Crypto: CoinGecko first, Stooq fallback ---
        if symbol in _CRYPTO:
            result = fetch_cg(symbol, "")
            if result:
                return result
            self.log(f"{symbol}: CoinGecko failed, trying Stooq")
            return fetch_stooq(symbol)

        # --- Metals: spot price from CommodityPriceAPI + Stooq OHLCV enrichment ---
        if symbol in _METALS:
            if self.metals_key:
                result = fetch_metals(symbol, self.metals_key)
                if result and result.ohlcv_50:
                    return result
                if result:
                    self.log(f"{symbol}: metals API price-only, enriching OHLCV via Stooq")
                    stooq = fetch_stooq(symbol)
                    if stooq and stooq.ohlcv_50:
                        return AssetData(
                            asset=result.asset,
                            price=result.price,         # accurate metals API spot price
                            change_1h=result.change_1h,
                            change_24h=stooq.change_24h,
                            volume_ratio=stooq.volume_ratio,
                            ohlcv_14=stooq.ohlcv_14,
                            ohlcv_50=stooq.ohlcv_50,
                            high_52w=stooq.high_52w,
                            low_52w=stooq.low_52w,
                        )
                    return result  # price-only, better than skipping
            # No metals key — Stooq futures (gc.f / si.f)
            self.log(f"{symbol}: no metals API key, using Stooq futures")
            return fetch_stooq(symbol)

        # --- Indices: Stooq only ---
        if symbol in _INDICES:
            result = fetch_stooq(symbol)
            if not result:
                self.log(f"{symbol}: Stooq failed for index")
            return result

        # --- Forex: open.er-api (confirmed cloud-safe) ---
        if symbol in forex_pairs:
            result = fetch_forex(symbol)
            if not result:
                self.log(f"{symbol}: all forex sources failed")
            return result

        # --- Stocks & other commodities (WTI, Copper): Stooq primary ---
        result = fetch_stooq(symbol)
        if result:
            return result

        # --- Alpha Vantage: last-resort fallback for stocks ---
        if self.alpha_key:
            self.log(f"{symbol}: Stooq failed, trying Alpha Vantage")
            return fetch_av(symbol, self.alpha_key)

        return None

    def run(self, watchlist: List[str]) -> List[AssetData]:
        self.log("Starting data fetch for watchlist")
        results: List[AssetData] = []
        for symbol in watchlist:
            data = None
            for attempt in range(2):
                try:
                    data = self.fetch_one(symbol)
                    if data:
                        break
                except Exception as e:
                    self.log(f"Fetch {symbol} attempt {attempt + 1} error: {type(e).__name__}: {e}")
                    if attempt == 0:
                        time.sleep(3)

            if data:
                self.log(
                    f"Fetched {symbol}: price={data.price} "
                    f"ohlcv={len(data.ohlcv_50)}d change={data.change_24h}"
                )
                results.append(data)
            else:
                self.log(f"Skipped {symbol} (all sources failed)")

        self.log(f"Fetched {len(results)}/{len(watchlist)} assets")
        return results