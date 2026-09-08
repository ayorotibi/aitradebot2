from fastapi import APIRouter, Depends, HTTPException

from common import db
from common.config import Config
from dashboard.auth import require_login

router = APIRouter(prefix="/api", tags=["control"])


@router.post("/kill-switch")
def set_kill_switch(payload: dict, _=Depends(require_login)):
    enabled = bool(payload.get("enabled", True))
    rules = db.get_rules()
    rules["kill_switch"] = enabled
    db.set_rules(rules)
    db.log("WARNING", "dashboard", f"Kill switch set to {enabled} from dashboard")
    return {"kill_switch": enabled}


@router.post("/mode")
def set_mode(payload: dict, _=Depends(require_login)):
    mode = payload.get("mode")
    if mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be 'paper' or 'live'")

    if mode == "live":
        if payload.get("confirm") != "CONFIRM":
            raise HTTPException(
                400,
                "Switching to live trading requires {\"confirm\": \"CONFIRM\"} in the request body.",
            )
        if not Config.ALLOW_LIVE_TRADING:
            raise HTTPException(
                403,
                "ALLOW_LIVE_TRADING is not set to true in the server's .env file. "
                "This is a deliberate second gate the dashboard cannot override - "
                "edit .env on the host and restart the bot container to enable live trading.",
            )

    rules = db.get_rules()
    rules["mode"] = mode
    db.set_rules(rules)
    db.log("WARNING", "dashboard", f"Trading mode set to {mode} from dashboard")
    return {"mode": mode}
