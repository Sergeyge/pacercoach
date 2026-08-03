from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import Any

from .db import (
    get_active_goal,
    get_active_plan,
    get_activity_analysis,
    get_conn,
    get_planned_workout,
    list_planned_workouts,
    list_runs,
    list_unanalyzed_running_activities,
    save_activity_analysis,
    update_activity_sent_at,
)
from .goal_planner import phase_context
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
    "Cover: execution vs the planned session (kind, distance, pace), what went well, any "
    "concerns (e.g. HR drift, easy days run too hard), and one specific actionable "
    "takeaway. Anchor the takeaway to the athlete's actual upcoming schedule in "
    "'upcoming_days' — name the concrete next session (its day, kind, distance, target "
    "pace), never a generic 'next run'. Refer to days naturally by their 'day' label "
    "(e.g. 'tomorrow', 'on Wednesday'), never by raw ISO dates. Respect 'training_phase' "
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
    """Natural label for a plan date relative to the reviewed run: 'tomorrow'
    for the day right after it, else the weekday name."""
    try:
        d = date.fromisoformat(str(ds))
    except (TypeError, ValueError):
        return None
    if ref is not None and (d - ref).days == 1:
        return "tomorrow"
    return d.strftime("%A")


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


def analyze_activity(
    activity: dict[str, Any],
    goal: Any | None = None,
    recent_runs: list | None = None,
    planned: dict[str, Any] | None = None,
    plan_paces: dict[str, Any] | None = None,
    phase: dict[str, Any] | None = None,
    upcoming: list[dict[str, Any]] | None = None,
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
    if planned:
        context["planned_workout"] = {
            "kind": planned.get("kind"),
            "distance_km": planned.get("distance_km"),
            "target_pace": _fmt_pace(planned.get("target_pace_sec")),
            "details": planned.get("details"),
            "coach_note": planned.get("coach_note"),
        }
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
            max_out=1500,
            temperature=0.4,
        )
        content = resp.choices[0].message.content
        return content.strip() if content and content.strip() else None
    except Exception as exc:
        # Don't let one bad activity break the batch — but DO log so a permanent
        # OpenAI failure (revoked key, removed model, schema mismatch) is visible.
        print(f"[run_analyzer.analyze_activity] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


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
    plan_row = get_active_plan()
    prog: dict[str, Any] = {}
    if plan_row is not None:
        try:
            prog = json.loads(plan_row["progression"]) or {}
        except (TypeError, ValueError):
            prog = {}
    plan_paces = prog.get("paces") or {}

    for r in rows:
        activity = dict(r)
        source_id = activity["source_id"]
        # Belt-and-suspenders: if a parallel sync already analyzed this run, skip.
        if get_activity_analysis(source_id) is not None:
            out["skipped"] += 1
            out["results"].append({"date": activity.get("activity_date"), "status": "already_analyzed"})
            continue
        peers = [x for x in recent if x.get("source_id") != source_id]
        planned = phase = None
        upcoming: list[dict[str, Any]] = []
        activity_date = str(activity.get("activity_date") or "")
        if activity_date:
            planned_row = get_planned_workout(activity_date)
            planned = dict(planned_row) if planned_row else None
            try:
                act_day = date.fromisoformat(activity_date)
            except ValueError:
                act_day = None
            if act_day is not None:
                phase = phase_context(prog, act_day) if prog else None
                upcoming = [
                    dict(u)
                    for u in list_planned_workouts(
                        (act_day + timedelta(days=1)).isoformat(),
                        (act_day + timedelta(days=7)).isoformat(),
                    )
                ]
        summary = analyze_activity(
            activity, goal=goal, recent_runs=peers,
            planned=planned, plan_paces=plan_paces, phase=phase, upcoming=upcoming,
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
