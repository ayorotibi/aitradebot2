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
    MarketOrderRequest, TakeProfitRequest, StopLossRequest, GetOrdersRequest,
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
        belong to another bot/strategy sharing this account), or None."""
        try:
            pos = self.trading.get_open_position(symbol)
            return {"symbol": symbol, "qty": float(pos.qty), "avg_entry_price": float(pos.avg_entry_price)}
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

    def has_live_protective_orders(self, symbol: str) -> bool:
        """True if this bot has at least one open (unfilled, uncancelled)
        order at Alpaca for `symbol` - i.e. a bracket leg still capable of
        eventually closing the position. Used by reconcile_positions() to
        flag a position that Alpaca still shows as open but that has
        nothing left that could ever close it (e.g. DAY-TIF legs that
        already expired under the old code, before the GTC fix above)."""
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=20, nested=True)
            orders = self.trading.get_orders(req)
        except Exception as exc:
            logger.warning("Could not fetch open orders for %s: %s", symbol, exc)
            return True  # unknown - don't cry wolf on a lookup failure
        return any((getattr(o, "client_order_id", "") or "").startswith(Config.BOT_ORDER_TAG) for o in orders)

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
