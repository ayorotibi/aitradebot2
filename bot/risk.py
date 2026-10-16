"""
Risk controls: position sizing and the two circuit breakers (max open
positions, max daily loss) that force the bot to stop opening new trades.
These read from the same dashboard-editable rules dict as everything else,
so tightening a % in the UI takes effect on the next cycle.
"""
import logging

from common import db

logger = logging.getLogger("bot.risk")


def allocated_capital_base(equity: float, rules: dict) -> float:
    """The dollar figure position sizing is a % of. On a shared account,
    this should be capital_allocation_usd (a slice of the account reserved
    for this bot), not the full account equity - otherwise this bot could
    size positions as if it owned money the other bot is also using."""
    cap = rules["risk"].get("capital_allocation_usd")
    if cap is not None and cap > 0:
        return min(equity, cap)
    return equity


def capital_headroom(rules: dict, equity: float) -> float:
    """How many more dollars this bot is allowed to have deployed right now,
    given capital_allocation_usd and what it already has open (per this
    bot's own DB, not the account's total position - the other bot's
    positions don't count against this bot's allocation)."""
    base = allocated_capital_base(equity, rules)
    deployed = sum(p["qty"] * p["avg_price"] for p in db.get_open_positions())
    return max(base - deployed, 0.0)


def position_size(equity: float, price: float, rules: dict, stop_loss_price: float = None) -> int:
    """Two sizing methods, chosen by rules["risk"]["sizing_method"]:

    "risk_based" (recommended, the default): size the trade so that if the
    stop-loss actually fills, the dollar loss equals risk_per_trade_pct of
    allocated capital - regardless of how far away this particular stock's
    stop happens to be. A stock with a wide stop_distance gets a smaller
    share count than one with a tight stop_distance, for the same dollar
    risk. This needs stop_loss_price; callers should compute the stop
    price first and pass it in. Falls back to the fixed-dollar method
    below if stop_loss_price isn't supplied (e.g. an old caller) or the
    stop distance is zero.

    "fixed_dollar" (legacy): always spend position_size_pct of allocated
    capital, with no reference to the stop distance at all - this is what
    this function did before risk-based sizing was added, kept as an
    option since it's simpler to reason about.

    Either way, max_position_pct is then applied as a hard ceiling - a
    tight stop relative to risk_per_trade_pct can otherwise call for an
    outsized position, and this caps that regardless of which method ran."""
    if price <= 0 or equity <= 0:
        return 0
    base = allocated_capital_base(equity, rules)
    risk_cfg = rules["risk"]
    method = risk_cfg.get("sizing_method", "risk_based")

    dollar_size = None
    if method == "risk_based" and stop_loss_price:
        stop_distance = abs(price - stop_loss_price)
        if stop_distance > 0:
            risk_amount = base * (risk_cfg.get("risk_per_trade_pct", 1.0) / 100.0)
            dollar_size = (risk_amount / stop_distance) * price

    if dollar_size is None:
        dollar_size = base * (risk_cfg["position_size_pct"] / 100.0)

    max_position_pct = risk_cfg.get("max_position_pct")
    if max_position_pct:
        dollar_size = min(dollar_size, base * (max_position_pct / 100.0))

    dollar_size = min(dollar_size, capital_headroom(rules, equity))
    qty = int(dollar_size // price)
    return max(qty, 0)


def stop_and_target_prices(entry_price: float, side: str, rules: dict) -> tuple:
    sl_pct = rules["risk"]["stop_loss_pct"] / 100.0
    tp_pct = rules["risk"]["take_profit_pct"] / 100.0
    if side == "BUY":
        return entry_price * (1 - sl_pct), entry_price * (1 + tp_pct)
    return entry_price * (1 + sl_pct), entry_price * (1 - tp_pct)


def under_position_limit(rules: dict) -> bool:
    open_count = len(db.get_open_positions())
    return open_count < rules["risk"]["max_open_positions"]


PDT_EQUITY_THRESHOLD = 25000.0
PDT_DAY_TRADE_LIMIT = 3  # FINRA: 4+ day trades in 5 business days makes you a "pattern day trader"


def under_day_trade_limit(account: dict) -> bool:
    """FINRA's Pattern Day Trader rule applies to the WHOLE Alpaca account,
    not per-bot - so a day trade from your other strategy counts against
    the same limit as one from this bot, and this bot getting the account
    flagged would restrict your other bot's trading too. Alpaca reports
    both the account's current day-trade count and its PDT status directly
    (account_summary()), so this reads that rather than keeping its own
    ledger. Not a dashboard-editable rule on purpose - overriding a
    regulatory guardrail shouldn't be a config toggle.

    Since this bot exits the same day fairly often (stop-loss/take-profit),
    a new entry today could itself become tomorrow's day trade - so the
    check refuses new entries once daytrade_count is already at the limit,
    rather than waiting for the count to already be over it."""
    equity = account.get("NetLiquidation", 0.0)
    if equity >= PDT_EQUITY_THRESHOLD:
        return True  # PDT rule doesn't restrict accounts at/above this equity
    if account.get("PatternDayTrader"):
        return False  # already flagged and under the equity threshold - Alpaca will restrict trading
    return account.get("DayTradeCount", 0) < PDT_DAY_TRADE_LIMIT


def check_daily_loss_kill_switch(rules: dict) -> bool:
    """Returns True if the kill switch was just tripped by today's losses.
    Also persists the change to the rules row so the dashboard reflects it
    immediately."""
    if rules.get("kill_switch"):
        return False  # already tripped, nothing new to do

    snapshot = db.get_latest_snapshot()
    if not snapshot or not snapshot.get("equity"):
        return False

    daily_pnl = snapshot["daily_pnl"] or 0.0
    # Measured against this bot's allocated capital, not the whole shared
    # account's equity - a loss that's small relative to the full account
    # could still be a large hit to the slice actually assigned to this bot.
    base = allocated_capital_base(snapshot["equity"], rules)
    max_loss_pct = rules["risk"]["max_daily_loss_pct"]

    if base > 0 and daily_pnl < 0 and abs(daily_pnl) / base * 100.0 >= max_loss_pct:
        rules["kill_switch"] = True
        db.set_rules(rules)
        db.log("WARNING", "risk",
               f"Daily loss {daily_pnl:.2f} ({abs(daily_pnl)/base*100:.2f}% of allocated capital) hit "
               f"max_daily_loss_pct={max_loss_pct}% - kill switch engaged automatically")
        return True
    return False


def check_weekly_loss_kill_switch(rules: dict) -> bool:
    """Same pattern as check_daily_loss_kill_switch(), but measured against
    this calendar week's (Mon-Sun, UTC) total realized PnL instead of just
    today's - catches a slow bleed of small daily losses that each stay
    under max_daily_loss_pct but add up to a bad week. Like the daily
    version, this trips the SAME kill_switch flag, which this codebase only
    ever clears manually from the dashboard - there is no automatic
    reset, for either breaker, at the start of a new day or week."""
    if rules.get("kill_switch"):
        return False

    max_loss_pct = rules["risk"].get("weekly_loss_limit_pct")
    if not max_loss_pct:
        return False

    snapshot = db.get_latest_snapshot()
    if not snapshot or not snapshot.get("equity"):
        return False

    week_pnl = db.get_week_pnl()
    base = allocated_capital_base(snapshot["equity"], rules)

    if base > 0 and week_pnl < 0 and abs(week_pnl) / base * 100.0 >= max_loss_pct:
        rules["kill_switch"] = True
        db.set_rules(rules)
        db.log("WARNING", "risk",
               f"This week's realized loss {week_pnl:.2f} ({abs(week_pnl)/base*100:.2f}% of allocated "
               f"capital) hit weekly_loss_limit_pct={max_loss_pct}% - kill switch engaged automatically. "
               f"This does not clear on its own - toggle it off from the dashboard once you're ready to "
               f"resume trading.")
        return True
    return False


def check_consecutive_loss_pause(rules: dict) -> bool:
    """Trips the kill switch after N losing trades in a row (the most
    recent N closed trades, by exit order, all with negative realized
    PnL) - a streak that often means the strategy is mismatched to current
    conditions for the day, even if no single loss or daily/weekly total
    has crossed its own limit yet. Same manual-reset caveat as the other
    two kill-switch breakers above."""
    if rules.get("kill_switch"):
        return False

    pause_count = rules["risk"].get("consecutive_loss_pause_count")
    if not pause_count or pause_count <= 0:
        return False

    recent = db.get_recent_closed_trade_pnls(limit=pause_count)
    if len(recent) < pause_count:
        return False  # not enough closed trades yet to judge a streak

    if all(p < 0 for p in recent):
        rules["kill_switch"] = True
        db.set_rules(rules)
        db.log("WARNING", "risk",
               f"{pause_count} consecutive losing trades detected - kill switch engaged automatically "
               f"to pause trading for a cool-down. This does not clear on its own - toggle it off from "
               f"the dashboard once you're ready to resume trading.")
        return True
    return False


def under_daily_trade_limit(rules: dict) -> bool:
    """Unlike the three breakers above, this one needs no manual reset: it
    counts today's trades fresh each time, so it naturally opens back up
    at midnight UTC without anyone touching the dashboard."""
    limit = rules["risk"].get("max_trades_per_day")
    if not limit or limit <= 0:
        return True
    return db.get_today_trade_count(side="BUY") < limit
