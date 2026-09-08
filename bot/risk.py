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


def position_size(equity: float, price: float, rules: dict) -> int:
    if price <= 0 or equity <= 0:
        return 0
    base = allocated_capital_base(equity, rules)
    dollar_size = base * (rules["risk"]["position_size_pct"] / 100.0)
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
