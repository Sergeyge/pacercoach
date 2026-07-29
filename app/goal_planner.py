from __future__ import annotations

import json
import math
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

# --- Long-term periodization (base → build → peak → taper) -------------------

PHASE_ORDER = ["base", "build", "peak", "taper"]

PHASE_FOCUS = {
    "base": "Aerobic foundation — easy volume and strides; build the engine.",
    "build": "Race-specific quality — tempo and intervals at goal pace on a growing base.",
    "peak": "Highest load — longest long runs and race-pace sessions.",
    "taper": "Volume drops, a touch of intensity stays — arrive at race day fresh.",
}

# Quality-day prescription varies by phase (base stays light, taper stays short).
_PHASE_QUALITY = {
    "base": "Warm up 10 min, then 6-8 x 20s relaxed strides with full recovery; keep the rest easy.",
    "build": "Warm up 10 min, then tempo work (e.g. 3 x 8 min) near goal pace, cool down 10 min.",
    "peak": "Warm up 10 min, then race-pace intervals (e.g. 4-5 x 6 min at goal pace), cool down 10 min.",
    "taper": "Warm up 10 min, then 2 x 5 min at goal pace — short and controlled, just staying sharp.",
}


def default_total_weeks(distance_km: float) -> int:
    """Plan length when no race date is given: 10K→10w, half→14w, marathon→16w."""
    if distance_km <= 12:
        return 10
    if distance_km <= 30:
        return 14
    return 16


def split_phase_weeks(total_weeks: int) -> dict[str, int]:
    """Distribute total weeks across phases (~40/30/20% + 1-2 week taper)."""
    total = max(4, int(total_weeks))
    taper = 1 if total <= 11 else 2
    peak = max(1, round(total * 0.2))
    build = max(1, round(total * 0.3))
    base = total - build - peak - taper
    if base < 1:
        build = max(1, build + base - 1)
        base = total - build - peak - taper
    return {"base": base, "build": build, "peak": peak, "taper": taper}


def compute_phases(start: date, total_weeks: int) -> list[dict[str, Any]]:
    weeks = split_phase_weeks(total_weeks)
    phases: list[dict[str, Any]] = []
    w = 0
    for name in PHASE_ORDER:
        n = weeks[name]
        if n <= 0:
            continue
        phases.append(
            {
                "name": name,
                "start_week": w,
                "weeks": n,
                "start": (start + timedelta(weeks=w)).isoformat(),
                "end": (start + timedelta(weeks=w + n, days=-1)).isoformat(),
                "focus": PHASE_FOCUS[name],
            }
        )
        w += n
    return phases


def phase_for_week(phases: list[dict[str, Any]], week_index: int) -> dict[str, Any] | None:
    for ph in phases or []:
        if ph["start_week"] <= week_index < ph["start_week"] + ph["weeks"]:
            return ph
    return None


def phase_context(prog: dict[str, Any], today: date) -> dict[str, Any] | None:
    """Where the athlete stands in the long-term plan. None for plans created
    before phase support (they have no 'phases' in progression)."""
    phases = prog.get("phases")
    if not phases:
        return None
    try:
        plan_start = date.fromisoformat(prog["start_date"])
    except (KeyError, ValueError):
        return None
    week_index = max(0, (today - plan_start).days // 7)
    total_weeks = int(prog.get("total_weeks") or (phases[-1]["start_week"] + phases[-1]["weeks"]))
    ph = phase_for_week(phases, week_index)
    if ph is None:  # past race day — report as the last week of taper
        ph = phases[-1]
        week_index = min(week_index, total_weeks - 1)
    race_date = prog.get("race_date")
    if race_date:
        try:
            weeks_to_race = max(0, round((date.fromisoformat(race_date) - today).days / 7))
        except ValueError:
            weeks_to_race = max(0, total_weeks - week_index)
    else:
        weeks_to_race = max(0, total_weeks - week_index)
    return {
        "race_date": race_date,
        "total_weeks": total_weeks,
        "current_week": week_index + 1,
        "weeks_to_race": weeks_to_race,
        "phase": ph["name"],
        "phase_week": week_index - ph["start_week"] + 1,
        "phase_weeks": ph["weeks"],
        "phase_focus": ph["focus"],
    }


def active_phase_context(today: date | None = None) -> dict[str, Any] | None:
    """phase_context for the active plan (None if no plan / pre-phase plan)."""
    from .db import get_active_plan

    plan = get_active_plan()
    if plan is None:
        return None
    try:
        prog = json.loads(plan["progression"])
    except (TypeError, ValueError):
        return None
    return phase_context(prog, today or date.today())


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


def details_for(kind: str, pace_sec: int | None, phase: str | None = None) -> str:
    if kind == "quality" and phase in _PHASE_QUALITY:
        base = _PHASE_QUALITY[phase]
    else:
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
    """Starting weekly volume, anchored on where the athlete actually is.

    The 4-week average alone under-shoots when the athlete is trending up
    (early light weeks dilute it), so the last 7 days set the anchor —
    discounted 10% as spike protection and capped at 1.35x the 4-week
    average so one big week can't set an unsafe start. A light recent week
    never drags the start below the 4-week average."""
    week_cutoff = (today - timedelta(days=7)).isoformat()
    month_cutoff = (today - timedelta(days=28)).isoformat()
    last7 = sum(r["distance_km"] for r in runs if str(r["activity_date"]) >= week_cutoff)
    total = sum(r["distance_km"] for r in runs if str(r["activity_date"]) >= month_cutoff)
    avg = total / 4 if total else 0.0
    base = max(avg, min(last7 * 0.9, avg * 1.35)) if avg else 0.0
    return round(max(15.0, base), 1)  # never start below a sane floor


def weekly_volume(base: float, week_index: int, prog: dict[str, Any]) -> float:
    phases = prog.get("phases") or []
    ph = phase_for_week(phases, week_index)
    if ph is None and phases and week_index >= phases[-1]["start_week"]:
        ph = phases[-1]  # past race day — keep prescribing taper-level volume
    if ph and ph["name"] == "taper":
        # Taper is relative to the volume reached at the end of peak: ~65% of
        # peak, dropping to ~45% on race week.
        peak_week = max(0, ph["start_week"] - 1)
        peak_vol = min(base * ((1 + prog["weekly_increase"]) ** peak_week), prog["cap_km"])
        weeks_to_race = ph["start_week"] + ph["weeks"] - week_index
        vol = peak_vol * (0.45 if weeks_to_race <= 1 else 0.65)
        return round(vol * 2) / 2
    vol = base * ((1 + prog["weekly_increase"]) ** week_index)
    down_every = prog.get("down_week_every")
    if down_every and (week_index + 1) % down_every == 0:
        vol *= prog.get("down_factor", 0.85)
    cap = prog["cap_km"] * (0.85 if ph and ph["name"] == "base" else 1.0)
    vol = min(vol, cap)
    return round(vol * 2) / 2  # nearest 0.5 km


def build_and_store_plan(
    goal_id: int,
    distance_km: float,
    target_seconds: int,
    runs: list[dict[str, Any]],
    today: date | None = None,
    race_date: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    goal_pace = round(target_seconds / distance_km)
    paces = derive_paces(goal_pace)
    base = _base_weekly_km(runs, today)
    if race_date is not None:
        total_weeks = max(4, math.ceil((race_date - today).days / 7))
    else:
        total_weeks = default_total_weeks(distance_km)
        race_date = today + timedelta(weeks=total_weeks)
    phases = compute_phases(today, total_weeks)
    progression = {
        "weekly_increase": 0.07,
        "down_week_every": 4,
        "down_factor": 0.85,
        "cap_km": round(distance_km * 2.2, 1),
        "start_date": today.isoformat(),
        "goal_pace_sec": goal_pace,
        "paces": paces,
        "race_date": race_date.isoformat(),
        "total_weeks": total_weeks,
        "phases": phases,
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
        "race_date": race_date.isoformat(),
        "total_weeks": total_weeks,
        "phases": [{"name": p["name"], "weeks": p["weeks"], "start": p["start"], "end": p["end"]} for p in phases],
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
        ph = phase_for_week(prog.get("phases") or [], week_index)
        upsert_planned_workout(ds, kind, distance, pace, details_for(kind, pace, ph["name"] if ph else None), source="rule")


def plan_phase_overview(today: date | None = None) -> dict[str, Any]:
    """The full long-term picture for the dashboard roadmap: every phase with
    dates, weekly-volume range and status (done/current/upcoming), plus where
    the athlete currently stands (week X of Y, weeks to race)."""
    from .db import get_active_goal, get_active_plan

    today = today or date.today()
    goal = get_active_goal()
    plan = get_active_plan()
    if goal is None or plan is None:
        return {"phases": [], "message": "no active goal"}
    try:
        prog = json.loads(plan["progression"])
    except (TypeError, ValueError):
        return {"phases": [], "message": "plan progression unreadable"}
    phases = prog.get("phases") or []
    if not phases:
        return {
            "phases": [],
            "message": "This plan predates phase support — set the goal again (add your race day) to generate a phased roadmap.",
        }
    base = float(plan["base_weekly_km"])
    ctx = phase_context(prog, today) or {}
    try:
        plan_start = date.fromisoformat(prog["start_date"])
    except (KeyError, ValueError):
        plan_start = today
    out_phases = []
    for ph in phases:
        vols = [weekly_volume(base, w, prog) for w in range(ph["start_week"], ph["start_week"] + ph["weeks"])]
        # Dates derived from start_date + start_week (not the stored absolute
        # dates) so a pause/resume start-date shift keeps the roadmap aligned.
        ph_start = plan_start + timedelta(weeks=ph["start_week"])
        ph_end = ph_start + timedelta(weeks=ph["weeks"], days=-1)
        if today > ph_end:
            status = "done"
        elif today < ph_start:
            status = "upcoming"
        else:
            status = "current"
        out_phases.append(
            {
                "name": ph["name"],
                "start": ph_start.isoformat(),
                "end": ph_end.isoformat(),
                "weeks": ph["weeks"],
                "focus": ph["focus"],
                "weekly_km_min": min(vols),
                "weekly_km_max": max(vols),
                "status": status,
            }
        )
    return {
        "start_date": prog.get("start_date"),
        "goal": {"distance_km": goal["distance_km"], "target_seconds": goal["target_seconds"]},
        **ctx,
        "phases": out_phases,
    }


def resume_active_goal_and_shift(today: date | None = None) -> dict[str, Any]:
    """Resume the active goal AND adjust the plan calendar to reflect the
    time that passed paused:
      1. Compute pause_days = today - paused_at_date
      2. Clear the pause fields on the goal row
      3. Delete all status='paused' workouts + any future status='planned' rows
         (so we re-materialize them cleanly with the shifted start date)
      4. Shift training_plan.progression.start_date forward by pause_days so
         the weekly progression continues where it left off (week 3 day 5 stays
         week 3 day 5 — just on later calendar dates)
      5. Re-materialize 21 days of workouts from today
    Returns a small status dict; returns {"status": "not_paused"} if the goal
    wasn't actually paused.
    """
    from datetime import datetime, timedelta
    from .db import (
        clear_paused_planned,
        delete_planned_from,
        get_active_goal,
        get_active_plan,
        resume_active_goal,
        shift_active_plan_start,
    )

    today = today or date.today()
    g = get_active_goal()
    if g is None or g["paused_at"] is None:
        return {"status": "not_paused"}

    # Compute calendar days paused. paused_at is an ISO UTC timestamp like
    # 2026-05-29T03:55:12.123456Z — take just the date portion.
    try:
        paused_at = datetime.fromisoformat(g["paused_at"].replace("Z", "")).date()
    except (ValueError, AttributeError):
        paused_at = today
    pause_days = max(0, (today - paused_at).days)

    resume_active_goal()
    clear_paused_planned()
    # Any future 'planned' rows that snuck in (e.g. from ensure_horizon races)
    # also need to be wiped before re-materializing on the shifted schedule.
    # delete_planned_from also returns Garmin workout ids it found — for an
    # already-pushed day we leave the watch alone (the user can re-push if
    # the post-resume workout is different).
    delete_planned_from(today.isoformat())
    new_start = shift_active_plan_start(pause_days) if pause_days > 0 else None
    plan_row = get_active_plan()
    if plan_row is not None:
        materialize(plan_row, today, days=21)
    return {
        "status": "resumed",
        "pause_days": pause_days,
        "plan_start_shifted_by_days": pause_days,
        "new_plan_start_date": new_start,
    }
