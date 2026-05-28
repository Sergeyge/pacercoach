from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .db import delete_planned_from, get_planned_workout, save_plan, upsert_planned_workout

# Weekday (Mon=0 .. Sun=6) -> session kind. Matches the agreed default week:
# Mon easy · Tue strength/rest · Wed quality · Thu rest · Fri long · Sat recovery · Sun rest.
WEEKDAY_TEMPLATE: dict[int, str] = {0: "easy", 1: "rest", 2: "quality", 3: "rest", 4: "long", 5: "recovery", 6: "rest"}

# Share of the week's running volume per session kind (run days sum to 1.0).
KIND_RATIO: dict[str, float] = {"easy": 0.24, "quality": 0.22, "long": 0.34, "recovery": 0.20, "rest": 0.0}

_DETAILS = {
    "easy": "Easy aerobic run, fully conversational.",
    "long": "Long run, steady and relaxed — do not push the pace.",
    "recovery": "Very easy recovery run; stop early if legs feel heavy.",
    "quality": "Warm up 10 min, then tempo work (e.g. 3 x 8 min) near goal pace, cool down 10 min.",
    "rest": "Rest day — optional 20–30 min strength / mobility.",
}


def parse_target_time(value: str | int | float) -> int:
    """Accept 'H:MM:SS', 'MM:SS', or a number of seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    parts = str(value).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"invalid target_time: {value!r}") from exc
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        return nums[0]
    else:
        raise ValueError(f"invalid target_time: {value!r}")
    return h * 3600 + m * 60 + s


def pace_text(pace_sec: int | None) -> str:
    if not pace_sec:
        return ""
    return f"{pace_sec // 60}:{pace_sec % 60:02d} min/km"


def details_for(kind: str, pace_sec: int | None) -> str:
    base = _DETAILS.get(kind, "")
    p = pace_text(pace_sec)
    return f"{base} Target {p}.".strip() if p else base


def derive_paces(goal_pace_sec: int) -> dict[str, int | None]:
    """Easy/long/recovery slower than goal pace; quality near goal pace."""
    return {
        "easy": goal_pace_sec + 75,
        "long": goal_pace_sec + 60,
        "recovery": goal_pace_sec + 105,
        "quality": max(goal_pace_sec - 5, 180),
        "rest": None,
    }


def _base_weekly_km(runs: list[dict[str, Any]], today: date) -> float:
    cutoff = today - timedelta(days=28)
    total = sum(r["distance_km"] for r in runs if str(r["activity_date"]) >= cutoff.isoformat())
    avg = total / 4 if total else 0.0
    return round(max(15.0, avg), 1)  # never start below a sane floor


def weekly_volume(base: float, week_index: int, prog: dict[str, Any]) -> float:
    vol = base * ((1 + prog["weekly_increase"]) ** week_index)
    down_every = prog.get("down_week_every")
    if down_every and (week_index + 1) % down_every == 0:
        vol *= prog.get("down_factor", 0.85)
    vol = min(vol, prog["cap_km"])
    return round(vol * 2) / 2  # nearest 0.5 km


def build_and_store_plan(goal_id: int, distance_km: float, target_seconds: int, runs: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    goal_pace = round(target_seconds / distance_km)
    paces = derive_paces(goal_pace)
    base = _base_weekly_km(runs, today)
    progression = {
        "weekly_increase": 0.07,
        "down_week_every": 4,
        "down_factor": 0.85,
        "cap_km": round(distance_km * 2.2, 1),
        "start_date": today.isoformat(),
        "goal_pace_sec": goal_pace,
        "paces": paces,
    }
    plan_id = save_plan(goal_id, base, WEEKDAY_TEMPLATE, progression)

    # Drop the old plan's future days and remove their Garmin counterparts so
    # the user's watch calendar doesn't keep showing workouts for the previous
    # goal (and so a re-push doesn't duplicate them).
    dropped_garmin_ids = delete_planned_from(today.isoformat())
    if dropped_garmin_ids:
        import sys

        try:
            from .workout_publisher import delete_garmin_workout

            for wid in dropped_garmin_ids:
                try:
                    res = delete_garmin_workout(str(wid))
                    if isinstance(res, dict) and res.get("status") != "ok":
                        print(
                            f"[goal_planner.cleanup_garmin] non-ok delete for {wid}: {res}",
                            file=sys.stderr,
                            flush=True,
                        )
                except Exception as exc:
                    print(
                        f"[goal_planner.cleanup_garmin] {type(exc).__name__} on {wid}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
        except Exception as exc:
            print(
                f"[goal_planner.cleanup_garmin] import/setup failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    plan_row = {"base_weekly_km": base, "weekly_template": json.dumps(WEEKDAY_TEMPLATE), "progression": json.dumps(progression)}
    materialize(plan_row, today, days=21)

    return {
        "plan_id": plan_id,
        "goal_pace": pace_text(goal_pace),
        "base_weekly_km": base,
        "peak_weekly_km": progression["cap_km"],
        "paces": {k: pace_text(v) for k, v in paces.items() if v},
    }


def materialize(plan_row: sqlite3.Row | dict[str, Any], start: date, days: int = 14) -> None:
    """Create rule-based planned_workout rows for `days` from `start`.

    Existing rows (adapted or completed) are left untouched.
    """
    template = {int(k): v for k, v in json.loads(plan_row["weekly_template"]).items()}
    prog = json.loads(plan_row["progression"])
    paces = prog.get("paces", {})
    plan_start = date.fromisoformat(prog["start_date"])
    base = float(plan_row["base_weekly_km"])

    for offset in range(days):
        day = start + timedelta(days=offset)
        ds = day.isoformat()
        if get_planned_workout(ds) is not None:
            continue  # keep adapted/completed/existing days
        kind = template.get(day.weekday(), "rest")
        if kind == "rest":
            upsert_planned_workout(ds, "rest", 0.0, None, details_for("rest", None), source="rule")
            continue
        week_index = max(0, (day - plan_start).days // 7)
        wk = weekly_volume(base, week_index, prog)
        distance = round(wk * KIND_RATIO[kind], 1)
        pace = paces.get(kind)
        upsert_planned_workout(ds, kind, distance, pace, details_for(kind, pace), source="rule")
