from __future__ import annotations

from typing import Any

from .garmin_client import get_garmin_client


def fetch_last_splits() -> dict[str, Any]:
    """Per-km/lap splits of the most recent stored run."""
    from .db import list_runs

    runs = list_runs(limit=1)
    if not runs:
        return {"error": "no runs synced yet"}
    activity_id = runs[0]["source_id"]
    client = get_garmin_client()
    try:
        data = client.get_activity_splits(activity_id)
    except Exception as exc:
        return {"error": f"could not fetch splits: {exc}"}

    laps = (data.get("lapDTOs") or data.get("splits") or []) if isinstance(data, dict) else []
    out = []
    for i, lap in enumerate(laps, 1):
        dist = lap.get("distance")  # meters
        dur = lap.get("duration") or lap.get("elapsedDuration")  # seconds
        hr = lap.get("averageHR")
        pace = round(dur / (dist / 1000)) if dist and dur else None
        out.append(
            {
                "lap": i,
                "distance_km": round((dist or 0) / 1000, 2),
                "duration_sec": round(dur) if dur else None,
                "pace_sec": pace,
                "avg_hr": int(hr) if hr else None,
            }
        )
    return {"activity_id": activity_id, "date": runs[0]["activity_date"], "laps": out}


def _profile_number(client) -> Any:
    for getter in (lambda: client.get_user_profile(), lambda: getattr(client, "profile", None)):
        try:
            prof = getter()
        except Exception:
            continue
        if isinstance(prof, dict):
            for k in ("userProfileId", "profileId", "id", "userProfileNumber"):
                if prof.get(k):
                    return prof[k]
    return None


def fetch_gear() -> dict[str, Any]:
    """Shoe/gear list with total distance + a replace warning near 600–800 km."""
    client = get_garmin_client()
    upn = _profile_number(client)
    if upn is None:
        return {"error": "could not determine Garmin user profile id for gear"}
    try:
        gear = client.get_gear(upn) or []
    except Exception as exc:
        return {"error": f"could not fetch gear: {exc}"}

    out = []
    for g in gear:
        uuid = g.get("uuid") or g.get("gearPk")
        name = g.get("displayName") or g.get("customMakeModel") or g.get("gearMakeName") or "gear"
        total_m = None
        try:
            stats = client.get_gear_stats(uuid) or {}
            total_m = stats.get("totalDistance")
        except Exception:
            pass
        if total_m is None:
            total_m = g.get("totalDistance")
        km = round((total_m or 0) / 1000, 1)
        retired = (g.get("gearStatusName") or "").lower() == "retired"
        warn = (not retired) and km >= 600
        out.append({"name": name, "km": km, "retired": retired, "replace_soon": warn})
    out.sort(key=lambda x: x["km"], reverse=True)
    return {"gear": out}
