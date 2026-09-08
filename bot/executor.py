"""
Turns a signal into an order, respecting every safety gate: kill switch,
the paper/live effective-mode gate, max open positions, position sizing,
capital allocation, and - because this account is shared with another
bot/strategy - a check that this symbol isn't already held by that other
bot. Every attempt, executed or blocked, is logged so the dashboard's
activity feed tells the full story.
"""
import logging
import time
import uuid

from common import db
from common.config import Config
from bot import notifier, risk

logger = logging.getLogger("bot.executor")


def _client_order_id(symbol: str) -> str:
    return f"{Config.BOT_ORDER_TAG}-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _symbol_conflicts_with_other_bot(alpaca, symbol: str) -> bool:
    """True if Alpaca shows an open position in this symbol that our own DB
    didn't open - i.e. it belongs to the other strategy sharing this
    account. We refuse to trade a symbol under those conditions rather than
    risk merging into (or accidentally flattening) someone else's shares."""
    broker_pos = alpaca.broker_position_for_symbol(symbol)
    if broker_pos is None:
        return False
    our_positions = {p["symbol"] for p in db.get_open_positions()}
    return symbol not in our_positions


def execute_signal(alpaca, rules: dict, signal: dict):
    symbol = signal["symbol"]
    action = signal["action"]

    if action != "BUY":
        return

    if rules.get("kill_switch"):
        db.log("INFO", "executor", f"Kill switch engaged - skipping {symbol} signal")
        return

    effective_mode = Config.effective_mode(rules["mode"])
    if rules["mode"] == "live" and effective_mode == "paper":
        db.log("WARNING", "executor",
               f"rules.mode='live' but ALLOW_LIVE_TRADING is not set in the environment - "
               f"{symbol} will be traded on the PAPER account instead. This is an intentional double gate.")

    if _symbol_conflicts_with_other_bot(alpaca, symbol):
        db.log("WARNING", "executor",
               f"{symbol} already has a position in this Alpaca account that this bot didn't open "
               f"(likely your other strategy) - skipping to avoid interfering with it.")
        return

    if not risk.under_position_limit(rules):
        db.log("INFO", "executor", f"Max open positions reached - skipping {symbol}")
        return

    account = alpaca.account_summary()
    equity = account.get("NetLiquidation", 0.0)

    if not risk.under_day_trade_limit(account):
        db.log("WARNING", "executor",
               f"Skipping {symbol} - account is at/near the Pattern Day Trader limit "
               f"({account.get('DayTradeCount')} day trades, equity ${equity:,.2f}). "
               f"This is an account-wide limit shared with your other bot, so this bot "
               f"stops opening new positions rather than risk tripping it.")
        if rules["notifications"]["notify_on_error"]:
            notifier.send(f"⚠️ Skipped {symbol}: near account-wide PDT day-trade limit "
                           f"({account.get('DayTradeCount')}/{risk.PDT_DAY_TRADE_LIMIT})")
        return

    price = alpaca.last_price(symbol)
    if not price or price <= 0:
        db.log("WARNING", "executor", f"Could not get a live price for {symbol} - skipping")
        return

    qty = risk.position_size(equity, price, rules)
    if qty <= 0:
        db.log("INFO", "executor",
               f"Computed position size for {symbol} was 0 (check capital_allocation_usd headroom) - skipping")
        return

    stop_loss, take_profit = risk.stop_and_target_prices(price, "BUY", rules)
    order_id = _client_order_id(symbol)

    try:
        alpaca.place_bracket_order(symbol, qty, "BUY", stop_loss, take_profit, order_id)
        db.record_trade(symbol, "BUY", qty, price, "MARKET+BRACKET", "SUBMITTED", signal["reason"])
        db.upsert_position(symbol, qty, price, stop_loss, take_profit)
        db.log("INFO", "executor",
               f"Submitted BUY {qty} {symbol} @ ~{price:.2f}, SL={stop_loss:.2f} TP={take_profit:.2f} "
               f"(client_order_id={order_id}, mode={effective_mode})")
        if rules["notifications"]["notify_on_trade"]:
            notifier.send(f"BUY {qty} {symbol} @ ~{price:.2f}\nSL {stop_loss:.2f} / TP {take_profit:.2f}\n{signal['reason']}")
    except Exception as exc:
        db.log("ERROR", "executor", f"Order failed for {symbol}: {exc}")
        if rules["notifications"]["notify_on_error"]:
            notifier.send(f"⚠️ Order failed for {symbol}: {exc}")


def flatten_all(alpaca, reason: str):
    """Used by the dashboard's kill-switch endpoint for an immediate,
    manual flatten of every position THIS BOT opened - never the other
    bot's positions, even in the same symbol."""
    positions = db.get_open_positions()
    for pos in positions:
        try:
            order_id = _client_order_id(pos["symbol"])
            alpaca.close_tracked_position(pos["symbol"], pos["qty"], order_id)
            db.record_trade(pos["symbol"], "SELL", pos["qty"], None, "MARKET", "SUBMITTED", reason)
            db.remove_position(pos["symbol"])
            db.log("WARNING", "executor", f"Flattened {pos['symbol']} ({reason})")
        except Exception as exc:
            db.log("ERROR", "executor", f"Failed to flatten {pos['symbol']}: {exc}")
