"""
Signal evaluation. Deliberately simple, rule-based logic (no chart-reading
LLM call in the hot path) so the entry/exit logic is deterministic, testable,
and cheap to run every cycle across dozens of candidates. This mirrors the
baseline strategy in the article: gap-and-go momentum, with an optional
breakout add-on you can enable from the dashboard.

If you want an LLM in the loop for qualitative judgment (news, sentiment),
add it as an extra filter that runs AFTER this quantitative pass narrows the
list to a handful of candidates - not as a replacement for it. See the
README's "Where an LLM fits" section.
"""
import logging

from bot.data_feed import get_daily_bars

logger = logging.getLogger("bot.strategy")


def evaluate_gap_momentum(candidate: dict, rules: dict) -> dict:
    """A candidate that has already passed the premarket gap filter is a
    'buy' if the gap is positive (gap-up momentum), 'sell short' if the
    strategy config allows shorts (not enabled by default in this
    baseline), otherwise 'hold'."""
    gap_pct = candidate["gap_pct"]
    if gap_pct > 0:
        return {"symbol": candidate["symbol"], "action": "BUY",
                "reason": f"Gap up {gap_pct:.2f}% vs prior close"}
    return {"symbol": candidate["symbol"], "action": "HOLD",
            "reason": f"Gap down {gap_pct:.2f}% - shorting disabled in baseline rules"}


def evaluate_breakout(symbol: str, rules: dict) -> dict:
    strat = rules["strategies"]["breakout"]
    if not strat.get("enabled", False):
        return {"symbol": symbol, "action": "HOLD", "reason": "breakout strategy disabled"}

    bars = get_daily_bars(symbol, lookback_days=strat["lookback_days"] + 2)
    if bars.empty or len(bars) < strat["lookback_days"]:
        return {"symbol": symbol, "action": "HOLD", "reason": "insufficient bar history"}

    lookback_high = bars["High"].iloc[-(strat["lookback_days"] + 1):-1].max()
    last_close = bars["Close"].iloc[-1]
    threshold = lookback_high * (1 + strat["breakout_pct"] / 100.0)

    if last_close >= threshold:
        return {"symbol": symbol, "action": "BUY",
                "reason": f"Broke {strat['lookback_days']}d high by >= {strat['breakout_pct']}%"}
    return {"symbol": symbol, "action": "HOLD", "reason": "no breakout"}
