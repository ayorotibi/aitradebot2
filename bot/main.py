"""
Bot entrypoint. Alpaca's API is plain REST/HTTPS, not a persistent socket
like IBKR's TWS/Gateway protocol - so there's no "connection" to keep alive
or pump an event loop for. Each job just builds a fresh AlpacaClient scoped
to the currently-effective mode (paper/live) and makes its calls.
"""
import logging
import sys
import time

from common import db
from common.config import Config
from bot.alpaca_client import AlpacaClient
from bot.scanner import run_premarket_scan
from bot.strategy import evaluate_gap_momentum, evaluate_breakout
from bot.executor import execute_signal, reconcile_positions
from bot import notifier, risk, scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bot.main")

_last_candidates = []


def get_client(rules: dict) -> AlpacaClient:
    mode = Config.effective_mode(rules["mode"])
    if mode == "live":
        api_key, secret_key = Config.ALPACA_LIVE_API_KEY, Config.ALPACA_LIVE_SECRET_KEY
    else:
        api_key, secret_key = Config.ALPACA_PAPER_API_KEY, Config.ALPACA_PAPER_SECRET_KEY

    if not api_key or not secret_key:
        db.log("ERROR", "main", f"No Alpaca API key/secret configured for mode={mode} - check .env")

    return AlpacaClient(api_key, secret_key, paper=(mode == "paper"))


def on_premarket_scan(rules: dict):
    global _last_candidates
    db.log("INFO", "main", "Running premarket scan...")
    _last_candidates = run_premarket_scan(rules)
    if rules["notifications"]["notify_on_trade"]:
        symbols = ", ".join(c["symbol"] for c in _last_candidates[:10]) or "none"
        notifier.send(f"📋 Premarket scan: {len(_last_candidates)} candidates. Top: {symbols}")


def on_trading_cycle(rules: dict):
    alpaca = get_client(rules)
    connected = alpaca.is_connected()
    db.set_heartbeat(connected, Config.effective_mode(rules["mode"]))
    if not connected:
        db.log("ERROR", "main", "Could not reach Alpaca this cycle - skipping")
        return

    # Sync our tracked positions against Alpaca's real ones first, every
    # cycle - a bracket order's stop-loss/take-profit leg can close a
    # position on its own with nothing else telling this bot about it.
    # This also runs while the kill switch is on ("still monitoring"),
    # since it only reflects reality rather than placing any new orders.
    reconcile_positions(alpaca, rules)

    if rules.get("kill_switch"):
        db.log("INFO", "main", "Kill switch engaged - trading cycle skipped (still monitoring)")
        return

    if risk.check_daily_loss_kill_switch(rules):
        return  # kill switch just tripped this cycle; don't also trade

    account = alpaca.account_summary()
    equity = account.get("NetLiquidation")
    if equity:
        db.record_snapshot(
            equity, account.get("TotalCashValue"), db.get_today_pnl(), rules["mode"],
            day_trade_count=account.get("DayTradeCount"),
            pattern_day_trader=account.get("PatternDayTrader"),
        )

    for candidate in _last_candidates:
        signal = evaluate_gap_momentum(candidate, rules)
        if signal["action"] == "BUY":
            execute_signal(alpaca, rules, signal)

    if rules["strategies"]["breakout"]["enabled"]:
        for candidate in _last_candidates:
            signal = evaluate_breakout(candidate["symbol"], rules)
            if signal["action"] == "BUY":
                execute_signal(alpaca, rules, signal)


def on_eod_summary(rules: dict):
    pnl = db.get_today_pnl()
    positions = db.get_open_positions()
    db.log("INFO", "main", f"EOD summary: pnl={pnl:.2f}, open_positions={len(positions)}")
    if rules["notifications"]["notify_eod_summary"]:
        notifier.send(f"📊 EOD summary: PnL {pnl:+.2f}, {len(positions)} open position(s)")


def rules_provider() -> dict:
    return db.get_rules()


if __name__ == "__main__":
    db.init_db()
    rules = db.get_rules()
    effective = Config.effective_mode(rules["mode"])
    db.log("INFO", "main", f"Bot starting - rules.mode={rules['mode']}, effective mode={effective} "
                            f"(ALLOW_LIVE_TRADING={Config.ALLOW_LIVE_TRADING})")

    startup_client = get_client(rules)
    db.set_heartbeat(startup_client.is_connected(), effective)

    scheduler.run_loop(
        rules_provider=rules_provider,
        on_premarket_scan=on_premarket_scan,
        on_trading_cycle=on_trading_cycle,
        on_eod_summary=on_eod_summary,
        sleep_fn=time.sleep,
    )
