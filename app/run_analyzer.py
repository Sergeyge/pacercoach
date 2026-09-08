from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import Any

from .db import (
    get_active_goal,
    get_active_plan,
    get_activity,
    get_activity_analysis,
    get_conn,
    get_planned_workout,
    list_planned_workouts,
    list_runs,
    list_unanalyzed_running_activities,
    save_activity_analysis,
    update_activity_sent_at,
)
from .garmin_extra import fetch_activity_splits
from .goal_planner import SHAPES_BY_STRUCTURE, phase_context, row_structure
from .lap_review import review_laps
from .notify import analysis_email_html, send_message
from .openai_client import _client, _create, current_model

_ANALYSIS_SYSTEM = (
    "You are an expert running coach reviewing the athlete's just-completed run. "
    "Write a concise, professional analysis (3-6 sentences).\n"
    "Judge the run against 'planned_workout' — the session the training plan prescribed "
    "for that day (kind, distance, target pace). An easy/recovery/long run is meant to be "
    "SLOWER than race pace: compare its pace to the planned target and 'plan_paces', and "
    "never call a correctly-paced easy run too slow against the race-goal pace. Only "
    "compare against the goal pace when the session itself targeted it (quality/race-pace "
    "work). Flag the opposite problem — easy days run too hard — explicitly.\n"
    "NEVER compare the whole-run average pace to a target that applies to only part "
    "of the run. On a structured session (warm-up, work reps, jog recoveries, cool-down) "
    "the average pace is a blend of all of those, so a perfectly executed set of reps "
    "still averages out near easy pace. 'planned_workout.target_pace_applies_to' says "
    "what the day's target pace covers; judge paced reps from 'work_intervals' — the "
    "measured reps, with their pace deltas already computed — and quote those numbers. "
    "If a structured day has no 'work_intervals', the rep paces could not be read from "
    "the watch data: say so and assess the session on distance, duration and HR instead "
    "of pinning a pace shortfall on the average. Use 'laps' for split and HR-drift "
    "observations.\n"
    "Cover: execution vs the planned session (kind, distance, pace), what went well, any "
    "concerns (e.g. HR drift, easy days run too hard), and one specific actionable "
    "takeaway. Anchor the takeaway to the athlete's actual upcoming schedule in "
    "'upcoming_days' — name the concrete next session (its day, kind, distance, target "
    "pace), never a generic 'next run'. Take the words for a day from its 'day' label — "
    "never a raw ISO date, and never a weekday you worked out yourself: a day whose "
    "'days_away' is 7 falls on the same weekday as the run being reviewed, so a bare "
    "weekday name there would point at the run the athlete just finished. Write the label "
    "as ordinary prose (tomorrow, on Friday, next Wednesday) — never in quotes. "
    "Respect 'training_phase' "
    "(base = aerobic "
    "patience, taper = freshness over fitness) when advising. If no planned_workout is "
    "provided, this was an unplanned run — say so and assess it on its own merits.\n"
    "Be direct, encouraging but honest. Use the numbers provided — do not invent data."
)


def _fmt_pace(pace_sec: Any) -> str | None:
    try:
        s = int(pace_sec)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return f"{s // 60}:{s % 60:02d}/km"


def _day_label(ds: Any, ref: date | None) -> str | None:
    """Natural label for a plan date relative to the reviewed run.

    A bare weekday name is only unambiguous for the six days after the run. The
    seventh is the run's OWN weekday coming round again, and the review is read
    on the day of the run: labelling next week's quality day "Wednesday" on a
    Wednesday made the takeaway point at the session the athlete had just
    finished. That day gets "next <weekday>".
    """
    try:
        d = date.fromisoformat(str(ds))
    except (TypeError, ValueError):
        return None
    if ref is None:
        return d.strftime("%A")
    delta = (d - ref).days
    if delta == 1:
        return "tomorrow"
    if 2 <= delta <= 6:
        return d.strftime("%A")
    return f"next {d.strftime('%A')}"


def _days_away(ds: Any, ref: date | None) -> int | None:
    """How many days after the reviewed run a plan date falls."""
    if ref is None:
        return None
    try:
        return (date.fromisoformat(str(ds)) - ref).days
    except (TypeError, ValueError):
        return None


def _fmt_duration(sec: Any) -> str | None:
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_delta(sec: Any) -> str | None:
    """Signed pace gap, e.g. '+8 s/km' slower than target, '-4 s/km' faster."""
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return None
    return f"{s:+d} s/km"


def _target_scope(shape: dict[str, Any] | None) -> str:
    """What the planned day's target pace actually covers — spelled out for the
    model, because the failure mode this guards against is it silently assuming
    the target applies to the whole run and reporting the average's shortfall."""
    if shape is None:
        return "the whole run — it is one continuous effort, so the average pace is comparable"
    if shape.get("reps_at_pace"):
        return (
            "the work reps ONLY. The warm-up, jog recoveries and cool-down are easy by "
            "feel and are part of the same activity, so the whole-run average pace is "
            "much slower than this target even when every rep is on target — never "
            "compare the two"
        )
    return (
        "the easy body of the run (the laps tagged 'body'). The strides are short and "
        "fast by feel and carry no pace target, so do not judge them on pace"
    )


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def analyze_activity(
    activity: dict[str, Any],
    goal: Any | None = None,
    recent_runs: list | None = None,
    planned: dict[str, Any] | None = None,
    plan_paces: dict[str, Any] | None = None,
    phase: dict[str, Any] | None = None,
    upcoming: list[dict[str, Any]] | None = None,
    structure: str | None = None,
    laps: list[dict[str, Any]] | None = None,
) -> str | None:
    """Generate a coach summary for one completed activity. Returns None on any failure."""
    client = _client()
    if client is None:
        return None
    context: dict[str, Any] = {
        "completed_run": {
            "date": activity.get("activity_date"),
            "distance_km": activity.get("distance_km"),
            "duration": _fmt_duration(activity.get("duration_seconds")),
            "pace": _fmt_pace(activity.get("avg_pace_sec_per_km")),
            "avg_hr": activity.get("avg_hr"),
            "calories": activity.get("calories"),
            "activity_type": activity.get("activity_type"),
        }
    }
    shape = SHAPES_BY_STRUCTURE.get(structure or "")
    if planned:
        context["planned_workout"] = {
            "kind": planned.get("kind"),
            "distance_km": planned.get("distance_km"),
            "target_pace": _fmt_pace(planned.get("target_pace_sec")),
            "structure": structure or "steady run (no interval structure)",
            "target_pace_applies_to": _target_scope(shape),
            "details": planned.get("details"),
            "coach_note": planned.get("coach_note"),
        }
    # Only a shape whose reps carry the pace target gets that target compared
    # against them; strides are run fast by feel, and the day's target pace is
    # the EASY pace of the body they hang off, so pinning it to the reps would
    # report a stride set as wildly ahead of pace.
    paced_reps = bool(shape and shape.get("reps_at_pace"))
    labelled, work = review_laps(
        laps, (planned or {}).get("target_pace_sec") if paced_reps else None, shape
    )
    if work:
        context["work_intervals"] = _prune({
            "note": (
                "the prescribed work reps as actually run — warm-up, jog recoveries "
                "and cool-down excluded. This, not 'completed_run.pace', is the "
                "execution of the day's pace target."
                if paced_reps else
                "the strides as actually run. They are short and fast by feel and carry "
                "no pace target, so report that they were done — do not judge them on pace."
            ),
            "reps_completed": work["reps_completed"],
            "reps_planned": work["reps_planned"],
            "work_distance_km": work["work_distance_km"],
            "work_duration": _fmt_duration(work["work_duration_sec"]),
            "target_pace": _fmt_pace(work.get("target_pace_sec")),
            "mean_work_pace": _fmt_pace(work["mean_work_pace_sec"]),
            "mean_vs_target": _fmt_delta(work.get("mean_delta_sec_per_km")),
            "rep_paces": [_fmt_pace(p) for p in work["rep_paces_sec"]],
            "rep_durations": [_fmt_duration(d) for d in work["rep_durations_sec"]],
            "rep_vs_target": [_fmt_delta(d) for d in work.get("rep_deltas_sec_per_km") or []] or None,
            "rep_avg_hr": [hr for hr in work["rep_avg_hr"] if hr] or None,
        })
    elif paced_reps:
        context["work_intervals"] = (
            "UNAVAILABLE — the lap data does not identify which laps were the work reps, "
            "so their paces are unknown. Say the rep paces could not be read and judge "
            "the session on distance, duration and HR; do NOT report a pace shortfall "
            "from the whole-run average."
        )
    if labelled:
        context["laps"] = [
            _prune({
                "lap": lap.get("lap"),
                "role": lap.get("role"),
                "distance_km": lap.get("distance_km"),
                "duration": _fmt_duration(lap.get("duration_sec")),
                "pace": _fmt_pace(lap.get("pace_sec")),
                "avg_hr": lap.get("avg_hr"),
            })
            for lap in labelled
        ]
    if plan_paces:
        paces = {k: _fmt_pace(v) for k, v in plan_paces.items()}
        context["plan_paces"] = {k: v for k, v in paces.items() if v}
    if phase:
        context["training_phase"] = phase
    if upcoming:
        try:
            run_day = date.fromisoformat(str(activity.get("activity_date")))
        except (TypeError, ValueError):
            run_day = None
        context["upcoming_days"] = [
            {
                "date": u.get("plan_date"),
                "day": _day_label(u.get("plan_date"), run_day),
                "days_away": _days_away(u.get("plan_date"), run_day),
                "kind": u.get("kind"),
                "distance_km": u.get("distance_km"),
                "target_pace": _fmt_pace(u.get("target_pace_sec")),
            }
            for u in upcoming
        ]
    if goal:
        gp = None
        if goal.get("distance_km"):
            gp = round(goal["target_seconds"] / goal["distance_km"])
        context["goal"] = {
            "distance_km": goal.get("distance_km"),
            "target_time": _fmt_duration(goal.get("target_seconds")),
            "target_pace": _fmt_pace(gp),
        }
    if recent_runs:
        context["recent_runs"] = [
            {
                "date": r.get("activity_date"),
                "distance_km": r.get("distance_km"),
                "pace": _fmt_pace(r.get("avg_pace_sec_per_km")),
                "avg_hr": r.get("avg_hr"),
            }
            for r in recent_runs[:5]
        ]
    try:
        resp = _create(
            client,
            current_model(),
            [
                {"role": "system", "content": _ANALYSIS_SYSTEM},
                {"role": "user", "content": json.dumps(context, default=str)},
            ],
            json_mode=False,
            # Reasoning models (GPT-5/o-series) spend this budget thinking before
            # they emit a word, and the lap/interval context made them think
            # longer: at 1500 the review came back empty with finish_reason
            # 'length'. The prose itself is 3-6 sentences.
            max_out=3000,
            temperature=0.4,
        )
        choice = resp.choices[0]
        content = choice.message.content
        if content and content.strip():
            return content.strip()
        # Empty content is not an exception, so without this the review just
        # silently doesn't happen — which is exactly how the budget being too
        # small looked from the outside.
        print(
            f"[run_analyzer.analyze_activity] empty review for {activity.get('activity_date')} "
            f"(finish_reason={getattr(choice, 'finish_reason', None)!r}); "
            "the model produced no text",
            file=sys.stderr,
            flush=True,
        )
        return None
    except Exception as exc:
        # Don't let one bad activity break the batch — but DO log so a permanent
        # OpenAI failure (revoked key, removed model, schema mismatch) is visible.
        print(f"[run_analyzer.analyze_activity] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


class _LapReader:
    """Lap splits for a batch of activities over one Garmin login.

    Laps are what let a review judge an interval session's work reps instead of
    its blended whole-run average, but they are still a nice-to-have: a Garmin
    outage must not stop the review from being written. A failed login disables
    lap reading for the rest of the batch (retrying it per activity would just
    stall the run on the same timeout); a per-activity fetch failure only costs
    that one activity its laps.
    """

    def __init__(self) -> None:
        self._client: Any | None = None
        self._disabled = False

    def laps(self, activity_id: str) -> list[dict[str, Any]] | None:
        if self._disabled:
            return None
        if self._client is None:
            try:
                from .garmin_client import get_garmin_client

                self._client = get_garmin_client()
            except Exception as exc:
                self._disabled = True
                print(
                    f"[run_analyzer] no Garmin client, reviewing without lap splits: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return None
        data = fetch_activity_splits(activity_id, client=self._client)
        laps = data.get("laps")
        if not laps:
            print(
                f"[run_analyzer] no lap splits for activity {activity_id}: "
                f"{data.get('error') or 'empty response'}",
                file=sys.stderr,
                flush=True,
            )
            return None
        # Keep them. These laps are the only record of what pace was run at what
        # heart rate, and the review below reduces them to prose — so without
        # this the data is fetched and discarded on every sync, and a per-zone
        # question can never be answered from history. A storage failure must not
        # cost the review, hence the broad catch.
        try:
            from .db import upsert_activity_laps

            upsert_activity_laps(str(activity_id), laps)
        except Exception as exc:
            print(
                f"[run_analyzer] could not store laps for {activity_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        return laps


def _plan_context(
    activity: dict[str, Any], prog: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """Plan-side context for the day a run happened on: the planned session, its
    session shape, the phase position, and the next week of planned days."""
    planned = phase = structure = None
    upcoming: list[dict[str, Any]] = []
    activity_date = str(activity.get("activity_date") or "")
    if not activity_date:
        return planned, structure, phase, upcoming
    planned_row = get_planned_workout(activity_date)
    planned = dict(planned_row) if planned_row else None
    try:
        act_day = date.fromisoformat(activity_date)
    except ValueError:
        return planned, structure, phase, upcoming
    phase = phase_context(prog, act_day) if prog else None
    # The day's session shape decides what its target pace applies to: on a
    # tempo/interval day it is the reps' target, not the whole run's.
    if planned_row is not None:
        structure = row_structure(planned_row, act_day)
    upcoming = [
        dict(u)
        for u in list_planned_workouts(
            (act_day + timedelta(days=1)).isoformat(),
            (act_day + timedelta(days=7)).isoformat(),
        )
    ]
    return planned, structure, phase, upcoming


def _plan_progression() -> dict[str, Any]:
    """The active plan's progression blob (paces, phases), or {} when there is
    no plan — the review then falls back to judging the run on the goal pace."""
    plan_row = get_active_plan()
    if plan_row is None:
        return {}
    try:
        return json.loads(plan_row["progression"]) or {}
    except (TypeError, ValueError):
        return {}


def re_review_run(source_id: str | None = None, notify: bool = False) -> dict[str, Any]:
    """Regenerate the stored review for one run, replacing the old text.

    A review is normally written once and never revisited, so a summary the coach
    got wrong stays wrong. `source_id` defaults to the most recent synced run.
    Does not email by default: a re-review is a correction the athlete is already
    looking at, not news.
    """
    if source_id is None:
        runs = list_runs(limit=1)
        if not runs:
            return {"status": "error", "error": "no runs synced yet"}
        source_id = str(runs[0]["source_id"])
    row = get_activity(source_id)
    if row is None:
        return {"status": "error", "error": f"unknown activity {source_id}"}

    activity = dict(row)
    prog = _plan_progression()
    planned, structure, phase, upcoming = _plan_context(activity, prog)
    peers = [dict(r) for r in list_runs(limit=10) if r["source_id"] != source_id]
    goal_row = get_active_goal()
    summary = analyze_activity(
        activity,
        goal=dict(goal_row) if goal_row else None,
        recent_runs=peers,
        planned=planned,
        plan_paces=prog.get("paces") or {},
        phase=phase,
        upcoming=upcoming,
        structure=structure,
        laps=_LapReader().laps(source_id),
    )
    if not summary:
        return {"status": "error", "error": "the coach returned no review; the previous one is unchanged"}
    save_activity_analysis(source_id, summary, sent_at=None)
    out = {
        "status": "ok",
        "source_id": source_id,
        "date": activity.get("activity_date"),
        "summary": summary,
    }
    if notify:
        subject = f"PACER · Run review (revised) · {activity.get('activity_date','')}"
        send = send_message(subject, summary, html=analysis_email_html(activity, summary))
        if send.get("status") == "sent":
            update_activity_sent_at(source_id)
        out["notify_status"] = send.get("status")
    return out


def analyze_new_runs_and_notify(limit: int = 5, notify: bool = True) -> dict[str, Any]:
    """Find new running activities without analysis, generate coach summaries,
    and persist them. By default also emails the summary via the configured
    notify channel; pass `notify=False` to save the analysis row without
    emailing (used by the morning job to avoid double-emails)."""
    out: dict[str, Any] = {"analyzed": 0, "skipped": 0, "results": []}
    rows = list_unanalyzed_running_activities(limit=limit)
    if not rows:
        return out

    goal_row = get_active_goal()
    goal = dict(goal_row) if goal_row else None
    recent = [dict(r) for r in list_runs(limit=10)]

    # Training-plan context so the review judges the run against the day's
    # planned session (easy pace vs easy target) instead of the race-goal pace.
    prog = _plan_progression()
    plan_paces = prog.get("paces") or {}

    lap_reader = _LapReader()

    for r in rows:
        activity = dict(r)
        source_id = activity["source_id"]
        # Belt-and-suspenders: if a parallel sync already analyzed this run, skip.
        if get_activity_analysis(source_id) is not None:
            out["skipped"] += 1
            out["results"].append({"date": activity.get("activity_date"), "status": "already_analyzed"})
            continue
        peers = [x for x in recent if x.get("source_id") != source_id]
        planned, structure, phase, upcoming = _plan_context(activity, prog)
        summary = analyze_activity(
            activity, goal=goal, recent_runs=peers,
            planned=planned, plan_paces=plan_paces, phase=phase, upcoming=upcoming,
            structure=structure, laps=lap_reader.laps(source_id),
        )
        if not summary:
            out["skipped"] += 1
            out["results"].append({"date": activity.get("activity_date"), "status": "skipped"})
            continue
        # SAVE FIRST — claims the slot so no concurrent/later call can re-process
        # this activity. Even if the email send fails the row is in place →
        # strict once-per-activity email guarantee.
        save_activity_analysis(source_id, summary, sent_at=None)
        out["analyzed"] += 1
        if not notify:
            out["results"].append({
                "date": activity.get("activity_date"),
                "distance_km": activity.get("distance_km"),
                "notify_status": "suppressed",
            })
            continue
        subject = f"PACER · Run review · {activity.get('activity_date','')}"
        send = send_message(subject, summary, html=analysis_email_html(activity, summary))
        if send.get("status") == "sent":
            update_activity_sent_at(source_id)
        out["results"].append({
            "date": activity.get("activity_date"),
            "distance_km": activity.get("distance_km"),
            "notify_status": send.get("status"),
        })
    return out
