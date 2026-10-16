"""
The baseline rule set, modeled on the rules.json / strategy described in the
Humbled Trader "AI Trading Bot with Claude + IBKR" article: an S&P 500 gap
scanner feeding a momentum entry, with fixed position sizing and stop-loss /
take-profit exits, run on a fixed daily schedule.

This dict is the single source of truth for what the dashboard can edit and
what the bot reads each cycle. It is stored as one JSON blob in the `rules`
table (see common/db.py) so edits from the dashboard take effect on the bot's
next loop iteration without a restart.
"""

DEFAULT_RULES = {
    "mode": "paper",                # "paper" | "live" - live also requires ALLOW_LIVE_TRADING=true in .env
    "kill_switch": False,           # when true, bot scans/logs but places no orders and exits no positions except protective ones

    "schedule": {
        "timezone": "America/New_York",
        "premarket_scan_time": "08:00",
        "trading_window_start": "09:35",
        "trading_window_end": "15:55",
        "time_exit_enabled": True,     # flatten every open position once per day at time_exit_time,
                                        # so nothing is held overnight into unknown pre/after-market moves
        "time_exit_time": "15:50",
        "eod_summary_time": "16:15",
        "cycle_interval_minutes": 5,
    },

    "universe": "sp500",            # currently only "sp500" is implemented

    "strategies": {
        "gap_momentum": {
            "enabled": True,
            "min_gap_pct": 2.0,
            "max_gap_pct": 15.0,
            "min_price": 5.0,
            "max_price": 500.0,
            "min_avg_volume": 500000,
        },
        "breakout": {
            "enabled": False,
            "lookback_days": 20,
            "breakout_pct": 1.5,
        },
    },

    "risk": {
        "sizing_method": "risk_based",   # "risk_based" (recommended): size each trade so that actually
                                          # hitting the stop-loss loses exactly risk_per_trade_pct of
                                          # allocated capital, regardless of this stock's stop distance.
                                          # "fixed_dollar" (legacy): always spend position_size_pct of
                                          # allocated capital, with no reference to the stop distance.
        "risk_per_trade_pct": 1.0,       # used when sizing_method="risk_based" - % of allocated capital
                                          # you're willing to lose on this one trade if the stop fills.
        "max_position_pct": 20.0,        # safety ceiling that applies under EITHER sizing method: never
                                          # put more than this % of allocated capital into a single
                                          # position, even if risk-based math (a tight stop relative to
                                          # risk_per_trade_pct) would otherwise call for more.
        "position_size_pct": 2.0,     # % of this bot's allocated capital per new position - used when
                                       # sizing_method="fixed_dollar" (see capital_allocation_usd)
        "max_open_positions": 5,
        "max_trades_per_day": 10,     # blank/0 = no daily trade-count limit. Resets automatically at
                                       # midnight UTC since it's computed from today's trade count, not
                                       # a flag - no manual reset needed.
        "stop_loss_pct": 3.0,
        "take_profit_pct": 6.0,
        "max_daily_loss_pct": 5.0,    # trips kill_switch automatically for the rest of the day
        "weekly_loss_limit_pct": 10.0,   # blank/0 = disabled. Trips kill_switch automatically, same as
                                          # max_daily_loss_pct, when this week's realized PnL (Mon-Sun,
                                          # UTC) drops below -this% of allocated capital.
        "consecutive_loss_pause_count": 3,  # blank/0 = disabled. Trips kill_switch automatically after
                                             # this many losing trades in a row (most recent closed trades).
        "capital_allocation_usd": None,  # cap on total $ this bot will deploy at once, since the
                                          # Alpaca account is shared with another bot/strategy.
                                          # null = use the full account equity (not recommended
                                          # on a shared account - set a number instead).
    },

    "notifications": {
        "telegram_enabled": True,
        "notify_on_trade": True,
        "notify_on_error": True,
        "notify_eod_summary": True,
    },
}


def merge_with_defaults(rules: dict) -> dict:
    """Deep-merge a stored rules dict on top of DEFAULT_RULES so new fields
    added in later versions of this file always have a value, even for a
    rules row saved by an older version of the app."""
    def merge(base, override):
        result = dict(base)
        for k, v in (override or {}).items():
            if isinstance(v, dict) and isinstance(result.get(k), dict):
                result[k] = merge(result[k], v)
            else:
                result[k] = v
        return result
    return merge(DEFAULT_RULES, rules)
