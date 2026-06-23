"""GCP service account credentials from environment (local dev) or ADC (Cloud Run)."""

from __future__ import annotations

import os
from typing import Any

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

_CLOUD_PLATFORM_SCOPE = ("https://www.googleapis.com/auth/cloud-platform",)

# One env var per field in the GCP service account JSON key file.
_SA_ENV: dict[str, str] = {
    "type": "ASSEMBLY_GCP_SA_TYPE",
    "project_id": "ASSEMBLY_GCP_SA_PROJECT_ID",
    "private_key_id": "ASSEMBLY_GCP_SA_PRIVATE_KEY_ID",
    "private_key": "ASSEMBLY_GCP_SA_PRIVATE_KEY",
    "client_email": "ASSEMBLY_GCP_SA_CLIENT_EMAIL",
    "client_id": "ASSEMBLY_GCP_SA_CLIENT_ID",
    "auth_uri": "ASSEMBLY_GCP_SA_AUTH_URI",
    "token_uri": "ASSEMBLY_GCP_SA_TOKEN_URI",
    "auth_provider_x509_cert_url": "ASSEMBLY_GCP_SA_AUTH_PROVIDER_X509_CERT_URL",
    "client_x509_cert_url": "ASSEMBLY_GCP_SA_CLIENT_X509_CERT_URL",
    "universe_domain": "ASSEMBLY_GCP_SA_UNIVERSE_DOMAIN",
}

_SA_DEFAULTS: dict[str, str] = {
    "type": "service_account",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "universe_domain": "googleapis.com",
}

_CREDENTIALS_HINT = (
    "GCP credentials not configured. For local dev, set service account fields in .env "
    "(see .env.example — ASSEMBLY_GCP_SA_*). Required: ASSEMBLY_GCP_SA_PRIVATE_KEY and "
    "ASSEMBLY_GCP_SA_CLIENT_EMAIL. On Cloud Run, the service account is attached automatically."
)

_credentials: Any | None = None


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _normalize_private_key(value: str) -> str:
    if "\\n" in value and "\n" not in value:
        return value.replace("\\n", "\n")
    return value


def service_account_info_from_env() -> dict[str, str] | None:
    """Build a service account dict from ASSEMBLY_GCP_SA_* env vars."""
    private_key = _env(_SA_ENV["private_key"])
    client_email = _env(_SA_ENV["client_email"])
    if not private_key and not client_email:
        return None
    if not private_key or not client_email:
        raise RuntimeError(
            "Incomplete GCP service account env: set both "
            "ASSEMBLY_GCP_SA_PRIVATE_KEY and ASSEMBLY_GCP_SA_CLIENT_EMAIL."
        )

    info: dict[str, str] = {}
    for field, env_name in _SA_ENV.items():
        value = _env(env_name)
        if value:
            info[field] = value
        elif field in _SA_DEFAULTS:
            info[field] = _SA_DEFAULTS[field]

    if "project_id" not in info:
        project = _env("ASSEMBLY_GCP_PROJECT") or _env("GCP_PROJECT")
        if project:
            info["project_id"] = project

    info["private_key"] = _normalize_private_key(info["private_key"])
    return info


def get_gcp_credentials():
    """Return cached GCP credentials (env service account or application default)."""
    global _credentials
    if _credentials is not None:
        return _credentials

    info = service_account_info_from_env()
    if info is not None:
        from google.oauth2 import service_account

        _credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=_CLOUD_PLATFORM_SCOPE,
        )
        return _credentials

    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        _credentials, _ = google.auth.default(scopes=_CLOUD_PLATFORM_SCOPE)
        return _credentials
    except DefaultCredentialsError as exc:
        raise RuntimeError(_CREDENTIALS_HINT) from exc
