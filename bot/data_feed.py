"""
Market data for scanning. This used to run on yfinance (free, no auth), but
Yahoo Finance blocks requests from cloud/datacenter IP ranges - including
Render's - so every yfinance call from a deployed instance came back empty
(see the JSONDecodeError / 403s in the Render logs if you're wondering why
this changed). Alpaca already provides authenticated market data via the
same account used for execution (bot/alpaca_client.py's last_price()), so
this now pulls daily bars from there instead: no scraping, no IP-blocking
risk, one fewer dependency.

Free/paper Alpaca accounts only have entitlement to the IEX feed (not SIP),
so every request below asks for feed=IEX explicitly - leaving it to
alpaca-py's default risks a 403 on accounts without a SIP subscription.

Either key pair works for market data (it's tied to the account, not to
paper-vs-live trading permissions), so this reads whichever is configured,
preferring paper since that's the default mode. If you rotate keys, both
this module and bot/alpaca_client.py need the same account's keys.
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

from common.config import Config

logger = logging.getLogger("bot.data_feed")

_client = None


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        api_key = Config.ALPACA_PAPER_API_KEY or Config.ALPACA_LIVE_API_KEY
        secret_key = Config.ALPACA_PAPER_SECRET_KEY or Config.ALPACA_LIVE_SECRET_KEY
        if not api_key or not secret_key:
            logger.error("No Alpaca API key/secret configured - cannot fetch market data")
        _client = StockHistoricalDataClient(api_key, secret_key)
    return _client


def _symbol_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """alpaca-py's BarSet.df is (symbol, timestamp)-MultiIndexed even for a
    single-symbol request - pull one symbol's rows out as a plain
    timestamp-indexed frame with yfinance-style capitalized columns."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        if symbol not in df.index.get_level_values(0):
            return pd.DataFrame()
        sym_df = df.xs(symbol, level=0)
    else:
        sym_df = df
    sym_df = sym_df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    return sym_df.sort_index()


def get_gap_candidates(tickers: list, batch_size: int = 100) -> pd.DataFrame:
    """Returns a DataFrame with columns: symbol, prev_close, last_price,
    gap_pct, volume - one row per ticker Alpaca returned at least 2 daily
    bars for."""
    client = _get_client()
    rows = []
    start = datetime.now(timezone.utc) - timedelta(days=10)  # covers weekends/holidays

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=start, feed=DataFeed.IEX,
            )
            data = client.get_stock_bars(req).df
        except Exception as exc:
            logger.warning("Alpaca batch bar fetch failed for %s tickers: %s", len(batch), exc)
            continue

        for symbol in batch:
            try:
                df = _symbol_frame(data, symbol).dropna()
                if len(df) < 2:
                    continue
                prev_close = float(df["Close"].iloc[-2])
                last_price = float(df["Close"].iloc[-1])
                volume = float(df["Volume"].iloc[-1])
                if prev_close <= 0:
                    continue
                gap_pct = (last_price - prev_close) / prev_close * 100.0
                rows.append({
                    "symbol": symbol, "prev_close": prev_close,
                    "last_price": last_price, "gap_pct": gap_pct, "volume": volume,
                })
            except Exception:
                continue

    return pd.DataFrame(rows)


def get_daily_bars(symbol: str, lookback_days: int = 30) -> pd.DataFrame:
    client = _get_client()
    # Calendar-day buffer so lookback_days of *trading* days actually fit
    # in the window (weekends/holidays), same intent as yfinance's period=.
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days + 15)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=start, feed=DataFeed.IEX,
        )
        data = client.get_stock_bars(req).df
        return _symbol_frame(data, symbol).dropna()
    except Exception as exc:
        logger.warning("Alpaca daily bars failed for %s: %s", symbol, exc)
        return pd.DataFrame()
