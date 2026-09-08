"""
Market data for scanning. This deliberately does NOT depend on TradingView
Desktop (see the project README for why). yfinance is free, needs no auth,
and is good enough for the premarket gap scan (previous close vs. latest
quote across hundreds of tickers) and the breakout strategy's daily bars -
this mirrors how the original article's "Part 3" premarket analyst pulled
data. The actual execution price at order time comes from Alpaca directly
(bot/alpaca_client.py's last_price()), since that's the data your orders
execute against.
"""
import logging

import pandas as pd
import yfinance as yf

logger = logging.getLogger("bot.data_feed")


def get_gap_candidates(tickers: list, batch_size: int = 100) -> pd.DataFrame:
    """Returns a DataFrame with columns: symbol, prev_close, last_price,
    gap_pct, volume - one row per ticker that yfinance returned data for."""
    rows = []
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            data = yf.download(
                tickers=batch, period="2d", interval="1d",
                group_by="ticker", progress=False, threads=True,
            )
        except Exception as exc:
            logger.warning("yfinance batch download failed for %s tickers: %s", len(batch), exc)
            continue

        for symbol in batch:
            try:
                if len(batch) == 1:
                    df = data
                else:
                    df = data[symbol]
                df = df.dropna()
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
    try:
        df = yf.download(symbol, period=f"{lookback_days}d", interval="1d", progress=False)
        return df.dropna()
    except Exception as exc:
        logger.warning("yfinance daily bars failed for %s: %s", symbol, exc)
        return pd.DataFrame()
