from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import Any

from .models import Readiness


def _round_half(x: float) -> float:
    return round(x * 2) / 2


def _parse_run_date(run: dict[str, Any]) -> date:
    return date.fromisoformat(str(run["activity_date"]))


def _recent_runs(runs: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    return sorted(
        [r for r in runs if _parse_run_date(r) <= today],
        key=lambda r: _parse_run_date(r),
        reverse=True,
    )


def _avg_easy_pace(runs: list[dict[str, Any]]) -> int | None:
    paces = [int(r["avg_pace_sec_per_km"]) for r in runs if r.get("avg_pace_sec_per_km")]
    return round(mean(paces)) if paces else None


def _pace_text(avg_pace_sec_per_km: int | None, modifier_sec: int = 30) -> str:
    if not avg_pace_sec_per_km:
        return "Use easy conversational effort / Garmin Zone 2."
    easy = avg_pace_sec_per_km + modifier_sec
    minutes = easy // 60
    seconds = easy % 60
    return f"Target easy effort around {minutes}:{seconds:02d} min/km, or Garmin Zone 2."


def _auto_target_distance(readiness: Readiness, target_distance_km: float | None) -> float:
    if target_distance_km:
        return target_distance_km
    if readiness.four_week_avg_km > 0:
        if readiness.status == "green":
            return readiness.four_week_avg_km * 1.05
        if readiness.status == "yellow":
            return readiness.four_week_avg_km * 0.90
        return readiness.four_week_avg_km * 0.70
    return 25.0


def generate_today_plan(
    readiness: Readiness,
    runs: list[dict[str, Any]],
    goal: str = "general_fitness",
    today: date | None = None,
) -> dict:
    today = today or date.today()
    runs = _recent_runs(runs, today)
    last_run = runs[0] if runs else None
    days_since_last_run = (_parse_run_date(last_run) - today).days * -1 if last_run else None
    avg_pace = _avg_easy_pace(runs[:10])

    # Protect against stacking hard work after a run or when readiness is low.
    if readiness.status == "red":
        title = "Rest / recovery only"
        distance = 0.0
        details = "Skip running today. Easy walk and mobility are okay. Resume only when you feel normal."
    elif last_run and days_since_last_run == 0:
        title = "Already ran today"
        distance = 0.0
        details = "No second run. Focus on recovery, food, hydration, and sleep."
    elif readiness.status == "yellow":
        title = "Easy recovery run"
        distance = 4.0
        details = _pace_text(avg_pace, modifier_sec=45) + " Keep it short; stop early if legs feel heavy."
    elif today.weekday() in (0, 2):  # Mon/Wed quality-capable days
        title = "Quality run" if goal != "base" else "Easy aerobic run"
        distance = 7.0
        details = (
            "Warm up 10 min, then 3 x 8 min comfortably hard with 3 min easy jog, cool down."
            if title == "Quality run"
            else _pace_text(avg_pace, modifier_sec=30)
        )
    elif today.weekday() == 4:  # Friday long run, matching your preference
        title = "Long run"
        distance = 10.0
        details = _pace_text(avg_pace, modifier_sec=35) + " Keep it fully conversational."
    elif today.weekday() == 5:
        title = "Recovery run"
        distance = 5.0
        details = _pace_text(avg_pace, modifier_sec=45)
    else:
        title = "Rest or mobility"
        distance = 0.0
        details = "No run planned today. Optional 20–30 min strength/mobility."

    return {
        "date": today.isoformat(),
        "readiness": readiness.__dict__,
        "last_run": dict(last_run) if last_run else None,
        "recommendation": {
            "title": title,
            "distance_km": distance,
            "details": details,
        },
    }


def generate_weekly_plan(
    readiness: Readiness,
    runs: list[dict[str, Any]] | None = None,
    target_distance_km: float | None = None,
    goal: str = "general_fitness",
    start_date: date | None = None,
) -> dict:
    start_date = start_date or date.today()
    runs = runs or []
    target_distance_km = _auto_target_distance(readiness, target_distance_km)

    if readiness.status == "red":
        planned_km = min(target_distance_km * 0.65, max(12, readiness.four_week_avg_km * 0.75))
        intensity = "recovery"
    elif readiness.status == "yellow":
        planned_km = min(target_distance_km * 0.85, max(16, readiness.four_week_avg_km * 0.90))
        intensity = "cautious"
    else:
        baseline = readiness.four_week_avg_km or target_distance_km * 0.8
        planned_km = min(target_distance_km, baseline * 1.08)
        intensity = "normal"

    planned_km = _round_half(max(10, planned_km))
    avg_pace = _avg_easy_pace(_recent_runs(runs, start_date)[:10])

    easy1 = _round_half(planned_km * 0.22)
    quality = _round_half(planned_km * 0.24)
    long_run = _round_half(planned_km * 0.34)
    recovery = _round_half(max(3, planned_km - easy1 - quality - long_run))

    quality_text = "tempo intervals" if goal != "base" and intensity == "normal" else "easy aerobic"
    quality_details = (
        "Warm up 10 min, then 3 x 8 min comfortably hard with 3 min easy jog, cool down."
        if quality_text == "tempo intervals"
        else _pace_text(avg_pace, modifier_sec=35) + " No workout intensity this week."
    )

    days = [
        (0, "Easy run", easy1, _pace_text(avg_pace, modifier_sec=35)),
        (1, "Strength / mobility", 0, "20–30 min calves, glutes, core. Keep it controlled."),
        (2, quality_text.title(), quality, quality_details),
        (3, "Rest", 0, "Full rest or easy walk."),
        (4, "Long run", long_run, _pace_text(avg_pace, modifier_sec=40) + " Do not push the pace."),
        (5, "Recovery run", recovery, _pace_text(avg_pace, modifier_sec=50) + " Stop early if legs feel heavy."),
        (6, "Rest", 0, "Rest day."),
    ]

    return {
        "start_date": start_date.isoformat(),
        "goal": goal,
        "readiness": readiness.__dict__,
        "planned_distance_km": planned_km,
        "intensity_mode": intensity,
        "activities": [
            {
                "date": (start_date + timedelta(days=offset)).isoformat(),
                "title": title,
                "distance_km": distance,
                "details": details,
            }
            for offset, title, distance, details in days
        ],
    }
