from __future__ import annotations

from pathlib import Path

from .settings import settings


class GarminClientError(RuntimeError):
    pass


# Persisted OAuth tokens (oauth1_token.json + oauth2_token.json under this dir).
# Lives on the bind-mounted ./data volume so it survives container rebuilds.
_TOKEN_DIR = Path(__file__).resolve().parent.parent / "data" / "garmin_token"


def get_garmin_client():
    """Return a logged-in community garminconnect client.

    Tokens are cached to disk after the first SSO login so subsequent calls
    skip Garmin's login flow entirely (avoiding HTTP 429 rate-limits caused
    by re-authenticating per request). On expiry/load failure we fall back
    to a fresh SSO login and re-dump.
    """
    if not settings.garmin_email or not settings.garmin_password:
        raise GarminClientError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise GarminClientError(
            "Missing garminconnect dependency. Install with: pip install garminconnect"
        ) from exc

    client = Garmin(
        email=settings.garmin_email,
        password=settings.garmin_password,
        is_cn=False,
        prompt_mfa=lambda: settings.garmin_mfa_code or input("Garmin MFA code: "),
    )

    # garminconnect 0.3.x: login(tokenstore) both LOADS cached tokens if the
    # directory holds valid ones AND auto-persists fresh tokens to it after a
    # full SSO login (it calls the internal garth client.dump()). So a single
    # login(token_dir) call covers both the fast path (reuse tokens, no SSO)
    # and the slow path (SSO once, then cache) — no manual dump needed.
    import sys

    token_dir = str(_TOKEN_DIR)
    try:
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[garmin_client] token dir mkdir failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    client.login(token_dir)
    return client
