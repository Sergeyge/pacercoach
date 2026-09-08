from __future__ import annotations

from typing import Any

from .garmin_client import get_garmin_client


def fetch_activity_splits(activity_id: Any, client: Any | None = None) -> dict[str, Any]:
    """Per-lap splits of one activity.

    `client` lets a caller reuse one logged-in Garmin client across several
    activities instead of paying for a login per call. Garmin's own per-lap
    `intensityType` is carried through: on a run that followed a structured
    workout it marks warm-up / work / recovery / cool-down laps, which is what
    lets a review judge the work reps instead of the blended whole-run average.
    """
    if client is None:
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
                # 3 dp, not 2: a 20-second stride lap is ~80 m, and rounding that
                # to 0.08 km skews any pace averaged over the reps by several
                # percent. The dashboard formats it back down for display.
                "distance_km": round((dist or 0) / 1000, 3),
                "duration_sec": round(dur) if dur else None,
                "pace_sec": pace,
                "avg_hr": int(hr) if hr else None,
                "intensity_type": lap.get("intensityType"),
            }
        )
    return {"activity_id": activity_id, "laps": out}


def fetch_last_splits() -> dict[str, Any]:
    """Per-km/lap splits of the most recent stored run."""
    from .db import list_runs

    runs = list_runs(limit=1)
    if not runs:
        return {"error": "no runs synced yet"}
    out = fetch_activity_splits(runs[0]["source_id"])
    if "error" not in out:
        out["date"] = runs[0]["activity_date"]
    return out


def backfill_activity_laps(limit: int = 25) -> dict[str, Any]:
    """Fetch and store laps for past runs that have none, newest first.

    New activities get their laps stored during sync (the review already fetches
    them), so this is only for history predating that. Deliberately bounded and
    manually triggered rather than run on a schedule: it costs one Garmin call
    per activity, and a few hundred in a burst is exactly what earns the HTTP 429
    that `garmin_client` caches tokens to avoid. Call it repeatedly to work back
    through the history.
    """
    from .db import activities_missing_laps, lap_coverage, upsert_activity_laps

    pending = activities_missing_laps(limit=max(1, min(limit, 100)))
    if not pending:
        return {"status": "ok", "fetched": 0, "message": "every run already has laps", "coverage": lap_coverage()}

    try:
        client = get_garmin_client()
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200], "coverage": lap_coverage()}

    stored, failed = 0, []
    for row in pending:
        sid = str(row["source_id"])
        data = fetch_activity_splits(sid, client=client)
        laps = data.get("laps")
        if not laps:
            failed.append({"source_id": sid, "date": row["activity_date"], "reason": data.get("error") or "no laps"})
            continue
        try:
            upsert_activity_laps(sid, laps)
            stored += 1
        except Exception as exc:
            failed.append({"source_id": sid, "date": row["activity_date"], "reason": f"{type(exc).__name__}: {exc}"[:120]})

    return {
        "status": "ok",
        "fetched": stored,
        "failed": failed,
        "remaining": len(activities_missing_laps(limit=100)),
        "coverage": lap_coverage(),
    }


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
