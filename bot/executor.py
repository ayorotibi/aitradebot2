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

    our_positions = {p["symbol"] for p in db.get_open_positions()}
    if symbol in our_positions:
        db.log("INFO", "executor",
               f"Already holding {symbol} - skipping duplicate BUY signal instead of adding to the position.")
        return

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


def reconcile_positions(alpaca, rules: dict):
    """Runs at the start of every trading cycle, before any new signals are
    evaluated. This bot's own `positions` table is only ever updated when
    THIS bot places or flattens an order - but a bracket order's stop-loss
    or take-profit leg can close a position on its own, straight at Alpaca,
    with nothing telling this bot about it. Left unreconciled, that symbol
    stays "open" in our DB forever: it keeps eating one of max_open_positions
    even though the shares are gone, and the realized gain/loss from that
    exit never gets recorded anywhere (which is also why daily PnL used to
    always read $0.00).

    For each symbol we think we hold, this checks whether Alpaca still
    agrees. If Alpaca no longer shows an open position, we look up the
    filled price of the bracket leg that closed it, record that as a SELL
    trade (with realized PnL if the fill price could be found), and drop it
    from our tracked positions so the freed-up slot can be used again.

    If Alpaca still shows it open but with no live order left that could
    ever close it (e.g. a DAY-TIF bracket leg that expired before
    place_bracket_order() was switched to GTC), it's re-armed on the spot
    with a fresh OCO stop-loss/take-profit pair - see _rearm_unprotected()."""
    closed = []
    for pos in db.get_open_positions():
        symbol = pos["symbol"]
        broker_pos = alpaca.broker_position_for_symbol(symbol)
        if broker_pos is not None:
            # Still open at Alpaca - nothing to remove from tracking, but
            # make sure it still has something that could eventually close
            # it, since reconciliation only ever notices an ACTUAL closed
            # position - a position that's open-and-bare never trips that.
            if not alpaca.has_live_protective_orders(symbol, broker_pos):
                _rearm_unprotected(alpaca, rules, pos)
            continue

        exit_price = alpaca.last_bracket_exit_price(symbol)
        pnl = None
        if exit_price is not None:
            pnl = round((exit_price - pos["avg_price"]) * pos["qty"], 2)

        db.record_trade(
            symbol, "SELL", pos["qty"], exit_price, "BRACKET_EXIT", "FILLED",
            "Position closed by Alpaca (stop-loss or take-profit filled) - detected on reconciliation",
            pnl=pnl,
        )
        db.remove_position(symbol)

        if pnl is not None:
            db.log("INFO", "executor",
                   f"Reconciled {symbol}: Alpaca shows this position already closed "
                   f"(exit ~{exit_price:.2f}, realized PnL ${pnl:+.2f}). Freed up a position slot.")
            if rules["notifications"]["notify_on_trade"]:
                notifier.send(f"Position closed: {symbol} exited ~{exit_price:.2f}, realized PnL ${pnl:+.2f} "
                               f"(stop-loss/take-profit filled at Alpaca)")
        else:
            db.log("WARNING", "executor",
                   f"Reconciled {symbol}: Alpaca shows this position already closed, but the exit "
                   f"fill price could not be found, so PnL for this trade was not recorded. "
                   f"Freed up a position slot.")

        closed.append({"symbol": symbol, "pnl": pnl})
    return closed


def _rearm_unprotected(alpaca, rules: dict, pos: dict):
    """Places a fresh OCO stop-loss/take-profit pair on a position Alpaca
    still shows as open but that has no live exit order left. Prices are
    computed from the CURRENT market price, not the original entry price
    (pos["avg_price"]) - so re-arming is safe to run unattended: neither
    the new stop nor the new target can be on the wrong side of the live
    price and fire immediately on submission, which re-arming off the
    original entry price could do if the price has moved a lot since. This
    does mean the new protection reflects the rules' stop_loss_pct/
    take_profit_pct from here, not from the original entry - the realized
    PnL if it fires is still measured against the real avg_price, only the
    new order's trigger levels are relative to today's price."""
    symbol = pos["symbol"]
    price = alpaca.last_price(symbol)
    if not price or price <= 0:
        db.log("WARNING", "executor",
               f"{symbol} is unprotected but a live price could not be fetched - "
               f"could not re-arm this cycle, will retry next cycle.")
        return

    stop_loss, take_profit = risk.stop_and_target_prices(price, "BUY", rules)
    order_id = _client_order_id(symbol)
    try:
        alpaca.place_oco_exit_order(symbol, pos["qty"], stop_loss, take_profit, order_id)
        db.update_position_stops(symbol, stop_loss, take_profit)
        db.log("INFO", "executor",
               f"Re-armed {symbol}: new SL={stop_loss:.2f} TP={take_profit:.2f} around current price "
               f"~{price:.2f} (client_order_id={order_id}). Position is protected again.")
        if rules["notifications"]["notify_on_trade"]:
            notifier.send(f"Re-armed protection for {symbol}: SL {stop_loss:.2f} / TP {take_profit:.2f} "
                           f"(current price ~{price:.2f})")
    except Exception as exc:
        db.log("ERROR", "executor", f"Failed to re-arm protection for {symbol}: {exc}")
        if rules["notifications"]["notify_on_error"]:
            notifier.send(f"⚠️ Failed to re-arm protection for {symbol}: {exc}")


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
