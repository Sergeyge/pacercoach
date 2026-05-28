from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .models import RunActivity


def _to_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _duration_seconds(activity: dict[str, Any]) -> int:
    for key in ("duration", "elapsedDuration", "movingDuration"):
        value = activity.get(key)
        if value:
            return int(float(value))
    return 0


def _distance_km(activity: dict[str, Any]) -> float:
    for key in ("distance", "distanceMeters"):
        value = activity.get(key)
        if value:
            # python-garminconnect usually returns meters.
            return round(float(value) / 1000, 3)
    return 0.0


def _activity_date(activity: dict[str, Any]) -> date:
    for key in ("startTimeLocal", "startTimeGMT", "beginTimestamp", "activityDate"):
        if activity.get(key):
            return _to_date(activity[key])
    return date.today()


def _activity_type(activity: dict[str, Any]) -> str:
    t = activity.get("activityType") or activity.get("type") or {}
    if isinstance(t, dict):
        return str(t.get("typeKey") or t.get("typeId") or t.get("displayName") or "unknown")
    return str(t)


def normalize_garmin_activity(activity: dict[str, Any]) -> RunActivity | None:
    activity_type = _activity_type(activity)
    if "run" not in activity_type.lower():
        return None

    distance_km = _distance_km(activity)
    duration_seconds = _duration_seconds(activity)
    if distance_km <= 0 or duration_seconds <= 0:
        return None

    source_id = str(activity.get("activityId") or activity.get("id") or f"garmin-{_activity_date(activity)}-{distance_km}-{duration_seconds}")

    avg_hr = activity.get("averageHR") or activity.get("avgHr") or activity.get("averageHeartRate")
    calories = activity.get("calories")

    return RunActivity(
        source_id=source_id,
        activity_date=_activity_date(activity),
        activity_type=activity_type,
        distance_km=distance_km,
        duration_seconds=duration_seconds,
        avg_hr=int(avg_hr) if avg_hr else None,
        avg_pace_sec_per_km=round(duration_seconds / distance_km) if distance_km else None,
        calories=int(calories) if calories else None,
    )


class GarminSyncError(RuntimeError):
    pass


def fetch_recent_garmin_runs(email: str, password: str, days: int = 30, mfa_code: str | None = None) -> list[RunActivity]:
    """
    Prototype connector using the community python-garminconnect package.
    For production/commercial usage, replace this with Garmin's official OAuth API.
    """
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise GarminSyncError("Missing optional dependency. Install with: pip install garminconnect") from exc

    try:
        client = Garmin(email=email, password=password, is_cn=False, prompt_mfa=lambda: mfa_code or input("Garmin MFA code: "))
        client.login()
        raw = client.get_activities(0, 100)
    except Exception as exc:
        raise GarminSyncError(f"Garmin login/sync failed: {exc}") from exc

    cutoff = date.today() - timedelta(days=days)
    runs: list[RunActivity] = []
    for activity in raw:
        run = normalize_garmin_activity(activity)
        if run and run.activity_date >= cutoff:
            runs.append(run)
    return runs
