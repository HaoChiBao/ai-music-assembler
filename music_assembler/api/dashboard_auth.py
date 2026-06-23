"""Dashboard password gate — separate from API key auth."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request, Response

from music_assembler.api.config import ApiSettings

COOKIE_NAME = "assembly_dash_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _session_token(dashboard_password: str) -> str:
    return hmac.new(
        dashboard_password.encode("utf-8"),
        b"assembly-dashboard-v1",
        hashlib.sha256,
    ).hexdigest()


def has_dashboard_session(request: Request, settings: ApiSettings) -> bool:
    """True when dashboard password is unset or the browser has a valid session cookie."""
    if not settings.dashboard_password:
        return True
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    expected = _session_token(settings.dashboard_password)
    return hmac.compare_digest(token, expected)


def set_dashboard_session(response: Response, settings: ApiSettings) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=_session_token(settings.dashboard_password),
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
        path="/",
    )


def clear_dashboard_session(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")
