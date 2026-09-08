from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
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

# Where each post-sleep metric records the instant it was PRODUCED, as opposed to
# the calendar day it belongs to.
#
# This distinction is the whole point. Garmin stamps an overnight training-readiness
# record with today's `calendarDate` the moment midnight passes — hours before you
# wake — so a date check alone reads a 03:00 provisional score as "this morning's
# data". Observed 2026-08-26: at 06:00 the payload was stamped 2026-08-26 and
# looked fresh, but the athlete woke at 06:23 and the readiness record reflecting
# that night was not written until 07:45.
_GENERATED_AT: dict[str, tuple[str, ...]] = {
    "training_readiness": ("timestamp",),
    "hrv": ("hrvSummary", "createTimeStamp"),
    # Sleep is produced by its own ending: the wake instant.
    "sleep": ("dailySleepDTO", "sleepEndTimestampGMT"),
}


def _as_epoch_ms(v: Any) -> int | None:
    """Garmin gives UTC instants as epoch milliseconds or as an ISO string.

    Fractional seconds are dropped rather than parsed — they arrive with varying
    precision ('...:36.0', '...:15.820') and none of it matters at this scale.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    if isinstance(v, str) and len(v) >= 19:
        try:
            stamp = datetime.fromisoformat(v[:19])
        except ValueError:
            return None
        return int(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return None


def _generated_at_ms(name: str, obj: Any) -> int | None:
    """When Garmin produced this payload (epoch ms UTC), or None if it doesn't say."""
    if isinstance(obj, list):
        obj = obj[0] if obj else None
    path = _GENERATED_AT.get(name)
    if not isinstance(obj, dict) or path is None:
        return None
    return _as_epoch_ms(_g(obj, *path))


def wake_instant_ms(sleep: Any) -> int | None:
    """When last night's sleep ended (epoch ms UTC) — the moment from which Garmin
    can have anything to say about this morning. None while the night is still
    open or untracked."""
    return _generated_at_ms("sleep", sleep)


def _hhmm_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%MZ")

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

_FRESHNESS_KEYS = ("date", "fresh", "have", "unverified", "stale", "pre_wake", "missing", "errors", "reason")


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

    Both producers here emit exactly the nine `_FRESHNESS_KEYS`, so consumers —
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
        "pre_wake": [],
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
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Whether the post-sleep metrics really are from THIS MORNING.

    Garmin answers a request for today with null fields when the watch has not
    synced since waking, so a missing metric is indistinguishable from a normal
    morning unless the calendar stamp on the payload is checked.

    `fresh` requires at least one metric to pass BOTH tests:

    1. **Right day.** Its calendar stamp is `day`. A payload that is present but
       undatable is reported as `unverified` and does NOT count: treating it as
       fresh meant yesterday's numbers could decide today's session whenever a
       response shape changed.
    2. **After you woke.** It was produced at or after `sleepEndTimestampGMT`.
       The date test alone is not enough — Garmin stamps an overnight readiness
       record with today's date from midnight, so at 06:00 a provisional score
       computed while the athlete was still asleep looked exactly like the
       morning report. Anything older than the wake instant is listed in
       `pre_wake` and ignored, and while the night is still open (no wake
       instant, or one in the future) nothing is fresh at all.

    A metric that carries no production timestamp of its own is kept once the
    night is known to be over: it cannot be shown to predate waking, and by then
    the provisional-overnight case it would otherwise admit is already past.

    The morning job proceeds at its cutoff regardless, so an unexpected shape
    delays the adaptation and logs loudly rather than stalling it forever.

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
    # Second test: nothing Garmin wrote before you woke describes this morning.
    payloads = dict(zip(_POST_SLEEP_METRICS, (tr, sleep, hrv)))
    pre_wake: list[str] = []
    wake = wake_instant_ms(sleep)
    now = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if have and (wake is None or wake > now):
        # The night is still open (or untracked): every "today" payload in hand
        # was necessarily written before waking, whatever its calendar stamp.
        pre_wake, have = have, []
        reason = reason or (
            f"last night's sleep has not ended yet (wakes {_hhmm_utc(wake)})"
            if wake is not None
            else "no wake time recorded for last night yet"
        )
    elif have:
        confirmed: list[str] = []
        for name in have:
            produced = _generated_at_ms(name, payloads[name])
            if produced is not None and produced < wake:
                pre_wake.append(f"{name}@{_hhmm_utc(produced)}")
            else:
                confirmed.append(name)
        have = confirmed
        if not have and not reason:
            reason = "every metric for today was written before you woke"

    failed = {k: v for k, v in (errors or {}).items() if k in _POST_SLEEP_METRICS}
    return {
        "date": day,
        "fresh": bool(have),
        "have": have,
        "unverified": unverified,
        "stale": stale,
        "pre_wake": pre_wake,
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


# --- Cached fitness read -----------------------------------------------------

# VO2max, race predictions and training status move on a scale of weeks, so a
# reader that just wants to know how fit the athlete is should never pay for a
# Garmin login. Half a day keeps it current without putting a multi-second fetch
# in front of an interactive request.
_FITNESS_CACHE_KEY = "fitness_summary_cache"
_FITNESS_CACHE_TTL_SEC = 12 * 3600

# Garmin's race-prediction payload keys, in ascending distance.
_RACE_KEYS = {
    "5k": "time5K",
    "10k": "time10K",
    "half_marathon": "timeHalfMarathon",
    "marathon": "timeMarathon",
}


def race_predictions(rp: Any) -> dict[str, Any]:
    """Garmin's predicted race times, as seconds plus a readable pace.

    These are the closest thing to a recent maximal effort that exists without
    the athlete actually racing, which is exactly what a coach needs to answer a
    "how fast can I go" question. Garmin returns either a dict or a one-element
    list depending on the endpoint version.
    """
    if isinstance(rp, list):
        rp = rp[0] if rp else None
    if not isinstance(rp, dict):
        return {}
    dists = {"5k": 5.0, "10k": 10.0, "half_marathon": 21.0975, "marathon": 42.195}
    out: dict[str, Any] = {}
    for label, key in _RACE_KEYS.items():
        secs = rp.get(key)
        if not isinstance(secs, (int, float)) or secs <= 0:
            continue
        pace = round(secs / dists[label])
        out[label] = {
            "seconds": int(secs),
            "time": f"{int(secs) // 3600}:{(int(secs) % 3600) // 60:02d}:{int(secs) % 60:02d}",
            "pace_sec_per_km": pace,
            "pace": f"{pace // 60}:{pace % 60:02d}/km",
            "speed_kmh": round(3600.0 / pace, 2),
        }
    return out


def _fitness_summary_live(cdate: date | None = None) -> dict[str, Any]:
    """Compact fitness summary + race predictions. Never raises."""
    try:
        raw = fetch_fitness(cdate) or {}
        m = raw.get("metrics", {}) or {}
        summary = _summarize_fitness(
            m.get("training_status"),
            m.get("race_predictions"),
            m.get("endurance_score"),
            m.get("vo2max"),
        )
        summary["race_predictions"] = race_predictions(m.get("race_predictions"))
        summary["date"] = raw.get("date")
        summary["errors"] = raw.get("errors")
        return {k: v for k, v in summary.items() if v is not None}
    except Exception as exc:
        print(f"[garmin_metrics._fitness_summary_live] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def cached_fitness_summary(fresh: bool = False, allow_fetch: bool = True) -> dict[str, Any]:
    """`_fitness_summary_live` behind a ~12h cache. Never raises.

    Failures are cached too, so a revoked Garmin token cannot put a doomed login
    attempt in front of every interactive request.

    `allow_fetch=False` is cache-only and never touches the network — a cold read
    costs six Garmin calls, which is not something an interactive request should
    wait on. The morning job primes the cache.
    """
    from .db import get_config, set_config

    if not fresh:
        try:
            raw = get_config(_FITNESS_CACHE_KEY)
            if raw:
                entry = json.loads(raw)
                age = (datetime.utcnow() - datetime.fromisoformat(entry["ts"])).total_seconds()
                if age < _FITNESS_CACHE_TTL_SEC:
                    return {**entry["data"], "cached": True}
        except Exception as exc:
            print(f"[garmin_metrics.cached_fitness_summary] cache read: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    if not allow_fetch:
        return {"cached": False, "error": "no fitness data cached yet (not fetched here to avoid blocking on Garmin)"}

    data = _fitness_summary_live()
    try:
        set_config(_FITNESS_CACHE_KEY, json.dumps({"ts": datetime.utcnow().isoformat(), "data": data}))
    except Exception as exc:
        print(f"[garmin_metrics.cached_fitness_summary] cache write: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return {**data, "cached": False}
