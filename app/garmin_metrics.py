from __future__ import annotations

from datetime import date
from typing import Any, Callable

# `garmin_client` is imported lazily inside the fetchers, not here, so this module
# stays import-safe. `no_freshness` below is a plain dict builder that callers rely
# on in their own exception handlers — if importing this module could fail, those
# handlers would fail with it.


def _run(metrics: dict[str, Callable[[], Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Call each metric fetcher independently.

    Garmin only reports a metric when the user's device supports it and recorded
    data for the day, so one unsupported metric must not fail the whole response.
    """
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, fetch in metrics.items():
        try:
            data[key] = fetch()
        except Exception as exc:  # noqa: BLE001 - isolate per-metric failures
            data[key] = None
            errors[key] = str(exc)
    return data, errors


def fetch_recovery(cdate: date | None = None) -> dict[str, Any]:
    d = (cdate or date.today()).isoformat()
    from .garmin_client import get_garmin_client

    client = get_garmin_client()
    data, errors = _run(
        {
            "training_readiness": lambda: client.get_training_readiness(d),
            "hrv": lambda: client.get_hrv_data(d),
            "sleep": lambda: client.get_sleep_data(d),
            "stress": lambda: client.get_stress_data(d),
            "body_battery": lambda: client.get_body_battery(d, d),
            "resting_heart_rate": lambda: client.get_rhr_day(d),
            "respiration": lambda: client.get_respiration_data(d),
            "spo2": lambda: client.get_spo2_data(d),
        }
    )
    return {"date": d, "metrics": data, "errors": errors or None}


def fetch_fitness(cdate: date | None = None) -> dict[str, Any]:
    d = (cdate or date.today()).isoformat()
    from .garmin_client import get_garmin_client

    client = get_garmin_client()
    data, errors = _run(
        {
            "training_status": lambda: client.get_training_status(d),
            "race_predictions": lambda: client.get_race_predictions(),
            "vo2max": lambda: client.get_max_metrics(d),
            "endurance_score": lambda: client.get_endurance_score(d, d),
            "hill_score": lambda: client.get_hill_score(d, d),
            "fitness_age": lambda: client.get_fitnessage_data(d),
        }
    )
    return {"date": d, "metrics": data, "errors": errors or None}


def _g(obj: Any, *keys: str) -> Any:
    """Defensive nested dict get."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _calendar_date(obj: Any) -> str | None:
    """The calendar date Garmin stamped on a metric payload, if it carries one."""
    if isinstance(obj, list):
        obj = obj[0] if obj else None
    if not isinstance(obj, dict):
        return None
    for key in ("calendarDate", "calendar_date"):
        v = obj.get(key)
        if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit():
            return v[:10]
    for nested in ("dailySleepDTO", "hrvSummary"):
        v = _calendar_date(obj.get(nested))
        if v:
            return v
    for key in ("timestamp", "timestampLocal", "calendarDateLocal"):
        v = obj.get(key)
        if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit():
            return v[:10]
    return None


# The post-sleep signals — the ones that only exist once you've woken up and the
# watch has synced. Body battery/stress/RHR trickle in all day and say nothing
# about whether this morning's recovery picture has landed, so they are excluded
# here and cannot on their own make a verdict physiological.
_POST_SLEEP_METRICS = ("training_readiness", "sleep", "hrv")

# Which summary keys each post-sleep metric contributes, so a metric that is
# stale, missing or undatable can be dropped from the summary before it is scored.
POST_SLEEP_SUMMARY_KEYS: dict[str, tuple[str, ...]] = {
    "training_readiness": ("training_readiness", "training_readiness_level"),
    "hrv": ("hrv_status", "hrv_last_night_ms"),
    "sleep": ("sleep_score", "sleep_hours"),
}

# Every summary key that a date-verified post-sleep metric can contribute. This is
# the set that may assert "this morning's recovery picture has landed" — derived
# rather than hand-listed so it cannot drift from the filter above.
VERIFIED_SUMMARY_KEYS: tuple[str, ...] = tuple(k for keys in POST_SLEEP_SUMMARY_KEYS.values() for k in keys)

_FRESHNESS_KEYS = ("date", "fresh", "have", "unverified", "stale", "missing", "errors", "reason")


def has_verified_signal(summary: dict[str, Any]) -> bool:
    """Whether a recovery summary carries any date-verified post-sleep value.

    A payload can be stamped with today's date and still be empty of usable
    numbers — an untracked night, or an HRV status Garmin hasn't established yet.
    Callers use this so "the data arrived" and "the data says something" cannot
    diverge: the morning job would otherwise stop waiting on a payload that
    scores nothing, and then tell the athlete their watch hadn't synced.
    """
    return any(summary.get(k) is not None for k in VERIFIED_SUMMARY_KEYS)


def no_freshness(reason: str, errors: dict[str, str] | None = None) -> dict[str, Any]:
    """A freshness report for "we never got a usable answer".

    Both producers here emit exactly the eight `_FRESHNESS_KEYS`, so consumers —
    including the dashboard's JavaScript — can rely on the shape. `reason` describes
    a whole-call failure (or a payload that scored nothing); `errors` carries
    per-metric Garmin failures. Either may be None.
    """
    return {
        "date": None,
        "fresh": False,
        "have": [],
        "unverified": [],
        "stale": [],
        "missing": list(_POST_SLEEP_METRICS),
        "errors": errors,
        "reason": reason,
    }


def recovery_freshness(
    day: str,
    *,
    tr: Any = None,
    hrv: Any = None,
    sleep: Any = None,
    errors: dict[str, str] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Whether the post-sleep metrics really are from `day`.

    Garmin answers a request for today with null fields when the watch has not
    synced since waking, so a missing metric is indistinguishable from a normal
    morning unless the calendar stamp on the payload is checked.

    `fresh` requires at least one metric CONFIRMED to carry today's date. A
    payload that is present but undatable is reported as `unverified` and does
    NOT count as fresh: treating it as fresh meant yesterday's numbers could
    decide today's session whenever a response shape changed. The morning job
    proceeds at its cutoff regardless, so an unexpected shape delays the
    adaptation and logs loudly rather than stalling it forever.

    `errors` carries `fetch_recovery`'s per-metric failures so callers can tell a
    broken Garmin connection ("401 Unauthorized") from a watch that simply has
    not synced. Without it both look identical.

    `tr`/`hrv`/`sleep` are keyword-only: they are three same-typed opaque payloads,
    and the internal pairing below deliberately reorders them to match
    `_POST_SLEEP_METRICS`, so a positional call is easy to get silently wrong.
    """
    have: list[str] = []
    unverified: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    for name, obj in zip(_POST_SLEEP_METRICS, (tr, sleep, hrv)):
        if obj is None or (isinstance(obj, (list, dict)) and not obj):
            missing.append(name)
            continue
        stamped = _calendar_date(obj)
        if stamped is None:
            unverified.append(name)
        elif stamped != day:
            stale.append(f"{name}@{stamped}")
        else:
            have.append(name)
    failed = {k: v for k, v in (errors or {}).items() if k in _POST_SLEEP_METRICS}
    return {
        "date": day,
        "fresh": bool(have),
        "have": have,
        "unverified": unverified,
        "stale": stale,
        "missing": missing,
        # Set only when Garmin itself refused a call — the difference between
        # "your watch hasn't synced" and "this service can't reach Garmin".
        "errors": failed or None,
        "reason": reason,
    }


def _summarize_recovery(tr, hrv, sleep, stress, bb, rhr) -> dict[str, Any]:
    if isinstance(tr, list) and tr:
        tr = tr[0]
    secs = _g(sleep, "dailySleepDTO", "sleepTimeSeconds")
    bb_val = None
    try:
        if isinstance(bb, list) and bb:
            arr = bb[0].get("bodyBatteryValuesArray") or []
            bb_val = arr[-1][1] if arr else bb[0].get("charged")
    except Exception:
        bb_val = None
    rhr_val = None
    try:
        mm = _g(rhr, "allMetrics", "metricsMap")
        arr = mm.get("WELLNESS_RESTING_HEART_RATE") if isinstance(mm, dict) else None
        rhr_val = arr[0].get("value") if arr else _g(rhr, "restingHeartRate")
    except Exception:
        rhr_val = None
    return {
        "training_readiness": _g(tr, "score") if isinstance(tr, dict) else None,
        "training_readiness_level": _g(tr, "level") if isinstance(tr, dict) else None,
        "hrv_status": _g(hrv, "hrvSummary", "status"),
        "hrv_last_night_ms": _g(hrv, "hrvSummary", "lastNightAvg"),
        "sleep_score": _g(sleep, "dailySleepDTO", "sleepScores", "overall", "value"),
        "sleep_hours": round(secs / 3600, 1) if isinstance(secs, (int, float)) else None,
        "avg_stress": _g(stress, "avgStressLevel"),
        "body_battery": bb_val,
        "resting_hr": rhr_val,
    }


def _summarize_fitness(ts, rp, es, vo2) -> dict[str, Any]:
    vo2v = _g(ts, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue") or _g(ts, "mostRecentVO2Max", "generic", "vo2MaxValue")
    if vo2v is None and isinstance(vo2, list) and vo2:
        vo2v = _g(vo2[0], "generic", "vo2MaxPreciseValue") or _g(vo2[0], "generic", "vo2MaxValue")
    tstat, phrase = None, None
    latest = _g(ts, "mostRecentTrainingStatus", "latestTrainingStatusData")
    if isinstance(latest, dict):
        for v in latest.values():
            if isinstance(v, dict) and (v.get("trainingStatus") or v.get("trainingStatusFeedbackPhrase")):
                tstat = v.get("trainingStatus")
                phrase = v.get("trainingStatusFeedbackPhrase")
                break
    return {
        "vo2max": round(vo2v, 1) if isinstance(vo2v, (int, float)) else None,
        "training_status": tstat,
        "training_status_phrase": phrase,
        "endurance_score": _g(es, "overallScore") or _g(es, "enduranceScore"),
        "half_prediction_sec": _g(rp, "timeHalfMarathon"),
    }


def fetch_snapshot(cdate: date | None = None) -> dict[str, Any]:
    """One Garmin login → compact recovery + fitness summaries for the dashboard."""
    d = (cdate or date.today()).isoformat()
    from .garmin_client import get_garmin_client

    client = get_garmin_client()

    def safe(fn):
        try:
            return fn()
        except Exception:
            return None

    rec = _summarize_recovery(
        safe(lambda: client.get_training_readiness(d)),
        safe(lambda: client.get_hrv_data(d)),
        safe(lambda: client.get_sleep_data(d)),
        safe(lambda: client.get_stress_data(d)),
        safe(lambda: client.get_body_battery(d, d)),
        safe(lambda: client.get_rhr_day(d)),
    )
    fit = _summarize_fitness(
        safe(lambda: client.get_training_status(d)),
        safe(lambda: client.get_race_predictions()),
        safe(lambda: client.get_endurance_score(d, d)),
        safe(lambda: client.get_max_metrics(d)),
    )
    return {"date": d, "recovery": rec, "fitness": fit}
