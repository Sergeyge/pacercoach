from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from .settings import settings

API_KEY_HEADER = "X-API-Key"

# Paths served without auth: liveness check and the API docs (which expose only
# the schema, not data). Everything else requires the API key.
# The dashboard shell is public (it contains no data — every API call it makes
# still carries the X-API-Key the user enters).
_OPEN_PATHS = {"/", "/dashboard", "/health", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}

_api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


async def require_api_key(
    request: Request,
    api_key: str | None = Security(_api_key_header),
) -> None:
    """Global dependency enforcing an X-API-Key header on protected endpoints.

    Fails closed: if no API_KEY is configured on the server, every protected
    request is rejected so the app is never accidentally left open.
    """
    if request.url.path in _OPEN_PATHS:
        return
    if not settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server auth is not configured. Set API_KEY in the environment.",
        )
    if not api_key or not secrets.compare_digest(api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {API_KEY_HEADER} header.",
        )
