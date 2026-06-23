"""API authentication — API key (scripts) or dashboard session cookie (browser)."""

from __future__ import annotations

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from music_assembler.api.config import ApiSettings
from music_assembler.api.dashboard_auth import has_dashboard_session

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_prefix = "Bearer "


def _extract_api_key(request: Request, header_key: str | None) -> str | None:
    token = header_key
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith(_bearer_prefix):
            token = auth[len(_bearer_prefix) :].strip()
    return token.strip() if token else None


def require_api_key(
    request: Request,
    header_key: str | None = Security(_api_key_header),
) -> None:
    """Require ``X-API-Key`` when ``ASSEMBLY_API_KEY`` is configured."""
    settings = ApiSettings.from_env()
    if not settings.api_key:
        return
    token = _extract_api_key(request, header_key)
    if token != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_api_auth(
    request: Request,
    header_key: str | None = Security(_api_key_header),
) -> None:
    """Allow ``X-API-Key`` **or** a valid dashboard session cookie.

    Use on ``/v1/*`` routes so the browser dashboard works after password login
    without pasting ``ASSEMBLY_API_KEY``. Scripts and curl still use the header.
    """
    settings = ApiSettings.from_env()
    if not settings.api_key and not settings.dashboard_password:
        return
    if settings.dashboard_password and has_dashboard_session(request, settings):
        return
    if settings.api_key:
        token = _extract_api_key(request, header_key)
        if token == settings.api_key:
            return
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    # Dashboard password set but no API key — only session cookie can auth.
    raise HTTPException(status_code=401, detail="Dashboard session required")
