"""
Premarket gap scanner - the "morning_prefilter" step from the original
article. Pulls the S&P 500 universe, computes each ticker's gap vs. previous
close, filters against the dashboard-editable rules, and records every
candidate (pass or fail) to scan_results for visibility in the dashboard.
"""
import logging

from common import db
from bot.data_feed import get_gap_candidates
from bot.sp500_universe import fetch_sp500_tickers

logger = logging.getLogger("bot.scanner")


def run_premarket_scan(rules: dict) -> list:
    strat = rules["strategies"]["gap_momentum"]
    if not strat.get("enabled", True):
        db.log("INFO", "scanner", "gap_momentum strategy disabled - skipping premarket scan")
        return []

    tickers = fetch_sp500_tickers()
    if not tickers:
        db.log("ERROR", "scanner", "S&P 500 universe was empty - scan aborted")
        return []

    df = get_gap_candidates(tickers)
    passed = []

    for _, row in df.iterrows():
        ok = (
            strat["min_price"] <= row["last_price"] <= strat["max_price"]
            and strat["min_gap_pct"] <= abs(row["gap_pct"]) <= strat["max_gap_pct"]
            and row["volume"] >= strat["min_avg_volume"]
        )
        notes = "passed all filters" if ok else "filtered out"
        db.record_scan(
            symbol=row["symbol"], gap_pct=row["gap_pct"], price=row["last_price"],
            volume=row["volume"], passed=ok, notes=notes,
        )
        if ok:
            passed.append(row.to_dict())

    passed.sort(key=lambda r: abs(r["gap_pct"]), reverse=True)
    db.log("INFO", "scanner", f"Premarket scan complete: {len(passed)}/{len(df)} candidates passed filters")
    return passed
