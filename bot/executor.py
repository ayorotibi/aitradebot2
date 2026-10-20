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

    if not risk.under_daily_trade_limit(rules):
        db.log("INFO", "executor",
               f"Max trades per day ({rules['risk'].get('max_trades_per_day')}) already reached - skipping {symbol}")
        return

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

    # The gap_momentum signal was decided against this morning's premarket
    # scan, but the trading cycle that got us here can run hours later - by
    # then the "gap" that made this a candidate may have already faded or
    # fully reversed. Re-check it against the live price we just fetched
    # (reusing this call rather than an extra one) before committing real
    # capital. signal["prev_close"] is only set for gap_momentum signals
    # (see bot/strategy.py) - a breakout signal has no prev_close and skips
    # this check entirely, since staleness isn't a concern there (it
    # already re-fetches live daily bars every cycle).
    prev_close = signal.get("prev_close")
    if prev_close:
        strat = rules["strategies"]["gap_momentum"]
        current_gap_pct = (price - prev_close) / prev_close * 100.0
        if not (strat["min_gap_pct"] <= current_gap_pct <= strat["max_gap_pct"]):
            scan_gap_pct = signal.get("scan_gap_pct", current_gap_pct)
            db.log("INFO", "executor",
                   f"{symbol} gapped {scan_gap_pct:.2f}% at this morning's scan but is now at "
                   f"{current_gap_pct:.2f}% vs prior close (need {strat['min_gap_pct']}-{strat['max_gap_pct']}%) - "
                   f"the move has faded or reversed since the scan, skipping rather than buying stale momentum.")
            return

    # Stop/target prices are computed before sizing (not after, as before)
    # because risk-based sizing (rules["risk"]["sizing_method"]) needs the
    # stop distance to decide the share count - see bot/risk.py.
    stop_loss, take_profit = risk.stop_and_target_prices(price, "BUY", rules)
    qty = risk.position_size(equity, price, rules, stop_loss_price=stop_loss)
    if qty <= 0:
        db.log("INFO", "executor",
               f"Computed position size for {symbol} was 0 (check capital_allocation_usd headroom) - skipping")
        return

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
    manual flatten, and by bot/main.py's scheduled time-based exit, to
    close every position THIS BOT opened - never the other bot's
    positions, even in the same symbol.

    Every position this bot opens carries a live GTC bracket order, which
    claims the ENTIRE share count as "held for orders" at Alpaca for as
    long as it rests there. A plain flatten SELL submitted on top of that
    is rejected ("insufficient qty available", available: 0) - first
    surfaced when the scheduled time-based exit flattened one position
    fine but failed on four others still protected by a live bracket the
    same evening. So each symbol's own resting orders are cancelled first
    (freeing the shares), with a short pause for Alpaca to register the
    cancellation, before the flatten SELL is submitted.

    Before cancelling a symbol's resting orders, this checks that Alpaca's
    own broker-side quantity for that symbol exactly matches what this
    bot's own records show. If it doesn't - or Alpaca shows no position
    there at all, e.g. something else (reconciliation) beat this to it -
    that means either this bot's tracking has drifted or the other
    strategy sharing this account also holds shares in that same symbol,
    and cancelling "every open order for this symbol" could touch an
    order that isn't ours. Either way this skips the symbol and logs it
    for a manual look, rather than risk that. (The existence-only check
    used elsewhere in this file, _symbol_conflicts_with_other_bot(), only
    catches a symbol this bot doesn't track at all - every symbol reached
    here already IS tracked, since this loop iterates
    db.get_open_positions(), so that check alone would never fire here -
    hence the quantity comparison here instead.)"""
    positions = db.get_open_positions()
    for pos in positions:
        symbol = pos["symbol"]
        broker_pos = alpaca.broker_position_for_symbol(symbol)
        if broker_pos is None or abs(broker_pos["qty"] - pos["qty"]) > 1e-6:
            db.log("ERROR", "executor",
                   f"Could not flatten {symbol} ({reason}): Alpaca's broker-side quantity "
                   f"({broker_pos['qty'] if broker_pos else 'none'}) doesn't match this bot's tracked "
                   f"quantity ({pos['qty']}) - skipping rather than risk cancelling an order that might "
                   f"not be ours. This needs a manual look.")
            continue
        try:
            cancelled = alpaca.cancel_open_orders_for_symbol(symbol)
            if cancelled < 0:
                db.log("WARNING", "executor",
                       f"{symbol}: could not look up its resting orders before flatten - attempting the "
                       f"flatten SELL anyway, but it may fail with 'insufficient qty available' if an "
                       f"order is still holding the shares.")
            else:
                db.log("INFO", "executor", f"{symbol}: cancelled {cancelled} resting order(s) before flatten")

            # Give Alpaca a moment to register the cancellation(s) and
            # release the shares, then confirm the shares actually freed up
            # before attempting the sell - and retry the cancel once, with
            # a longer pause, if they haven't. This directly targets a
            # cancel-to-sell timing gap, one of the two live theories (the
            # other being the orders lookup itself missing something) for
            # why the first version of this fix still hit the same
            # "insufficient qty available" error the very next trading day.
            time.sleep(0.5)
            broker_pos = alpaca.broker_position_for_symbol(symbol)
            qty_available = broker_pos.get("qty_available") if broker_pos else None
            if qty_available is not None and qty_available < pos["qty"] - 1e-6:
                db.log("WARNING", "executor",
                       f"{symbol}: shares still not available {qty_available}/{pos['qty']} after cancelling "
                       f"and a 0.5s pause - retrying the cancel once with a longer pause before selling.")
                alpaca.cancel_open_orders_for_symbol(symbol)
                time.sleep(2.0)

            order_id = _client_order_id(symbol)
            alpaca.close_tracked_position(symbol, pos["qty"], order_id)
            db.record_trade(symbol, "SELL", pos["qty"], None, "MARKET", "SUBMITTED", reason)
            db.remove_position(symbol)
            db.log("WARNING", "executor", f"Flattened {symbol} ({reason})")
        except Exception as exc:
            db.log("ERROR", "executor", f"Failed to flatten {symbol}: {exc}")
