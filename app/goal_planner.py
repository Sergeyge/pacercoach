from __future__ import annotations

import json
import math
import sqlite3
import sys
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
    # Fallback for a quality day whose phase can't be resolved (a plan predating
    # phase support). Such a day is pushed as one continuous run at its target
    # pace, so this must describe that and not promise intervals — the prose used
    # to prescribe 3 x 8 min the publisher never built.
    "quality": "Steady continuous run near goal pace — no training phase is set, so no interval structure is prescribed.",
    "rest": "Rest day — optional 20–30 min strength / mobility.",
}

# --- Long-term periodization (base → build → peak → taper) -------------------

PHASE_ORDER = ["base", "build", "peak", "taper"]

PHASE_FOCUS = {
    "base": "Aerobic foundation — easy volume and strides; build the engine.",
    "build": "Race-specific quality — threshold tempo work on a growing base.",
    "peak": "Highest load — longest long runs and race-pace sessions.",
    "taper": "Volume drops, a touch of intensity stays — arrive at race day fresh.",
}

# What a "quality" day actually is, per phase. One entry defines the whole
# session: the prose, the day's target pace and the steps pushed to the watch are
# all derived from it, so a RULE-GENERATED day cannot describe a different workout
# than it pushes. (A coach-adapted day stores the model's own prose while the steps
# still come from `structure`, so those two can still drift — see `_merge_clamp`
# and the `details` rule in `openai_client._SYSTEM_PROMPT`.) Before this table the
# base-phase prose said strides while the pace said near-goal tempo and the watch
# got 3 x 8 min, and the peak/taper prose promised intervals never built.
#
#   structure      value stored in planned_workout.structure and dispatched on
#                  when building the watch workout
#   easy_body      True  -> the planned distance MINUS the timed tail (reps +
#                          cool-down, converted at the easy pace) is run at easy
#                          pace and the reps are appended — a strides day, which
#                          is not a hard session
#                  False -> time-based warm-up, then the reps, then a cool-down;
#                          the planned distance is metadata only
#   pace_anchor    which pace the day is run at, resolved by `pace_for`:
#                    'easy'      -> paces['easy']      (a strides day's body)
#                    'threshold' -> paces['threshold'] (measured lactate threshold,
#                                   falling back to the goal-anchored 'quality'
#                                   pace when Garmin has established no LT)
#                    'goal'      -> paces['quality']   (goal race pace - 5s)
#                  Build trains THRESHOLD and peak/taper rehearse RACE PACE. Before
#                  this every phase used the goal-anchored pace, so for any goal
#                  whose race pace sits slower than the athlete's threshold — which
#                  is normal for a half or a marathon, where race pace is threshold
#                  +15..25 s/km — no phase ever trained threshold at all.
#   reps_at_pace   whether the work intervals carry the day's pace target;
#                  strides are run fast by feel, so they do not. Currently always
#                  `not easy_body`; kept separate so a future shape can combine an
#                  easy body with paced reps.
QUALITY_SHAPES: dict[str, dict[str, Any]] = {
    "base": {
        "pace_anchor": "easy",
        "structure": "strides",
        "title": "Easy run + strides",
        "easy_body": True,
        "reps_at_pace": False,
        "reps": 6,
        "work_seconds": 20,
        "recovery_seconds": 60,
        "warmup_seconds": 0,
        "cooldown_seconds": 300,
        "prose": (
            "Easy aerobic run, then {reps} x {work_seconds}s relaxed strides "
            "({recovery_seconds}s jog between each) and an easy cool-down. "
            "The running stays at easy pace — the strides are fast but short and by feel, "
            "so this is not a hard session."
        ),
    },
    "build": {
        "pace_anchor": "threshold",
        "structure": "tempo",
        "title": "Tempo run",
        "easy_body": False,
        "reps_at_pace": True,
        "reps": 3,
        "work_seconds": 480,
        "recovery_seconds": 180,
        "warmup_seconds": 600,
        "cooldown_seconds": 600,
        "prose": (
            "Warm up {warmup_minutes} min, then {reps} x {work_minutes} min at threshold — "
            "comfortably hard, the pace you could hold for about an hour — with "
            "{recovery_minutes} min jog recoveries, cool down {cooldown_minutes} min."
        ),
    },
    "peak": {
        "pace_anchor": "goal",
        "structure": "intervals",
        "title": "Race-pace intervals",
        "easy_body": False,
        "reps_at_pace": True,
        "reps": 4,
        "work_seconds": 360,
        "recovery_seconds": 180,
        "warmup_seconds": 600,
        "cooldown_seconds": 600,
        "prose": (
            "Warm up {warmup_minutes} min, then {reps} x {work_minutes} min at goal pace with "
            "{recovery_minutes} min jog recoveries, cool down {cooldown_minutes} min."
        ),
    },
    "taper": {
        "pace_anchor": "goal",
        "structure": "sharpener",
        "title": "Race-pace sharpener",
        "easy_body": False,
        "reps_at_pace": True,
        "reps": 2,
        "work_seconds": 300,
        "recovery_seconds": 180,
        "warmup_seconds": 600,
        "cooldown_seconds": 600,
        "prose": (
            "Warm up {warmup_minutes} min, then {reps} x {work_minutes} min at goal pace "
            "({recovery_minutes} min jog between) — short and controlled, just staying sharp. "
            "Cool down {cooldown_minutes} min."
        ),
    },
}

SHAPES_BY_STRUCTURE: dict[str, dict[str, Any]] = {s["structure"]: s for s in QUALITY_SHAPES.values()}


def _shape_prose(shape: dict[str, Any]) -> str:
    """Render a shape's prose from its own numbers, so wording and steps agree."""
    return shape["prose"].format(  # noqa: E501 - placeholders are validated at import (below)
        reps=shape["reps"],
        work_seconds=shape["work_seconds"],
        recovery_seconds=shape["recovery_seconds"],
        work_minutes=round(shape["work_seconds"] / 60),
        recovery_minutes=round(shape["recovery_seconds"] / 60),
        warmup_minutes=round(shape["warmup_seconds"] / 60),
        cooldown_minutes=round(shape["cooldown_seconds"] / 60),
    )


# Fail at import rather than at 06:00: an unknown placeholder in a `prose`
# template, or two shapes sharing a `structure` name (which would make one of them
# unreachable and push the other's session), are configuration mistakes worth
# catching on deploy.
for _phase, _shape in QUALITY_SHAPES.items():
    _shape_prose(_shape)
assert len(SHAPES_BY_STRUCTURE) == len(QUALITY_SHAPES), "QUALITY_SHAPES structure names must be unique"

# Watch/notification titles per session kind. Quality days take their title from
# their shape instead (see `title_for`).
_TITLES = {"easy": "Easy run", "long": "Long run", "recovery": "Recovery run", "quality": "Quality run", "rest": "Rest day"}


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


def shape_for(kind: str, *, phase: str | None = None) -> dict[str, Any] | None:
    """The quality shape for a day, or None when the day isn't a shaped session.

    Returns None for a non-quality kind AND for a quality day whose phase can't
    be resolved. Guessing a shape for an unknown phase would mean prescribing
    intervals nobody asked for, so an unknown phase degrades to a plain steady
    run at the day's own pace rather than to the harder session.
    """
    if kind != "quality" or phase is None:
        return None
    return QUALITY_SHAPES.get(phase)


def details_for(kind: str, pace_sec: int | None, *, phase: str | None = None) -> str:
    """The athlete-facing prescription for a day.

    `phase` is keyword-only for the same reason as its siblings: `phase` and
    `structure` are both `str | None`, and passing a structure name here silently
    returned the generic fallback prose instead of raising.
    """
    shape = shape_for(kind, phase=phase)
    base = _shape_prose(shape) if shape else _DETAILS.get(kind, "")
    p = pace_text(pace_sec)
    return f"{base} Target {p}.".strip() if p else base


def is_strides_session(kind: str, *, phase: str | None = None) -> bool:
    """True for a base-phase quality day — an easy-pace run plus strides rather
    than a hard session. Used for the day's target pace, the readiness-easing
    decision, and the `is_hard_session` flag handed to the coach. The title and
    the pushed workout come from the shape itself (`structure_for`/`title_for`).
    """
    shape = shape_for(kind, phase=phase)
    return bool(shape and shape["easy_body"])


def pace_for(kind: str, paces: dict[str, Any], *, phase: str | None = None) -> int | None:
    """The day's target pace, resolved through the shape's `pace_anchor`.

    A strides day runs at EASY pace — the strides themselves are by feel — so it
    must not inherit the near-goal-pace quality target, which would describe the
    session as a tempo run. A build-phase tempo runs at measured THRESHOLD, which
    for a half or marathon goal is meaningfully faster than the goal-anchored
    'quality' pace; peak and taper rehearse race pace and so use that one.

    An unknown anchor, and a 'threshold' anchor on a plan that carries no measured
    threshold, both fall back to the goal-anchored pace — the value every plan
    has, so a plan built before threshold anchoring behaves exactly as it did.
    """
    shape = shape_for(kind, phase=phase)
    if shape:
        anchor = shape.get("pace_anchor", "goal")
        if anchor == "easy":
            return paces.get("easy")
        if anchor == "threshold":
            return paces.get("threshold") or paces.get("quality")
        return paces.get("quality")
    return paces.get(kind)


def structure_for(kind: str, *, phase: str | None = None) -> str | None:
    """Session shape name for the watch ('strides'/'tempo'/'intervals'/
    'sharpener'), or None for a plain steady run.

    Stored on the planned day (planned_workout.structure) so a push normally
    builds the session from data instead of re-deriving the phase. Rows written
    before the column existed still fall back through `row_structure`.
    """
    shape = shape_for(kind, phase=phase)
    return str(shape["structure"]) if shape else None


def title_for(kind: str, *, structure: str | None = None) -> str:
    """Session title as it appears on the watch and in the morning message.

    `structure` is keyword-only on purpose: it and `phase` are both `str | None`
    on sibling functions here, so a positional call passing a phase by mistake
    would return the wrong title with no error. That title then feeds the
    publisher's sniffing fallback, which is how a strides day becomes a tempo one.
    """
    shape = SHAPES_BY_STRUCTURE.get(structure or "")
    return str(shape["title"]) if shape else _TITLES.get(kind, "Run")


def phase_name_for(day: date) -> str | None:
    """Phase name ('base'/'build'/'peak'/'taper') for `day` on the active plan.
    None when there is no plan, or the plan predates phase support."""
    ctx = active_phase_context(day)
    return ctx.get("phase") if ctx else None


def row_structure(row: Any, plan_date: date) -> str | None:
    """A planned day's session shape: the stored value when present, otherwise
    derived from the plan phase for rows written before it was persisted.

    Only quality days can have a shape, so non-quality kinds return immediately
    rather than paying for a plan lookup on every easy/long/recovery/rest day.
    """
    kind = row["kind"]
    if kind != "quality":
        return None
    try:
        stored = row["structure"]
    except (KeyError, IndexError):
        stored = None
    if stored:
        return str(stored)
    phase = phase_name_for(plan_date)
    if phase is None:
        # Legacy row and no resolvable phase: fall back to a plain steady run
        # rather than inventing intervals, and say so — silently choosing a
        # shape here is what produced tempo sessions on strides days.
        print(
            f"[goal_planner.row_structure] {plan_date}: quality day has no stored structure and "
            "the plan phase could not be resolved; pushing it as a plain run",
            file=sys.stderr,
            flush=True,
        )
        return None
    return structure_for(kind, phase=phase)


# Plausible band for a measured lactate-threshold pace (sec/km), matching the
# bounds used elsewhere on an athlete-supplied pace. A value outside it is a unit
# change or a bad read, not a threshold, and must not become a training target.
_THRESHOLD_MIN_SEC = 150
_THRESHOLD_MAX_SEC = 900


def derive_paces(goal_pace_sec: int, threshold_pace_sec: int | None = None) -> dict[str, int | None]:
    """Easy/long/recovery slower than goal pace; quality near goal pace.

    `threshold_pace_sec` is the athlete's MEASURED lactate-threshold pace, stored
    alongside the goal-derived values rather than replacing them: build-phase
    tempo work resolves to it (see `pace_for`) while peak and taper keep
    rehearsing race pace. Omitted or implausible, it is simply absent and every
    phase falls back to the goal-anchored pace.
    """
    paces: dict[str, int | None] = {
        "easy": goal_pace_sec + 75,
        "long": goal_pace_sec + 60,
        "recovery": goal_pace_sec + 105,
        "quality": max(goal_pace_sec - 5, 180),
        "rest": None,
    }
    if isinstance(threshold_pace_sec, (int, float)) and (
        _THRESHOLD_MIN_SEC <= int(threshold_pace_sec) <= _THRESHOLD_MAX_SEC
    ):
        paces["threshold"] = int(threshold_pace_sec)
    elif threshold_pace_sec is not None:
        print(
            f"[goal_planner.derive_paces] ignoring implausible threshold pace {threshold_pace_sec!r}",
            file=sys.stderr,
            flush=True,
        )
    return paces


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


def measured_threshold_pace(allow_fetch: bool = True) -> int | None:
    """The athlete's measured lactate-threshold pace (sec/km), or None.

    Read through the cached zone report, so a plan build normally costs no Garmin
    call and a failure costs nothing at all: without a threshold every phase
    falls back to the goal-anchored pace, which is what plans did before.
    """
    try:
        from .zones import hr_zones

        lt = hr_zones(allow_fetch=allow_fetch).get("lactate_threshold") or {}
        pace = lt.get("pace_sec_per_km")
        if not isinstance(pace, (int, float)) or pace <= 0:
            return None
        # An out-of-band reading is not a threshold, so report it the same way as
        # no reading at all. `recalibrate_paces` then keeps the stored value; if
        # this returned the bad number instead, `derive_paces` would reject it a
        # step later and the stored threshold would be dropped anyway — while the
        # result still claimed the reading was "measured".
        if not (_THRESHOLD_MIN_SEC <= int(pace) <= _THRESHOLD_MAX_SEC):
            print(
                f"[goal_planner.measured_threshold_pace] discarding implausible "
                f"threshold pace {pace!r} sec/km",
                file=sys.stderr,
                flush=True,
            )
            return None
        return int(pace)
    except Exception as exc:
        print(f"[goal_planner.measured_threshold_pace] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


def recalibrate_paces(today: date | None = None, allow_fetch: bool = True) -> dict[str, Any]:
    """Re-derive the active plan's paces from current fitness, in place.

    Exists because the paces are computed once at plan creation: a threshold that
    Garmin establishes or revises later never reaches the plan, so a build-phase
    tempo would keep running at whatever the goal implied on the day the goal was
    set. Only the stored `paces` change — the phase roadmap, start date and
    volume progression are untouched, so this is not a plan rebuild.

    Future untouched rule projections are then refreshed by `materialize`
    (`_is_stale_projection` compares the pace), while days already pushed to the
    watch, coach-adapted or athlete-set are deliberately left as they are.
    """
    from .db import get_active_plan, update_plan_progression

    today = today or date.today()
    plan = get_active_plan()
    if plan is None:
        return {"status": "error", "error": "no active plan"}
    prog = json.loads(plan["progression"])
    before = dict(prog.get("paces") or {})
    threshold = measured_threshold_pace(allow_fetch=allow_fetch)

    # A threshold we cannot read right now is NOT a threshold of zero. Garmin
    # being unreachable — or the cached zone read holding a failure — would
    # otherwise drop the stored value, and since build-phase tempo falls back to
    # the goal-anchored pace when no threshold is present, an unlucky call would
    # silently put the athlete's tempo back to race pace. Keep what we had and
    # say which happened.
    source = "measured"
    if threshold is None:
        threshold = before.get("threshold")
        source = "retained" if threshold is not None else "none"
        note = (
            f"pace recalibration could not read a threshold; kept the stored {threshold}s/km"
            if source == "retained"
            else "pace recalibration could not read a threshold and none was stored; "
            "quality days fall back to goal pace"
        )
        print(f"[goal_planner.recalibrate_paces] {note}", file=sys.stderr, flush=True)
        try:
            from .db import add_sync_log

            add_sync_log("warn", note, 0)
        except Exception as exc:
            print(f"[goal_planner.recalibrate_paces] add_sync_log: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    prog["paces"] = derive_paces(int(prog["goal_pace_sec"]), threshold)
    # Only a fresh reading means the paces now reflect current fitness; a
    # retained one must not make a stale plan look just-measured.
    if source == "measured":
        prog["paces_recalibrated_at"] = today.isoformat()
    update_plan_progression(int(plan["id"]), prog)

    # Push the new paces into the days that may still change.
    refreshed = materialize({
        "weekly_template": plan["weekly_template"],
        "progression": json.dumps(prog),
        "base_weekly_km": plan["base_weekly_km"],
    }, today, days=21)

    changed = {
        k: {"from": before.get(k), "to": v}
        for k, v in prog["paces"].items()
        if before.get(k) != v
    }
    return {
        "status": "ok",
        "threshold_pace_sec": threshold,
        # 'measured' = read from Garmin just now; 'retained' = the read failed and
        # the previously stored value was kept; 'none' = no threshold at all, so
        # quality days use the goal-anchored pace.
        "threshold_source": source,
        "paces": prog["paces"],
        "changed": changed,
        "days_refreshed": refreshed,
    }


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
    paces = derive_paces(goal_pace, measured_threshold_pace())
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


def _is_stale_projection(
    existing: sqlite3.Row,
    kind: str,
    distance_km: float,
    pace: int | None,
    details: str,
    structure: str | None,
) -> bool:
    """True when an existing row is still an untouched rule projection whose
    prescription no longer matches what the plan would generate today.

    Projections are refreshed rather than frozen, so a change to the phase rules
    (e.g. base-phase quality days moving from near-goal pace to easy pace +
    strides) or to the plan's volume reaches days that were materialized under
    the old rules. Adapted, completed and already-pushed days are never
    rewritten — the athlete has either received them on the watch or the coach
    has deliberately set them.
    """
    if existing["source"] != "rule" or existing["status"] != "planned":
        return False
    if existing["garmin_workout_id"]:
        return False
    return (
        existing["kind"] != kind
        # Distance is stored as a rounded float, so compare with a tolerance
        # well under the 0.1 km the planner rounds to.
        or abs(float(existing["distance_km"] or 0) - distance_km) > 0.01
        or existing["target_pace_sec"] != pace
        or (existing["details"] or "") != details
        or (existing["structure"] or None) != structure
    )


def materialize(plan_row: sqlite3.Row | dict[str, Any], start: date, days: int = 14) -> int:
    """Create rule-based planned_workout rows for `days` from `start`.

    Adapted, completed and already-pushed days are left untouched. Untouched
    rule projections are refreshed when the plan's rules would now prescribe
    something different (see `_is_stale_projection`).

    Returns how many days were written, so a caller that changed the plan's rules
    can report whether anything actually reached the calendar.
    """
    written = 0
    template = {int(k): v for k, v in json.loads(plan_row["weekly_template"]).items()}
    prog = json.loads(plan_row["progression"])
    paces = prog.get("paces", {})
    plan_start = date.fromisoformat(prog["start_date"])
    base = float(plan_row["base_weekly_km"])

    for offset in range(days):
        day = start + timedelta(days=offset)
        ds = day.isoformat()
        existing = get_planned_workout(ds)
        kind = template.get(day.weekday(), "rest")
        if kind == "rest":
            details = details_for("rest", None)
            if existing is None or _is_stale_projection(existing, "rest", 0.0, None, details, None):
                upsert_planned_workout(ds, "rest", 0.0, None, details, source="rule")
                written += 1
            continue
        week_index = max(0, (day - plan_start).days // 7)
        wk = weekly_volume(base, week_index, prog)
        distance = round(wk * KIND_RATIO[kind], 1)
        ph = phase_for_week(prog.get("phases") or [], week_index)
        phase = ph["name"] if ph else None
        pace = pace_for(kind, paces, phase=phase)
        details = details_for(kind, pace, phase=phase)
        structure = structure_for(kind, phase=phase)
        if existing is None or _is_stale_projection(existing, kind, distance, pace, details, structure):
            upsert_planned_workout(ds, kind, distance, pace, details, source="rule", structure=structure)
            written += 1
    return written


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
