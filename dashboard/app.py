"""
Dashboard entrypoint. Serves the JSON API (routers/) plus the static
single-page frontend, and handles login/logout directly (kept out of a
router since it's the one set of routes that must NOT require login).
"""
from fastapi import FastAPI, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from common import db
from common.config import Config
from dashboard import auth
from dashboard.routers import rules, activity, control

app = FastAPI(title="AI Trading Bot Dashboard")

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def startup():
    db.init_db()


@app.post("/api/login")
def login(payload: dict, response: Response):
    username = payload.get("username", "")
    password = payload.get("password", "")
    if not auth.verify_password(username, password):
        raise HTTPException(401, "Invalid username or password")
    token = auth.create_session_token(username)
    response.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=Config.SESSION_TTL_HOURS * 3600,
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


app.include_router(rules.router)
app.include_router(activity.router)
app.include_router(control.router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
