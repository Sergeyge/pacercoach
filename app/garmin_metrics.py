from __future__ import annotations

from datetime import date
from typing import Any, Callable

from .garmin_client import get_garmin_client


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
