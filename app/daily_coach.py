from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import Any

from .db import (
    clear_garmin_pushed,
    get_active_goal,
    get_active_plan,
    get_planned_workout,
    link_actuals,
    list_planned_workouts,
    list_runs,
    upsert_planned_workout,
)
from .goal_planner import details_for, materialize
from .openai_client import coach_adjust
from .readiness import calculate_readiness

ALLOWED_KINDS = ["rest", "recovery", "easy", "long", "quality"]


def ensure_horizon(today: date | None = None, days: int = 14) -> None:
    """Make sure the next `days` of rule-based workouts exist."""
    today = today or date.today()
    plan = get_active_plan()
    if plan is not None:
        materialize(plan, today, days=days)


def _morning_metrics() -> dict[str, Any]:
    """Best-effort live Garmin recovery snapshot. Never raises — but log on
    failure so a permanent Garmin breakage (revoked token, schema change in
    training_readiness, chronic 429) doesn't silently feed `metrics={}` to the
    LLM every morning."""
    try:
        from .garmin_metrics import fetch_recovery

        m = (fetch_recovery() or {}).get("metrics", {}) or {}
        tr = m.get("training_readiness")
        if isinstance(tr, list) and tr:
            tr = tr[0]
        hrv = (m.get("hrv") or {}).get("hrvSummary") or {}
        sleep = (m.get("sleep") or {}).get("dailySleepDTO") or {}
        return {
            "garmin_training_readiness": (tr or {}).get("score") if isinstance(tr, dict) else None,
            "hrv_status": hrv.get("status"),
            "sleep_score": (sleep.get("sleepScores") or {}).get("overall", {}).get("value")
            if isinstance(sleep.get("sleepScores"), dict)
            else None,
        }
    except Exception as exc:
        print(f"[daily_coach._morning_metrics] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return {}


def _recent_results(today: date, days: int = 7) -> list[dict[str, Any]]:
    start = (today - timedelta(days=days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    out = []
    for r in list_planned_workouts(start, end):
        out.append(
            {
                "date": r["plan_date"],
                "kind": r["kind"],
                "planned_km": r["distance_km"],
                "status": r["status"],
                "actual_km": r["actual_distance_km"],
                "actual_pace_sec": r["actual_pace_sec"],
            }
        )
    return out


def _rule_adjust(base: dict[str, Any], paces: dict[str, Any], readiness, recent: list[dict[str, Any]]):
    """Deterministic guardrail workout + safe bounds for the LLM layer."""
    kind, dist, pace = base["kind"], base["distance_km"], base["target_pace_sec"]
    notes: list[str] = []

    if readiness.status == "red":
        kind, dist, pace = "rest", 0.0, None
        notes.append("readiness red → rest")
    elif readiness.status == "yellow":
        if kind == "quality":
            kind, pace = "easy", paces.get("easy")
            notes.append("readiness yellow → quality eased to aerobic")
        dist = round(dist * 0.8, 1)
        notes.append("volume trimmed ~20% for caution")

    missed_long = any(r["kind"] == "long" and r["status"] == "missed" for r in recent)
    if missed_long and kind in ("easy", "recovery") and readiness.status == "green":
        notes.append("a long run was missed this week — consider shifting it here")

    bounds = {
        "max_distance_km": round(max(dist, base["distance_km"]) * 1.1, 1),
        "allowed_kinds": ALLOWED_KINDS,
    }
    final = {
        "kind": kind,
        "distance_km": dist,
        "target_pace_sec": pace,
        "details": details_for(kind, pace),
        "coach_note": "; ".join(notes) if notes else "On plan — execute as prescribed.",
    }
    return final, bounds


def _merge_clamp(ai: dict[str, Any], fallback: dict[str, Any], bounds: dict[str, Any]):
    kind = ai.get("kind") if ai.get("kind") in bounds["allowed_kinds"] else fallback["kind"]
    try:
        dist = float(ai.get("distance_km"))
    except (TypeError, ValueError):
        dist = fallback["distance_km"]
    dist = max(0.0, min(round(dist, 1), bounds["max_distance_km"]))
    pace = ai.get("target_pace_sec") or fallback["target_pace_sec"]
    try:
        pace = int(pace) if pace is not None else None
    except (TypeError, ValueError):
        pace = fallback["target_pace_sec"]
    details = ai.get("details") or details_for(kind, pace)
    note = ai.get("coach_note") or fallback["coach_note"]
    return {"kind": kind, "distance_km": dist, "target_pace_sec": pace, "details": details, "coach_note": note}


def adapt_today(today: date | None = None, use_live_metrics: bool = True) -> dict[str, Any] | None:
    """Compute (and store) today's adapted workout. Returns the workout dict or
    None if there's no active goal/plan."""
    today = today or date.today()
    ds = today.isoformat()
    goal = get_active_goal()
    plan = get_active_plan()
    if goal is None or plan is None:
        return None

    link_actuals(today)
    base = get_planned_workout(ds)
    if base is None:
        ensure_horizon(today)
        base = get_planned_workout(ds)
    if base is None:
        return None
    base = dict(base)

    prog = json.loads(plan["progression"])
    paces = prog.get("paces", {})
    runs = [dict(r) for r in list_runs(limit=500)]
    readiness = calculate_readiness(runs, today=today)
    metrics = _morning_metrics() if use_live_metrics else {}
    recent = _recent_results(today)

    suggestion, bounds = _rule_adjust(base, paces, readiness, recent)

    context = {
        "goal": {"distance_km": goal["distance_km"], "target_seconds": goal["target_seconds"]},
        "today_planned": {"kind": base["kind"], "distance_km": base["distance_km"], "target_pace_sec": base["target_pace_sec"]},
        "rule_suggestion": suggestion,
        "safety_bounds": bounds,
        "readiness": {"score": readiness.score, "status": readiness.status, "reasons": readiness.reasons},
        "morning_metrics": metrics,
        "recent_results": recent,
    }
    ai = coach_adjust(context)
    final = _merge_clamp(ai, suggestion, bounds) if isinstance(ai, dict) else suggestion
    source = "adapted" if isinstance(ai, dict) else "rule"

    # Detect a meaningful change vs. what was previously stored so the prior
    # Garmin push can be removed and the next push (either the morning job's
    # today-push, or an explicit /goal/week/push) replaces the day cleanly.
    prior_kind = base.get("kind")
    prior_dist = float(base.get("distance_km") or 0)
    prior_pace = base.get("target_pace_sec") or None
    prior_workout_id = base.get("garmin_workout_id")
    new_pace = final.get("target_pace_sec") or None
    changed = (
        final["kind"] != prior_kind
        or abs(float(final["distance_km"] or 0) - prior_dist) > 0.01
        or new_pace != prior_pace
    )

    upsert_planned_workout(
        ds, final["kind"], final["distance_km"], final["target_pace_sec"], final["details"],
        source=source, coach_note=final["coach_note"], status="planned",
    )
    if changed:
        # Auto-unschedule the previous Garmin workout so the next push replaces
        # (rather than duplicates) the day on the calendar. A failed delete
        # leaves a dangling Garmin workout — log it so the user can see why.
        if prior_workout_id:
            try:
                from .workout_publisher import delete_garmin_workout

                result = delete_garmin_workout(str(prior_workout_id))
                if isinstance(result, dict) and result.get("status") != "ok":
                    print(
                        f"[daily_coach.delete_prior_workout] non-ok status: {result}",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"[daily_coach.delete_prior_workout] {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        clear_garmin_pushed(ds)
    return {
        "date": ds,
        **final,
        "engine": "openai+rules" if isinstance(ai, dict) else "rules-only",
        "readiness": readiness.__dict__,
    }
