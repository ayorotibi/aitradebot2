from fastapi import APIRouter, Depends

from common import db
from dashboard.auth import require_login

router = APIRouter(prefix="/api", tags=["activity"])


@router.get("/status")
def status(_=Depends(require_login)):
    rules = db.get_rules()
    hb = db.get_heartbeat()
    snapshot = db.get_latest_snapshot()
    return {
        "mode": rules["mode"],
        "kill_switch": rules["kill_switch"],
        "broker_connected": bool(hb["broker_connected"]) if hb else False,
        "last_heartbeat": hb["ts"] if hb else None,
        "equity": snapshot["equity"] if snapshot else None,
        "daily_pnl": snapshot["daily_pnl"] if snapshot else None,
        "open_positions": len(db.get_open_positions()),
        "day_trade_count": snapshot["day_trade_count"] if snapshot else None,
        "pattern_day_trader": bool(snapshot["pattern_day_trader"]) if snapshot and snapshot["pattern_day_trader"] is not None else None,
    }


@router.get("/trades")
def trades(limit: int = 200, _=Depends(require_login)):
    return db.get_recent_trades(limit)


@router.get("/positions")
def positions(_=Depends(require_login)):
    return db.get_open_positions()


@router.get("/scans")
def scans(limit: int = 200, _=Depends(require_login)):
    return db.get_recent_scans(limit)


@router.get("/logs")
def logs(limit: int = 200, _=Depends(require_login)):
    return db.get_recent_logs(limit)
