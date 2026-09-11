"""
Thin wrapper around alpaca-py for the two things this bot needs: an account/
position view and bracket-order execution.

IMPORTANT - this account is SHARED with another bot/strategy you already
run. Two things follow from that, both handled here rather than left as
footguns:

1. Every order carries a client_order_id starting with Config.BOT_ORDER_TAG,
   so it can always be told apart from the other bot's orders in Alpaca's
   own activity log (the approach Alpaca's own support forum recommends for
   running multiple strategies on one account - there's no account-level
   sub-account isolation for a single retail account).
2. This bot NEVER trusts "do I have a position in this symbol" from a
   simple existence check - it only trusts its own database (common/db.py).
   Before opening a new position it checks whether Alpaca already shows a
   position in that symbol; if it does and this bot's own DB didn't open
   it, that position belongs to the other bot, and this bot refuses to
   touch it (see bot/executor.py's conflict check). Closing a position
   closes exactly this bot's tracked share count via a plain order, never
   Alpaca's "close entire position" endpoint, which would also liquidate
   the other bot's shares if they hold the same symbol.
"""
import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest, GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.common.exceptions import APIError

from common.config import Config

logger = logging.getLogger("bot.alpaca_client")


class AlpacaClient:
    def __init__(self, api_key: str, secret_key: str, paper: bool):
        self.paper = paper
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)

    def is_connected(self) -> bool:
        try:
            self.trading.get_account()
            return True
        except Exception as exc:
            logger.error("Alpaca connection check failed (paper=%s): %s", self.paper, exc)
            return False

    # --- Account ---

    def account_summary(self) -> dict:
        acct = self.trading.get_account()
        return {
            "NetLiquidation": float(acct.equity),
            "TotalCashValue": float(acct.cash),
            "BuyingPower": float(acct.buying_power),
            # Alpaca tracks Pattern Day Trader status for the whole account
            # itself - both fields below are account-wide, i.e. they already
            # include whatever your other bot/strategy does. See
            # bot/risk.py's under_day_trade_limit().
            "DayTradeCount": int(getattr(acct, "daytrade_count", 0) or 0),
            "PatternDayTrader": bool(getattr(acct, "pattern_day_trader", False)),
        }

    def broker_position_for_symbol(self, symbol: str):
        """Returns the account-wide position for this symbol (which may
        belong to another bot/strategy sharing this account), or None.
        Includes qty_available - Alpaca's own count of how many of those
        shares are NOT already claimed by an open order - which
        has_live_protective_orders() uses as its primary signal."""
        try:
            pos = self.trading.get_open_position(symbol)
            qty_available = float(pos.qty_available) if pos.qty_available is not None else None
            return {
                "symbol": symbol, "qty": float(pos.qty), "avg_entry_price": float(pos.avg_entry_price),
                "qty_available": qty_available,
            }
        except APIError:
            return None  # no open position for this symbol

    def last_bracket_exit_price(self, symbol: str):
        """Used by reconcile_positions() in bot/executor.py to learn the
        fill price of whichever bracket leg (stop-loss or take-profit)
        already closed a position this bot no longer sees as open at
        Alpaca. Looks at this bot's own recent closed orders for the
        symbol (tagged with Config.BOT_ORDER_TAG, so the other strategy
        sharing this account is never touched) and returns the filled
        price of the most recently filled bracket leg, or None if it
        can't be determined (e.g. Alpaca hasn't settled the fill yet)."""
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=20, nested=True)
            orders = self.trading.get_orders(req)
        except Exception as exc:
            logger.warning("Could not fetch closed orders for %s: %s", symbol, exc)
            return None

        tagged = [o for o in orders if (getattr(o, "client_order_id", "") or "").startswith(Config.BOT_ORDER_TAG)]
        tagged.sort(key=lambda o: getattr(o, "submitted_at", None) or "", reverse=True)

        for order in tagged:
            legs = getattr(order, "legs", None) or []
            filled_legs = [
                leg for leg in legs
                if str(getattr(leg, "status", "")).lower().endswith("filled")
                and getattr(leg, "filled_avg_price", None)
            ]
            filled_legs.sort(key=lambda leg: getattr(leg, "filled_at", None) or "", reverse=True)
            if filled_legs:
                return float(filled_legs[0].filled_avg_price)
            # A plain (non-bracket) SELL this bot submitted directly, e.g. a
            # manual flatten, also counts as the closing fill.
            if (str(getattr(order, "side", "")).lower().endswith("sell")
                    and str(getattr(order, "status", "")).lower().endswith("filled")
                    and getattr(order, "filled_avg_price", None)):
                return float(order.filled_avg_price)
        return None

    # --- Market data ---

    def last_price(self, symbol: str):
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trades = self.data.get_stock_latest_trade(req)
            return float(trades[symbol].price)
        except Exception as exc:
            logger.warning("Could not fetch latest trade for %s: %s", symbol, exc)
            return None

    # --- Orders ---

    def place_bracket_order(self, symbol: str, qty: int, side: str,
                             stop_loss_price: float, take_profit_price: float,
                             client_order_id: str):
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            # GTC, not DAY: for a bracket order, time_in_force governs the
            # take-profit/stop-loss legs too, not just the entry. DAY legs
            # get cancelled at market close if unfilled, which silently
            # strips the position of its stop-loss/take-profit protection
            # the very first evening it doesn't hit either target - after
            # that it can only be closed by something noticing and acting,
            # never on its own. GTC keeps the legs live until one fills.
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)),
            client_order_id=client_order_id,
        )
        return self.trading.submit_order(order_request)

    def has_live_protective_orders(self, symbol: str, broker_pos: dict = None) -> bool:
        """True if this position still has something at Alpaca that could
        eventually close it. Used by reconcile_positions() to detect a
        position Alpaca shows as open but with nothing left protecting it
        (e.g. DAY-TIF legs that expired under the old code, before the GTC
        fix), so it can be re-armed with a fresh exit order.

        Primary signal: Alpaca's own qty_available on the position (pass
        the already-fetched broker_position_for_symbol() dict in as
        `broker_pos` to avoid a second lookup) - "total shares minus
        shares already claimed by an open order." If fewer shares are
        available than the position holds, something already has a live
        claim on them - almost certainly this bot's own bracket/OCO exit
        order. This is what actually caught a real bug: a client_order_id
        tag match (the fallback below, and originally the only check) can
        miss a live bracket's exit legs once the entry has filled and only
        the legs remain open, wrongly reporting "unprotected" and
        attempting a redundant, rejected re-arm on a position that was
        fine all along.

        Fallback: an open order tagged with this bot's client_order_id
        prefix, for when qty_available isn't available (e.g. a lookup
        failure upstream)."""
        if broker_pos is not None:
            qty_available = broker_pos.get("qty_available")
            tracked_qty = broker_pos.get("qty")
            if qty_available is not None and tracked_qty is not None and qty_available < tracked_qty - 1e-6:
                return True

        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=20, nested=True)
            orders = self.trading.get_orders(req)
        except Exception as exc:
            logger.warning("Could not fetch open orders for %s: %s", symbol, exc)
            return True  # unknown - don't cry wolf on a lookup failure
        for o in orders:
            if (getattr(o, "client_order_id", "") or "").startswith(Config.BOT_ORDER_TAG):
                return True
            for leg in (getattr(o, "legs", None) or []):
                if (getattr(leg, "client_order_id", "") or "").startswith(Config.BOT_ORDER_TAG):
                    return True
        return False

    def place_oco_exit_order(self, symbol: str, qty: float, stop_loss_price: float,
                              take_profit_price: float, client_order_id: str):
        """Attaches a fresh stop-loss/take-profit pair to a position that is
        ALREADY open - unlike place_bracket_order(), which opens a brand
        new position alongside its brackets, this only works on existing
        shares (Alpaca's OCO - one-cancels-other - order class: a linked
        limit sell and stop sell, whichever fills first cancels the
        other). Used by reconcile_positions() to re-arm a position found
        unprotected. GTC for the same reason place_bracket_order() is:
        a DAY exit order would just expire again at the next close."""
        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=abs(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)),
            client_order_id=client_order_id,
        )
        return self.trading.submit_order(order_request)

    def close_tracked_position(self, symbol: str, qty: float, client_order_id: str):
        """Closes exactly `qty` shares - this bot's own tracked amount from
        common/db.py - never the whole account position for the symbol."""
        side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=abs(qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        return self.trading.submit_order(order_request)
