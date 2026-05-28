from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from typing import Any

from .db import list_progress_snapshots, upsert_progress_snapshot


def _log_exc(where: str, exc: BaseException) -> None:
    print(f"[progress.{where}] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

# Goal distance -> Garmin race-prediction field, with a matching tolerance (km).
_DISTANCE_KEY = [
    (5.0, "time5K", 1.0),
    (10.0, "time10K", 1.5),
    (21.0975, "timeHalfMarathon", 2.0),
    (42.195, "timeMarathon", 3.0),
]


def _key_for_distance(distance_km: float) -> str | None:
    for d, key, tol in _DISTANCE_KEY:
        if abs(distance_km - d) <= tol:
            return key
    return None


def _hms(seconds: float) -> str:
    s = int(round(abs(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _signed_hms(seconds: float) -> str:
    return ("-" if seconds < 0 else "+") + _hms(seconds)


def _entry_date(entry: dict[str, Any]) -> str | None:
    for k in ("calendarDate", "date", "fromCalendarDate", "timestamp"):
        v = entry.get(k)
        if v:
            return str(v)[:10]
    return None


def _entry_seconds(entry: dict[str, Any], key: str) -> int | None:
    v = entry.get(key)
    try:
        return int(float(v)) if v else None
    except (TypeError, ValueError):
        return None


def _garmin_history(start: date, end: date, key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        from .garmin_client import get_garmin_client

        client = get_garmin_client()
        raw = client.get_race_predictions(start.isoformat(), end.isoformat())
        if isinstance(raw, dict):
            raw = [raw]
        for e in raw or []:
            if not isinstance(e, dict):
                continue
            d = _entry_date(e)
            s = _entry_seconds(e, key)
            if d and s:
                out[d] = s
    except Exception as exc:
        _log_exc("_garmin_history", exc)
    return out


def _garmin_current(key: str) -> int | None:
    try:
        from .garmin_metrics import fetch_fitness

        rp = (fetch_fitness().get("metrics") or {}).get("race_predictions") or {}
        return _entry_seconds(rp, key)
    except Exception as exc:
        _log_exc("_garmin_current", exc)
        return None


_ETA_CACHE_KEY = "eta_cache"
_ETA_CACHE_TTL_SEC = 6 * 3600


def _rule_based_eta(goal: Any, relevant: list, today: date) -> dict[str, Any]:
    target = int(goal["target_seconds"])
    if not relevant:
        return {
            "estimated_date": None,
            "on_pace": None,
            "weeks_remaining": None,
            "explanation": "Estimate will appear after the next sync brings in a Garmin race-prediction snapshot.",
            "engine": "rules",
        }
    last = relevant[-1]
    last_sec = int(last["predicted_seconds"])
    if last_sec <= target:
        return {
            "estimated_date": today.isoformat(),
            "on_pace": True,
            "weeks_remaining": 0,
            "explanation": f"Already on pace — Garmin's latest prediction ({_hms(last_sec)}) beats your target ({_hms(target)}) by {_hms(target - last_sec)}.",
            "engine": "rules",
        }
    gap = last_sec - target
    rate_per_week = None
    if len(relevant) >= 2:
        first = relevant[0]
        span_days = max(1, (date.fromisoformat(last["snap_date"]) - date.fromisoformat(first["snap_date"])).days)
        improvement = int(first["predicted_seconds"]) - last_sec
        rate_per_week = improvement / (span_days / 7)
    if rate_per_week and rate_per_week > 1:
        weeks = max(1, round(gap / rate_per_week))
        target_date = today + timedelta(weeks=weeks)
        return {
            "estimated_date": target_date.isoformat(),
            "on_pace": False,
            "weeks_remaining": weeks,
            "explanation": (
                f"Currently {_hms(gap)} behind target. At your recent improvement rate "
                f"(~{round(rate_per_week)}s/week), you should reach {_hms(target)} in about "
                f"{weeks} weeks (around {target_date.strftime('%b %d, %Y')})."
            ),
            "engine": "rules",
        }
    fallback_weeks = 12
    target_date = today + timedelta(weeks=fallback_weeks)
    return {
        "estimated_date": target_date.isoformat(),
        "on_pace": False,
        "weeks_remaining": fallback_weeks,
        "explanation": (
            f"Currently {_hms(gap)} behind target. With consistent training (~{fallback_weeks} weeks "
            f"of progressive overload) — typical for this gap — you should be on track; the estimate "
            f"sharpens as Garmin race-prediction snapshots accumulate."
        ),
        "engine": "rules",
    }


def _pace_text(sec: Any) -> str | None:
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return f"{s // 60}:{s % 60:02d}/km"


def _llm_eta(goal: Any, relevant: list, today: date) -> dict[str, Any] | None:
    """Ask the OpenAI coach for a context-aware completion estimate. Returns None
    on any failure so the caller falls back to the rule-based logic."""
    try:
        from .db import get_active_plan, link_actuals, list_planned_workouts, list_runs
        from .openai_client import _client, _create, current_model
        from .readiness import calculate_readiness

        client = _client()
        if client is None:
            return None
        distance = float(goal["distance_km"])
        target = int(goal["target_seconds"])

        # Training plan (DB)
        plan_row = get_active_plan()
        plan_info = None
        if plan_row:
            prog = json.loads(plan_row["progression"])
            plan_info = {
                "base_weekly_km": plan_row["base_weekly_km"],
                "weekly_increase_pct": round(prog.get("weekly_increase", 0) * 100, 1),
                "cap_weekly_km": prog.get("cap_km"),
                "plan_started_on": prog.get("start_date"),
            }

        # Recent runs for the LLM prompt: just the last 10 (more would crowd
        # the prompt without adding value). Readiness math needs a wider window
        # though — see `readiness_run_rows` below.
        recent_run_rows = list_runs(limit=10)
        recent_runs = [
            {
                "date": r["activity_date"],
                "distance_km": r["distance_km"],
                "pace": _pace_text(r["avg_pace_sec_per_km"]),
                "avg_hr": r["avg_hr"],
                "duration": _hms(r["duration_seconds"]) if r["duration_seconds"] else None,
            }
            for r in recent_run_rows
        ]

        # Training consistency over last 28 days (planned vs actual)
        link_actuals(today)
        consistency = None
        try:
            rows = list_planned_workouts((today - timedelta(days=28)).isoformat(), today.isoformat())
            run_days = [r for r in rows if r["kind"] != "rest"]
            completed = [r for r in run_days if r["status"] == "completed"]
            missed = [r for r in run_days if r["status"] == "missed"]
            consistency = {
                "window_days": 28,
                "planned_run_days": len(run_days),
                "completed": len(completed),
                "missed": len(missed),
                "completion_pct": round(100 * len(completed) / len(run_days)) if run_days else None,
            }
        except Exception as exc:
            _log_exc("_llm_eta.consistency", exc)
            consistency = None

        # Load-based readiness (DB) — fast. Use a wider window than the LLM
        # prompt's `recent_runs` because `calculate_readiness` sums distance
        # over the last 7 vs 28 days; only 10 rows under-counts the 4-week
        # baseline and biases acute:chronic ratio high → "much higher than
        # baseline" appears even on conservative training.
        readiness_info = None
        try:
            readiness_run_rows = [dict(r) for r in list_runs(limit=500)]
            r = calculate_readiness(readiness_run_rows, today=today)
            readiness_info = {
                "score": r.score,
                "status": r.status,
                "reasons": r.reasons,
                "weekly_distance_km": r.weekly_distance_km,
                "four_week_avg_km": r.four_week_avg_km,
                "acute_chronic_ratio": r.acute_chronic_ratio,
            }
        except Exception as exc:
            _log_exc("_llm_eta.readiness", exc)
            readiness_info = None

        # Live Garmin condition (best-effort — recovery + fitness summary).
        # Cached tokens make this fast; on failure (e.g. SSO 429) we proceed
        # without it and the LLM still has plenty of context.
        current_condition = None
        try:
            from .garmin_metrics import fetch_snapshot

            snap = fetch_snapshot()
            current_condition = {"recovery": snap.get("recovery"), "fitness": snap.get("fitness")}
        except Exception as exc:
            _log_exc("_llm_eta.current_condition", exc)
            current_condition = None

        context = {
            "today": today.isoformat(),
            "goal": {
                "distance_km": distance,
                "target_time": _hms(target),
                "target_pace_per_km": _hms(round(target / distance)) if distance else None,
            },
            "race_prediction_history": [
                {"date": s["snap_date"], "predicted_time": _hms(int(s["predicted_seconds"])), "predicted_seconds": int(s["predicted_seconds"])}
                for s in relevant
            ],
            "training_plan": plan_info,
            "recent_runs": recent_runs,
            "training_consistency_28d": consistency,
            "load_based_readiness": readiness_info,
            "current_condition": current_condition,
        }
        system = (
            "You are an expert running coach. Estimate when the athlete will realistically "
            "reach their goal target time, taking into account ALL the provided context — not "
            "just Garmin's race-prediction history.\n\n"
            "IMPORTANT: Garmin's race-predictions can be inaccurate (often optimistic for "
            "longer distances, sometimes pessimistic). Cross-check them against:\n"
            "- Recent run paces and HR (do recent easy runs already hit goal pace? are HRs "
            "rising relative to pace?)\n"
            "- Training consistency (high completion → trend reliable; lots of missed days → "
            "trajectory weaker than Garmin suggests)\n"
            "- Current condition (training readiness, HRV status, sleep, training status like "
            "'productive' vs 'overreaching', VO2max). Poor recovery slows adaptation.\n"
            "- Load-based readiness (acute:chronic ratio — high values mean injury risk and "
            "slower gains; very low means under-trained).\n"
            "- The plan's progression (weekly increase + cap) and how many weeks have elapsed.\n"
            "- The size of the gap and typical adaptation rates (8-16 weeks of consistent work "
            "yields meaningful gains for most athletes; bigger gaps need more time).\n\n"
            "If already on pace based on Garmin AND recent runs corroborate, say on pace. "
            "If Garmin says on pace but recent runs/HR suggest otherwise, be skeptical and "
            "say so. Be honest — don't promise unrealistic timelines.\n\n"
            "Respond with ONLY a JSON object: "
            "{\"estimated_date\": \"YYYY-MM-DD\" or null, "
            "\"on_pace\": bool, "
            "\"weeks_remaining\": integer or null, "
            "\"explanation\": \"3-4 sentence reasoning citing the specific numbers from the context — "
            "include at least one factor beyond Garmin's prediction (consistency, HR/pace trend, "
            "recovery, or readiness)\"}."
        )
        resp = _create(
            client,
            current_model(),
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(context, default=str)}],
            json_mode=True,
            max_out=2000,
            temperature=0.3,
        )
        content = resp.choices[0].message.content
        if not content or not content.strip():
            return None
        data = json.loads(content)
        if not data.get("explanation"):
            return None
        return {
            "estimated_date": data.get("estimated_date"),
            "on_pace": bool(data.get("on_pace")) if data.get("on_pace") is not None else None,
            "weeks_remaining": data.get("weeks_remaining"),
            "explanation": data["explanation"],
            "engine": "openai",
        }
    except Exception as exc:
        # Caller falls back to rule-based ETA — log so a permanent OpenAI
        # breakage (e.g. revoked key, schema change) doesn't silently degrade.
        _log_exc("_llm_eta", exc)
        return None


def _compute_eta(goal: Any) -> dict[str, Any]:
    today = date.today()
    distance = float(goal["distance_km"])
    snaps = list_progress_snapshots((today - timedelta(weeks=16)).isoformat(), today.isoformat())
    relevant = [s for s in snaps if abs(s["distance_km"] - distance) <= 2]
    llm = _llm_eta(goal, relevant, today)
    if llm:
        return llm
    return _rule_based_eta(goal, relevant, today)


def _cached_eta() -> dict[str, Any] | None:
    try:
        from .db import get_config

        raw = get_config(_ETA_CACHE_KEY)
        if not raw:
            return None
        entry = json.loads(raw)
        ts = datetime.fromisoformat(entry["ts"])
        if (datetime.utcnow() - ts).total_seconds() < _ETA_CACHE_TTL_SEC:
            return entry["data"]
    except Exception as exc:
        # A corrupt cache entry would otherwise silently force a fresh OpenAI
        # call on every dashboard load — log so we can identify the bad row.
        _log_exc("_cached_eta", exc)
        return None
    return None


def _cache_eta(data: dict[str, Any]) -> None:
    try:
        from .db import set_config

        set_config(_ETA_CACHE_KEY, json.dumps({"ts": datetime.utcnow().isoformat(), "data": data}))
    except Exception as exc:
        _log_exc("_cache_eta", exc)


def estimate_eta(goal: Any, fresh: bool = False) -> dict[str, Any]:
    """LLM-driven estimate (with rule-based fallback). Cached for ~6h to avoid
    hitting OpenAI on every dashboard load. Pass `fresh=True` to bypass cache —
    used by `POST /goal` so a goal change always recomputes."""
    if not fresh:
        cached = _cached_eta()
        if cached is not None:
            return cached
    data = _compute_eta(goal)
    _cache_eta(data)
    return data


def record_snapshot(goal: Any) -> int | None:
    """Best-effort: store today's Garmin prediction for the goal distance."""
    key = _key_for_distance(goal["distance_km"])
    if not key:
        return None
    current = _garmin_current(key)
    if current is None:
        return None
    upsert_progress_snapshot(date.today().isoformat(), goal["distance_km"], current, goal["target_seconds"])
    return current


def build_report(goal: Any, weeks: int = 12) -> dict[str, Any]:
    distance = float(goal["distance_km"])
    target = int(goal["target_seconds"])
    key = _key_for_distance(distance)
    today = date.today()
    start = today - timedelta(weeks=weeks)

    if key is None:
        return {"error": f"Garmin only predicts 5K/10K/half/marathon; no prediction maps to {distance} km."}

    series: dict[str, int] = {}
    series.update(_garmin_history(start, today, key))
    current = _garmin_current(key)
    if current is not None:
        series[today.isoformat()] = current
        upsert_progress_snapshot(today.isoformat(), distance, current, target)

    # merge our own accumulated snapshots (fills gaps / extends history over time)
    for snap in list_progress_snapshots(start.isoformat(), today.isoformat()):
        if abs(snap["distance_km"] - distance) <= 2:
            series.setdefault(snap["snap_date"], int(snap["predicted_seconds"]))

    points = sorted(series.items())
    if not points:
        return {
            "goal": {"distance_km": distance, "target": _hms(target)},
            "current_prediction": None,
            "message": "No race-prediction data from Garmin yet — it accrues over time; check back after a few runs.",
        }

    first_date, first_sec = points[0]
    last_date, last_sec = points[-1]
    cur = last_sec
    gap = cur - target  # negative => ahead of (faster than) target
    on_pace = cur <= target
    improvement = first_sec - last_sec  # positive => got faster over the window
    span_days = max(1, (date.fromisoformat(last_date) - date.fromisoformat(first_date)).days)
    rate_per_week = improvement / (span_days / 7)

    if improvement > 30:
        trend = "improving"
    elif improvement < -30:
        trend = "regressing"
    else:
        trend = "plateau"

    eta_weeks = None
    if not on_pace and rate_per_week > 1:
        eta_weeks = round(gap / rate_per_week, 1)

    if on_pace:
        verdict = f"On pace — predicted {_hms(cur)} beats your {_hms(target)} target by {_hms(gap)}."
    elif eta_weeks is not None:
        verdict = (
            f"Behind by {_hms(gap)}, but improving ~{round(rate_per_week)}s/week — on track to reach "
            f"{_hms(target)} in about {eta_weeks} weeks if the trend holds."
        )
    elif trend == "improving":
        verdict = f"Behind by {_hms(gap)} and improving slowly — stay consistent."
    else:
        verdict = f"Behind by {_hms(gap)} and not improving — consider adjusting training load/consistency."

    return {
        "goal": {"distance_km": distance, "target": _hms(target), "target_seconds": target},
        "current_prediction": _hms(cur),
        "current_seconds": cur,
        "gap_to_target": _signed_hms(gap),
        "on_pace": on_pace,
        "trend": trend,
        "change_over_window": _signed_hms(-improvement),  # negative = got faster
        "improvement_per_week_sec": round(rate_per_week),
        "eta_weeks_to_target": eta_weeks,
        "verdict": verdict,
        "window_weeks": weeks,
        "data_points": len(points),
        "series": [
            {"date": d, "predicted": _hms(s), "predicted_seconds": s, "on_pace": s <= target}
            for d, s in points
        ],
    }
