"""
Shared configuration loader. Both the bot and dashboard containers import this.
All values come from environment variables (see .env.example at the project root).
"""
import os


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    val = os.environ.get(name)
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


class Config:
    # --- Shared database ---
    # A real network-reachable Postgres, not a local file - this bot and the
    # dashboard run as separate processes/containers/services (e.g. two
    # separate Render services) that can't share a local disk. On Render,
    # DATABASE_URL is injected automatically from the attached Postgres
    # instance (see render.yaml). Locally, docker-compose.yml points this at
    # the bundled postgres service.
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    PGSSLMODE = os.environ.get("PGSSLMODE", "require")

    # --- Alpaca ---
    # This bot shares one Alpaca account with any other bot/strategy you run.
    # Use a SEPARATE API key pair for this bot (generate one in Alpaca's
    # dashboard - it takes 30 seconds and needs no new account) so the two
    # bots' credentials can be told apart and individually revoked.
    ALPACA_PAPER_API_KEY = os.environ.get("ALPACA_PAPER_API_KEY", "")
    ALPACA_PAPER_SECRET_KEY = os.environ.get("ALPACA_PAPER_SECRET_KEY", "")
    ALPACA_LIVE_API_KEY = os.environ.get("ALPACA_LIVE_API_KEY", "")
    ALPACA_LIVE_SECRET_KEY = os.environ.get("ALPACA_LIVE_SECRET_KEY", "")

    # Every order this bot places carries a client_order_id starting with
    # this tag, so it - and only it - can be told apart from your other
    # bot's orders when you look at the account's activity in Alpaca itself.
    BOT_ORDER_TAG = os.environ.get("BOT_ORDER_TAG", "aitradingbot")

    # --- Telegram (optional) ---
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    # --- Dashboard auth ---
    DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
    # bcrypt hash of the dashboard password - generate with:
    #   python -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
    DASHBOARD_PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    DASHBOARD_SECRET_KEY = os.environ.get("DASHBOARD_SECRET_KEY", "change-me-to-a-random-secret")
    SESSION_TTL_HOURS = _int("SESSION_TTL_HOURS", 12)

    # --- Timezone ---
    MARKET_TIMEZONE = os.environ.get("MARKET_TIMEZONE", "America/New_York")

    # --- Safety ---
    # A second, explicit gate on top of rules.json's "mode" field. Both must
    # say "live" before the bot will connect with live keys and place
    # real-money orders. This env var requires a deliberate deploy-time
    # decision, not just a dashboard click.
    ALLOW_LIVE_TRADING = _bool("ALLOW_LIVE_TRADING", False)

    @classmethod
    def effective_mode(cls, rules_mode: str) -> str:
        """The mode the bot will actually trade in, after applying the
        ALLOW_LIVE_TRADING gate. rules_mode can say "live" all it wants from
        the dashboard - this always falls back to "paper" unless the host's
        .env also explicitly allows it."""
        return "live" if (rules_mode == "live" and cls.ALLOW_LIVE_TRADING) else "paper"
