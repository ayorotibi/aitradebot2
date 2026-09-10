"""
Postgres access layer shared by the bot and dashboard. Both connect to the
same DATABASE_URL over the network - unlike the local-VPS/SQLite version of
this project, the bot and dashboard here are separate services (e.g.
separate Render services) with no shared disk between them, so the database
IS the shared state, not a file.

Function signatures deliberately match the SQLite version this replaced, so
nothing above this module (bot/*.py, dashboard/*.py) needed to change.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

from common.config import Config
from common.rules_schema import DEFAULT_RULES, merge_with_defaults

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL,
    order_type TEXT,
    status TEXT,
    reason TEXT,
    pnl REAL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    stop_loss REAL,
    take_profit REAL
);

CREATE TABLE IF NOT EXISTS scan_results (
    id SERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    gap_pct REAL,
    price REAL,
    volume REAL,
    passed INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS system_log (
    id SERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_snapshot (
    id SERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    equity REAL,
    cash REAL,
    daily_pnl REAL,
    mode TEXT,
    day_trade_count INTEGER,
    pattern_day_trader INTEGER
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT NOT NULL,
    broker_connected INTEGER NOT NULL,
    mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    job_name TEXT PRIMARY KEY,
    last_run_date TEXT NOT NULL
);
"""

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not Config.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. This app requires a Postgres database - "
                "see .env.example / render.yaml."
            )
        _pool = psycopg2.pool.SimpleConnectionPool(
            1, 5, dsn=Config.DATABASE_URL, sslmode=Config.PGSSLMODE,
            cursor_factory=RealDictCursor,
        )
    return _pool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("SELECT data FROM rules WHERE id = 1")
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO rules (id, data, updated_at) VALUES (1, %s, %s)",
                    (json.dumps(DEFAULT_RULES), _now()),
                )


# --- Rules ---

def get_rules() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM rules WHERE id = 1")
            row = cur.fetchone()
            stored = json.loads(row["data"]) if row else {}
            return merge_with_defaults(stored)


def set_rules(rules: dict) -> dict:
    merged = merge_with_defaults(rules)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rules SET data = %s, updated_at = %s WHERE id = 1",
                (json.dumps(merged), _now()),
            )
    return merged


# --- Logging ---

def log(level: str, source: str, message: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_log (ts, level, source, message) VALUES (%s, %s, %s, %s)",
                (_now(), level.upper(), source, message),
            )


def get_recent_logs(limit: int = 200):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM system_log ORDER BY id DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


# --- Trades ---

def record_trade(symbol, side, qty, price, order_type, status, reason, pnl=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trades (ts, symbol, side, qty, price, order_type, status, reason, pnl)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (_now(), symbol, side, qty, price, order_type, status, reason, pnl),
            )


def get_recent_trades(limit: int = 200):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


def get_today_pnl() -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE ts LIKE %s",
                (f"{today}%",),
            )
            row = cur.fetchone()
            return float(row["total"]) if row else 0.0


# --- Positions ---

def upsert_position(symbol, qty, avg_price, stop_loss=None, take_profit=None):
    """Opens a new tracked position, or - if one is somehow already open for
    this symbol (shouldn't happen now that executor.py skips a BUY signal
    for a symbol it already holds, but kept defensive) - adds to it rather
    than silently overwriting qty/avg_price and losing track of the earlier
    shares. stop_loss/take_profit from the newest fill win, since those
    apply to the most recently submitted bracket order."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO positions (symbol, qty, avg_price, opened_at, stop_loss, take_profit)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (symbol) DO UPDATE SET
                     qty = positions.qty + EXCLUDED.qty,
                     avg_price = ((positions.qty * positions.avg_price) + (EXCLUDED.qty * EXCLUDED.avg_price))
                                 / NULLIF(positions.qty + EXCLUDED.qty, 0),
                     stop_loss = EXCLUDED.stop_loss, take_profit = EXCLUDED.take_profit""",
                (symbol, qty, avg_price, _now(), stop_loss, take_profit),
            )


def remove_position(symbol):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM positions WHERE symbol = %s", (symbol,))


def get_open_positions():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM positions ORDER BY symbol")
            return [dict(r) for r in cur.fetchall()]


# --- Scans ---

def record_scan(symbol, gap_pct, price, volume, passed, notes=""):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO scan_results (ts, symbol, gap_pct, price, volume, passed, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (_now(), symbol, gap_pct, price, volume, int(passed), notes),
            )


def get_recent_scans(limit: int = 200):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scan_results ORDER BY id DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


# --- Account snapshots ---

def record_snapshot(equity, cash, daily_pnl, mode, day_trade_count=None, pattern_day_trader=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO account_snapshot
                   (ts, equity, cash, daily_pnl, mode, day_trade_count, pattern_day_trader)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (_now(), equity, cash, daily_pnl, mode, day_trade_count,
                 None if pattern_day_trader is None else int(pattern_day_trader)),
            )


def get_latest_snapshot():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM account_snapshot ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else None


# --- Heartbeat ---

def set_heartbeat(broker_connected: bool, mode: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO heartbeat (id, ts, broker_connected, mode) VALUES (1, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET ts = EXCLUDED.ts,
                     broker_connected = EXCLUDED.broker_connected, mode = EXCLUDED.mode""",
                (_now(), int(broker_connected), mode),
            )


def get_heartbeat():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM heartbeat WHERE id = 1")
            row = cur.fetchone()
            return dict(row) if row else None


# --- Job run tracking ---
# Persists which date each one-shot daily job (premarket scan, EOD summary)
# last ran on. bot/scheduler.py's DailyJobTracker keeps this in memory too
# (to avoid a DB round trip on every 15s poll) but reads/writes through here
# so that a Render redeploy - which wipes in-memory state - doesn't make the
# scheduler think a job hasn't run yet today when it already has. Without
# this, a redeploy any time after premarket_scan_time re-runs the premarket
# scan mid-day, overwriting that morning's real candidate list with
# whatever a same-day-but-hours-later "gap" scan finds (often much smaller
# gaps than at the open, sometimes none) - which starves the rest of that
# day's trading cycles of anything to buy.

def get_job_last_run_date(job_name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_run_date FROM job_runs WHERE job_name = %s", (job_name,))
            row = cur.fetchone()
            return row["last_run_date"] if row else None


def set_job_last_run_date(job_name: str, date_str: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO job_runs (job_name, last_run_date) VALUES (%s, %s)
                   ON CONFLICT (job_name) DO UPDATE SET last_run_date = EXCLUDED.last_run_date""",
                (job_name, date_str),
            )
