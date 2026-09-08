"""
Optional Telegram notifications, matching the article's alerting approach.
Skips gracefully (logs only) if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not
set, so this is safe to leave unconfigured.
"""
import logging

import requests

from common.config import Config

logger = logging.getLogger("bot.notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(message: str):
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured, skipping notification: %s", message)
        return
    try:
        resp = requests.post(
            API_URL.format(token=Config.TELEGRAM_BOT_TOKEN),
            json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Telegram notification failed (%s): %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Telegram notification error: %s", exc)
