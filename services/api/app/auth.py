from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from pathlib import Path

from fastapi import HTTPException, Request, status

from app.config import Settings
from app.database import Database, local_session_secret_path

SESSION_COOKIE = "omnitutor_session"
SESSION_TOKEN_PATTERN = re.compile(r"^([^.]+)\.(\d+)\.([A-Za-z0-9_-]+)$")


def _session_secret(settings: Settings) -> str:
    secret = settings.session_secret.strip() if settings.session_secret else None
    if not secret:
        path: Path = local_session_secret_path()
        try:
            secret = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "auth_not_configured", "message": "Shared session verification is not configured."},
            ) from error
        except OSError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "auth_not_configured", "message": "Shared session verification is unavailable."},
            ) from error
    if len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_not_configured", "message": "Shared session verification is not configured."},
        )
    return secret


def verified_learner_id(token: str | None, settings: Settings) -> str:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "Sign in to search this notebook."},
        )
    match = SESSION_TOKEN_PATTERN.fullmatch(token)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "The session is invalid or expired."},
        )
    learner_id, expiry, signature = match.groups()
    payload = f"{learner_id}.{expiry}".encode()
    expected = hmac.new(_session_secret(settings).encode(), payload, hashlib.sha256).digest()
    supplied = signature.encode()
    try:
        supplied_bytes = base64.urlsafe_b64decode(supplied + b"=" * (-len(supplied) % 4))
    except (ValueError, binascii.Error):
        supplied_bytes = b""
    if (
        not hmac.compare_digest(expected, supplied_bytes)
        or base64.urlsafe_b64encode(supplied_bytes).rstrip(b"=").decode() != signature
        or int(expiry) <= int(time.time())
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "The session is invalid or expired."},
        )
    return learner_id


async def require_authenticated_learner(request: Request) -> str:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(SESSION_COOKIE)
    learner_id = verified_learner_id(token, settings)
    database: Database = request.app.state.database
    if not await database.is_private_learner(learner_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "The session is invalid or expired."},
        )
    return learner_id
