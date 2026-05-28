from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from .db import (
    get_active_goal,
    get_activity_analysis,
    get_conn,
    list_runs,
    list_unanalyzed_running_activities,
    save_activity_analysis,
    update_activity_sent_at,
)
from .notify import analysis_email_html, send_message
from .openai_client import _client, _create, current_model

_ANALYSIS_SYSTEM = (
    "You are an expert running coach reviewing the athlete's just-completed run. "
    "Write a concise, professional analysis (3-6 sentences) covering: pace and effort "
    "vs the athlete's goal/target paces, what went well, any concerns (e.g. easy days "
    "run too hard, HR drift), and one specific actionable takeaway for the next session. "
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


def analyze_activity(activity: dict[str, Any], goal: Any | None = None, recent_runs: list | None = None) -> str | None:
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

    for r in rows:
        activity = dict(r)
        source_id = activity["source_id"]
        # Belt-and-suspenders: if a parallel sync already analyzed this run, skip.
        if get_activity_analysis(source_id) is not None:
            out["skipped"] += 1
            out["results"].append({"date": activity.get("activity_date"), "status": "already_analyzed"})
            continue
        peers = [x for x in recent if x.get("source_id") != source_id]
        summary = analyze_activity(activity, goal=goal, recent_runs=peers)
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
