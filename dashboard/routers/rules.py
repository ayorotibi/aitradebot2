from fastapi import APIRouter, Depends

from common import db
from dashboard.auth import require_login

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("")
def get_rules(_=Depends(require_login)):
    return db.get_rules()


@router.put("")
def update_rules(new_rules: dict, _=Depends(require_login)):
    saved = db.set_rules(new_rules)
    db.log("INFO", "dashboard", "Rules updated from dashboard")
    return saved
