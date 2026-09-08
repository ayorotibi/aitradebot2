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
    MarketOrderRequest, TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.common.exceptions import APIError

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
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
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
