"""Authentication (bcrypt + JWT) and role-based access control.

Roles:
- clinician: can list, view and query only the patients assigned to them in patient_access.
- auditor:   can read the audit log; no access to patient data (minimum necessary).
"""
import os
import time
from typing import FrozenSet

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import text

from src.api.db import get_db_engine

JWT_ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 8 * 60 * 60  # one clinical shift

_bearer = HTTPBearer(auto_error=False)


class User(BaseModel):
    username: str
    role: str
    patient_ids: FrozenSet[int] = frozenset()


def _secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set. Add it to .env (see docs/CHANGELOG.md).")
    return secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def authenticate(username: str, password: str):
    """Returns (username, role) on success, None otherwise."""
    with get_db_engine().connect() as conn:
        row = conn.execute(
            text("SELECT username, password_hash, role FROM app_users WHERE username = :u"), {"u": username}
        ).mappings().first()
    if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return row["username"], row["role"]
    return None


def create_token(username: str, role: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + TOKEN_TTL_SECONDS},
                      _secret(), algorithm=JWT_ALGORITHM)


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = jwt.decode(credentials.credentials, _secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    # Re-read role and assignments on every request, so revoking access takes effect immediately
    with get_db_engine().connect() as conn:
        role = conn.execute(text("SELECT role FROM app_users WHERE username = :u"), {"u": claims["sub"]}).scalar()
        if role is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
        patient_ids = frozenset(
            r[0] for r in conn.execute(text("SELECT subject_id FROM patient_access WHERE username = :u"),
                                       {"u": claims["sub"]})
        )
    return User(username=claims["sub"], role=role, patient_ids=patient_ids)


def require_role(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role '{user.role}' cannot access this resource")
        return user
    return dependency
