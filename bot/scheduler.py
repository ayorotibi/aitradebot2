"""
A deliberately simple time-window scheduler instead of APScheduler: one
loop, checked every 15s, that compares the current time (in the configured
market timezone) against each scheduled job and runs a job at most once per
day. Nothing here depends on the broker client, so `sleep_fn` can be plain
`time.sleep` (as bot/main.py uses it) - this was written with room for a
broker client that needs its own event loop pumped instead, which Alpaca's
plain REST API doesn't require, but a future broker integration might.

Replaces the article's 11 Windows Task Scheduler jobs with 3: premarket
scan, a repeating trading cycle during the trading window, and an EOD
summary - all times are dashboard-editable via rules["schedule"].
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from common import db

logger = logging.getLogger("bot.scheduler")


def _parse_hhmm(value: str):
    h, m = value.split(":")
    return int(h), int(m)


class DailyJobTracker:
    """Tracks which one-shot jobs (premarket scan, EOD summary) have already
    run today so they fire exactly once even though the loop polls every
    15 seconds.

    Backed by common.db (the job_runs table), not just this process's
    memory: a bare in-memory tracker forgets everything on a redeploy, so a
    restart any time after premarket_scan_time would make the bot think
    the premarket scan hadn't run yet today and re-run it - clobbering the
    morning's real candidate list with a same-day-but-later "gap" scan
    (usually far fewer or zero real candidates, since intraday moves often
    fade from their open). The in-memory dict is kept purely so a live,
    long-running process doesn't hit the database on every 15-second poll
    once today's run is already recorded."""

    def __init__(self):
        self._last_run_date = {}

    def should_run_once(self, job_name: str, today: date) -> bool:
        if self._last_run_date.get(job_name) == today:
            return False
        persisted = db.get_job_last_run_date(job_name)
        if persisted == today.isoformat():
            self._last_run_date[job_name] = today  # warm the cache, skip future DB checks today
            return False
        return True

    def mark_run(self, job_name: str, today: date):
        self._last_run_date[job_name] = today
        db.set_job_last_run_date(job_name, today.isoformat())


def run_loop(rules_provider, on_premarket_scan, on_trading_cycle, on_eod_summary,
             sleep_fn, poll_seconds: int = 15):
    """
    rules_provider: () -> dict, called every iteration so dashboard edits
        take effect without a bot restart.
    on_*: callables invoked at the right time.
    sleep_fn: called between polls - plain time.sleep for a REST-based
        broker client like Alpaca's.
    """
    tracker = DailyJobTracker()
    last_cycle_run = None

    while True:
        rules = rules_provider()
        sched = rules["schedule"]
        tz = ZoneInfo(sched.get("timezone", "America/New_York"))
        now = datetime.now(tz)
        today = now.date()

        premarket_h, premarket_m = _parse_hhmm(sched["premarket_scan_time"])
        eod_h, eod_m = _parse_hhmm(sched["eod_summary_time"])
        window_start_h, window_start_m = _parse_hhmm(sched["trading_window_start"])
        window_end_h, window_end_m = _parse_hhmm(sched["trading_window_end"])

        # Weekdays only.
        if now.weekday() < 5:
            if (now.hour, now.minute) >= (premarket_h, premarket_m) and tracker.should_run_once("premarket", today):
                try:
                    on_premarket_scan(rules)
                except Exception:
                    logger.exception("premarket scan job failed")
                tracker.mark_run("premarket", today)

            in_window = (window_start_h, window_start_m) <= (now.hour, now.minute) <= (window_end_h, window_end_m)
            if in_window:
                interval = sched.get("cycle_interval_minutes", 5)
                if last_cycle_run is None or (now - last_cycle_run).total_seconds() >= interval * 60:
                    try:
                        on_trading_cycle(rules)
                    except Exception:
                        logger.exception("trading cycle job failed")
                    last_cycle_run = now

            if (now.hour, now.minute) >= (eod_h, eod_m) and tracker.should_run_once("eod", today):
                try:
                    on_eod_summary(rules)
                except Exception:
                    logger.exception("EOD summary job failed")
                tracker.mark_run("eod", today)

        sleep_fn(poll_seconds)
