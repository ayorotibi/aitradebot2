"""
Single-user login: username + bcrypt-hashed password from the environment,
a signed session cookie (itsdangerous) with an expiry. Deliberately simple -
this is a personal control panel for one operator, not a multi-tenant app.

If you expose this dashboard on the public internet rather than a VPN/
private network, also put it behind HTTPS (see README) since the login
cookie is a bearer credential.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Request, HTTPException, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from common.config import Config

COOKIE_NAME = "session"
_serializer = URLSafeTimedSerializer(Config.DASHBOARD_SECRET_KEY, salt="dashboard-session")


def verify_password(username: str, password: str) -> bool:
    if username != Config.DASHBOARD_USERNAME:
        return False
    if not Config.DASHBOARD_PASSWORD_HASH:
        return False
    try:
        return bcrypt.checkpw(password.encode(), Config.DASHBOARD_PASSWORD_HASH.encode())
    except ValueError:
        return False


def create_session_token(username: str) -> str:
    return _serializer.dumps({"u": username, "iat": datetime.now(timezone.utc).isoformat()})


def verify_session_token(token: str) -> bool:
    max_age = Config.SESSION_TTL_HOURS * 3600
    try:
        _serializer.loads(token, max_age=max_age)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_login(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token or not verify_session_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
