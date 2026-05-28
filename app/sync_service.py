from __future__ import annotations

import sys

from .db import add_sync_log, upsert_activities
from .garmin_sync import fetch_recent_garmin_runs
from .settings import settings


def run_garmin_sync(days: int = 30, notify_analysis: bool = True) -> dict:
    """Sync recent Garmin runs into SQLite. When new runs land, the AI coach
    reviews them and (by default) emails a summary; pass `notify_analysis=False`
    to save the analysis row without emailing — used by the morning job so the
    user doesn't get two emails seconds apart."""
    if not settings.garmin_email or not settings.garmin_password:
        msg = "GARMIN_EMAIL and GARMIN_PASSWORD must be set"
        add_sync_log("error", msg, 0)
        return {"status": "error", "message": msg, "imported_or_updated": 0}

    try:
        runs = fetch_recent_garmin_runs(
            email=settings.garmin_email,
            password=settings.garmin_password,
            days=days,
            mfa_code=settings.garmin_mfa_code,
        )
        changed = upsert_activities(runs)
        msg = f"Synced {len(runs)} recent runs"
        add_sync_log("ok", msg, changed)
        analysis_summary = None
        if changed > 0:
            try:
                from .run_analyzer import analyze_new_runs_and_notify

                analysis_summary = analyze_new_runs_and_notify(notify=notify_analysis)
            except Exception as exc:
                print(f"[sync_service.analyze_hook] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                add_sync_log("warn", f"analyze hook failed: {type(exc).__name__}: {exc}", 0)
                analysis_summary = None
        return {"status": "ok", "message": msg, "imported_or_updated": changed, "analyzed": analysis_summary}
    except Exception as exc:
        msg = str(exc)
        add_sync_log("error", msg, 0)
        return {"status": "error", "message": msg, "imported_or_updated": 0}
