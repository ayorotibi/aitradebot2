"""
Loads the S&P 500 ticker universe. Tries to fetch a current list from a
public source and falls back to a small bundled snapshot so the bot never
hard-fails just because a data source is unreachable or changed its markup.

The bundled fallback is NOT guaranteed current - refresh sp500_fallback.csv
periodically (e.g. from https://github.com/datasets/s-and-p-500-companies)
if you rely on it for more than short outages.

Tickers are kept in exchange-native form (e.g. "BRK.B", not yfinance's
"BRK-B") since bot/data_feed.py now sources bars from Alpaca, and Alpaca
expects the dotted share-class notation - a hyphenated symbol comes back
as "invalid symbol" and (per Alpaca's batch behavior) fails the whole
batch it's in, not just that one ticker.
"""
import csv
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("bot.sp500_universe")

FALLBACK_PATH = Path(__file__).parent / "sp500_fallback.csv"
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def fetch_sp500_tickers() -> list:
    try:
        tables = pd.read_html(WIKI_URL)
        df = tables[0]
        tickers = sorted(df["Symbol"].astype(str).tolist())
        if len(tickers) > 400:  # sanity check before trusting it
            _write_fallback(tickers)
            return tickers
        raise ValueError(f"Unexpectedly small ticker list ({len(tickers)})")
    except Exception as exc:
        logger.warning("Live S&P 500 list fetch failed (%s); using bundled fallback.", exc)
        return _read_fallback()


def _write_fallback(tickers: list):
    try:
        with open(FALLBACK_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Symbol"])
            for t in tickers:
                writer.writerow([t])
    except Exception as exc:
        logger.warning("Could not refresh sp500_fallback.csv: %s", exc)


def _read_fallback() -> list:
    if not FALLBACK_PATH.exists():
        logger.error("No bundled S&P 500 fallback found at %s", FALLBACK_PATH)
        return []
    with open(FALLBACK_PATH) as f:
        reader = csv.reader(f)
        next(reader, None)
        return [row[0] for row in reader if row]
