from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .auth import require_api_key
from .daily_coach import adapt_today, ensure_horizon
from .db import (
    add_coach_message,
    clear_coach_messages,
    deactivate_goals,
    get_active_goal,
    get_latest_analysis,
    get_planned_workout,
    init_db,
    link_actuals,
    list_planned_workouts,
    list_recent_coach_messages,
    list_runs,
    list_sync_log,
    mark_garmin_pushed,
    mark_planned_paused_from,
    pause_active_goal,
    set_active_goal,
    set_config,
    trim_coach_messages,
    upsert_activities,
)
from .garmin_client import GarminClientError
from .garmin_extra import fetch_gear, fetch_last_splits
from .garmin_metrics import fetch_fitness, fetch_recovery, fetch_snapshot
from .goal_planner import build_and_store_plan, parse_target_time, pace_text, resume_active_goal_and_shift
from .importers import parse_garmin_csv
from .notify import current_channel, send_morning_summary
from .openai_client import coach_answer, list_models, ping
from .planner import generate_today_plan, generate_weekly_plan
from .progress import _hms, build_report, estimate_eta
from .readiness import calculate_readiness
from .scheduler import start_scheduler
from .settings import settings
from .sync_service import run_garmin_sync

from .workout_publisher import (
    WorkoutPublishError,
    delete_garmin_workout,
    planned_activity_to_structured_workout,
    push_workout_to_garmin,
    save_workout_json,
    to_garmin_workout_payload,
)

app = FastAPI(
    title="Running Personal Assistant",
    version="0.1.0",
    dependencies=[Depends(require_api_key)],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    start_scheduler()


_DASHBOARD = Path(__file__).resolve().parent / "static" / "dashboard.html"


@app.get("/")
@app.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/import/garmin-csv")
async def import_garmin_csv(
    file: UploadFile = File(...),
    distance_unit: str = Query("km", pattern="^(km|miles|mile|mi)$"),
) -> dict:
    content = await file.read()
    try:
        activities = parse_garmin_csv(content, distance_unit=distance_unit)
        changed = upsert_activities(activities)
        return {
            "imported_or_updated": changed,
            "parsed_running_activities": len(activities),
            "filename": file.filename,
        }
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/activities/runs")
def activities(limit: int = 50) -> dict:
    rows = list_runs(limit=limit)
    return {"count": len(rows), "activities": [dict(r) for r in rows]}


@app.get("/readiness")
def readiness() -> dict:
    runs = [dict(r) for r in list_runs(limit=500)]
    return calculate_readiness(runs).__dict__


@app.get("/plan/today")
def today_plan(
    goal: str = "general_fitness",
    today: date | None = None,
) -> dict:
    plan_date = today or date.today()
    runs = [dict(r) for r in list_runs(limit=500)]
    readiness = calculate_readiness(runs, today=plan_date)
    return generate_today_plan(readiness, runs=runs, goal=goal, today=plan_date)


@app.get("/plan/week")
@app.get("/plan/weekly")
def weekly_plan(
    target_distance_km: float | None = Query(None, gt=5, le=120),
    goal: str = "general_fitness",
    start_date: date | None = None,
) -> dict:
    plan_start = start_date or date.today()
    runs = [dict(r) for r in list_runs(limit=500)]
    readiness = calculate_readiness(runs, today=plan_start)
    return generate_weekly_plan(
        readiness,
        runs=runs,
        target_distance_km=target_distance_km,
        goal=goal,
        start_date=plan_start,
    )


@app.post("/sync/garmin")
def sync_garmin(days: int = Query(30, ge=1, le=365)) -> dict:
    return run_garmin_sync(days=days)


@app.get("/sync/status")
def sync_status() -> dict:
    return {
        "auto_sync_enabled": settings.auto_sync_enabled,
        "sync_interval_minutes": settings.sync_interval_minutes,
        "recent_syncs": [dict(r) for r in list_sync_log(limit=20)],
    }


@app.get("/garmin/recovery")
def garmin_recovery(target_date: date | None = Query(None, alias="date")) -> dict:
    """Live Garmin recovery metrics: readiness, HRV, sleep, stress, body battery, RHR, respiration, SpO2."""
    try:
        return fetch_recovery(target_date)
    except GarminClientError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})


@app.get("/garmin/fitness")
def garmin_fitness(target_date: date | None = Query(None, alias="date")) -> dict:
    """Live Garmin performance metrics: training status, race predictions, VO2max, endurance/hill score, fitness age."""
    try:
        return fetch_fitness(target_date)
    except GarminClientError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})


class GoalIn(BaseModel):
    distance_km: float
    target_time: str  # "H:MM:SS", "MM:SS", or seconds


@app.post("/goal")
def set_goal(body: GoalIn) -> dict:
    """Define the running goal and build + store a training plan from current fitness."""
    try:
        target_seconds = parse_target_time(body.target_time)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if body.distance_km <= 0 or target_seconds <= 0:
        return JSONResponse(status_code=400, content={"error": "distance_km and target_time must be positive"})
    goal_id = set_active_goal(body.distance_km, target_seconds)
    runs = [dict(r) for r in list_runs(limit=500)]
    summary = build_and_store_plan(goal_id, body.distance_km, target_seconds, runs)
    return {
        "status": "ok",
        "goal": {"distance_km": body.distance_km, "target_seconds": target_seconds, "target_pace": summary["goal_pace"]},
        "plan": summary,
        "eta": estimate_eta({"distance_km": body.distance_km, "target_seconds": target_seconds}, fresh=True),
    }


@app.get("/goal/eta")
def goal_eta(fresh: bool = False) -> dict:
    """Estimated completion date for the active goal + a short explanation.
    `fresh=true` bypasses the 6h cache and forces an LLM recompute."""
    g = get_active_goal()
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    return estimate_eta(g, fresh=fresh)


@app.get("/goal")
def get_goal(progress: bool = False) -> dict:
    g = get_active_goal()
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    out = {
        "distance_km": g["distance_km"],
        "target_seconds": g["target_seconds"],
        "target": _hms(g["target_seconds"]),
        "target_pace": pace_text(round(g["target_seconds"] / g["distance_km"])),
        "created_at": g["created_at"],
        "paused": bool(g["paused_at"]),
        "paused_at": g["paused_at"],
        "pause_reason": g["pause_reason"],
        "pause_until": g["pause_until"],
    }
    if progress:  # live Garmin call (slower) only when explicitly requested
        try:
            out["garmin_race_predictions"] = (fetch_fitness().get("metrics") or {}).get("race_predictions")
        except Exception:
            out["garmin_race_predictions"] = None
    return out


@app.delete("/goal")
def delete_goal() -> dict:
    deactivate_goals()
    return {"status": "ok", "message": "active goal deactivated"}


@app.get("/goal/plan")
def goal_plan(days: int = Query(21, ge=1, le=120)) -> dict:
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    ensure_horizon()
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    rows = list_planned_workouts(start, end)
    return {"count": len(rows), "workouts": [dict(r) for r in rows]}


@app.get("/goal/week")
def goal_week() -> dict:
    """7-day picture. Today is firm (re-adapted each morning); days 2-7 are
    projections that each get finalized on their own morning."""
    g = get_active_goal()
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    # When paused we do NOT call ensure_horizon — we want the dashboard to
    # show the paused state, not freshly-materialized rule days.
    if g["paused_at"] is None:
        ensure_horizon()
    today = date.today()
    rows = list_planned_workouts(today.isoformat(), (today + timedelta(days=6)).isoformat())
    workouts = []
    for r in rows:
        if r["status"] == "paused":
            firmness = "paused"
        elif r["status"] == "completed":
            firmness = "done"
        elif r["plan_date"] == today.isoformat():
            firmness = "today (adapts each morning)"
        elif r["source"] == "adapted":
            firmness = "adjusted"
        else:
            firmness = "projected"
        workouts.append(
            {
                "date": r["plan_date"],
                "kind": r["kind"],
                "distance_km": r["distance_km"],
                "target_pace_sec": r["target_pace_sec"],
                "firmness": firmness,
                "status": r["status"],
                "coach_note": r["coach_note"],
                "details": r["details"],
            }
        )
    return {
        "start": today.isoformat(),
        "days": 7,
        "note": "7-day projection. Each day's workout is re-adapted and auto-pushed on its own morning; today's adaptation can re-balance the rest of this week.",
        "workouts": workouts,
    }


@app.get("/goal/progress")
def goal_progress(weeks: int = Query(12, ge=2, le=52)) -> dict:
    """Weekly progress: Garmin race-prediction trend for the goal distance vs the target."""
    g = get_active_goal()
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    return build_report(g, weeks=weeks)


@app.get("/goal/today")
def goal_today() -> dict:
    """Today's workout. Returns the morning-adapted session if available, else the
    rule-based plan for today (cheap read — no OpenAI/Garmin call)."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    ds = date.today().isoformat()
    row = get_planned_workout(ds)
    if row is None:
        ensure_horizon()
        row = get_planned_workout(ds)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "no plan for today"})
    return dict(row)


@app.post("/goal/today/refresh")
def goal_today_refresh(live: bool = True) -> dict:
    """Force the adaptive recompute now (rules + OpenAI + this-morning metrics)."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    workout = adapt_today(use_live_metrics=live)
    if workout is None:
        return JSONResponse(status_code=404, content={"error": "no plan"})
    return workout


@app.post("/goal/today/push")
def goal_today_push() -> dict:
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    ds = date.today().isoformat()
    row = get_planned_workout(ds)
    if row is None:
        ensure_horizon()
        row = get_planned_workout(ds)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "no plan for today"})
    if row["kind"] == "rest" or float(row["distance_km"] or 0) <= 0:
        return {"status": "skipped_rest_day", "date": ds}
    titles = {"easy": "Easy run", "long": "Long run", "recovery": "Recovery run", "quality": "Quality run"}
    workout = planned_activity_to_structured_workout(
        {"title": titles.get(row["kind"], "Run"), "distance_km": row["distance_km"], "target_pace_sec": row["target_pace_sec"], "details": row["details"] or "", "date": ds},
        workout_date=date.today(),
    )
    save_workout_json(workout)
    try:
        result = push_workout_to_garmin(workout, schedule_date=date.today())
    except WorkoutPublishError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})
    # Record the Garmin workout id so the morning job's duplicate-push guard
    # sees today as already pushed (without this, the user manually pushing
    # then the morning job firing produces two calendar entries on the watch).
    wid = result.get("workout_id") if isinstance(result, dict) else None
    if wid:
        mark_garmin_pushed(ds, str(wid))
    return result


@app.post("/goal/today/notify")
def goal_today_notify() -> dict:
    """Send today's workout + coaching note as a morning summary (per NOTIFY_CHANNEL)."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    ds = date.today().isoformat()
    row = get_planned_workout(ds)
    if row is None:
        ensure_horizon()
        row = get_planned_workout(ds)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "no plan for today"})
    return send_morning_summary(dict(row))


@app.get("/garmin/calendar/month")
def garmin_calendar_month(year: int = Query(...), month0: int = Query(..., ge=0, le=11)) -> dict:
    """Garmin Connect calendar for a given month. `month0` is 0-indexed to match
    JS `Date.getMonth()` so the dashboard can pass it through directly."""
    try:
        from .garmin_client import get_garmin_client

        client = get_garmin_client()
        return client.garth.connectapi(f"/calendar-service/year/{year}/month/{month0}") or {}
    except GarminClientError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:200]})


@app.post("/garmin/workout/{workout_id}/schedule")
def schedule_existing_workout(workout_id: str, date_iso: str = Query(..., alias="date")) -> dict:
    """Re-schedule an EXISTING Garmin workout to a date (no new workout created)."""
    try:
        from .garmin_client import get_garmin_client

        client = get_garmin_client()
        resp = client.garth.connectapi(
            f"/workout-service/schedule/{workout_id}",
            method="POST",
            json={"date": date_iso},
        )
        return {"ok": True, "workout_id": workout_id, "date": date_iso, "response": resp}
    except GarminClientError as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "workout_id": workout_id, "date": date_iso, "error": str(exc)[:300]},
        )


@app.get("/garmin/snapshot")
def garmin_snapshot(target_date: date | None = Query(None, alias="date")) -> dict:
    """One Garmin login → compact recovery + fitness summary for the dashboard."""
    try:
        return fetch_snapshot(target_date)
    except GarminClientError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})


@app.get("/goal/stats")
def goal_stats(days: int = Query(30, ge=7, le=120)) -> dict:
    """Consistency: % of planned run days completed + current streak."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    link_actuals()
    today = date.today()
    rows = list_planned_workouts((today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat())
    run_days = [r for r in rows if r["kind"] != "rest"]
    completed = [r for r in run_days if r["status"] == "completed"]
    streak = 0
    for r in sorted(run_days, key=lambda x: x["plan_date"], reverse=True):
        if r["status"] == "completed":
            streak += 1
        else:
            break
    return {
        "window_days": days,
        "planned_run_days": len(run_days),
        "completed": len(completed),
        "completion_pct": round(100 * len(completed) / len(run_days)) if run_days else 0,
        "current_streak": streak,
    }


@app.get("/activities/last/analysis")
def last_run_analysis() -> dict:
    """The most recent AI coach review of a completed run."""
    row = get_latest_analysis()
    if row is None:
        return {"summary": None, "message": "No run analyses yet — sync after your next run."}
    pace = row["avg_pace_sec_per_km"]
    pace_str = f"{int(pace) // 60}:{int(pace) % 60:02d}/km" if pace else None
    return {
        "date": row["activity_date"],
        "distance_km": row["distance_km"],
        "pace": pace_str,
        "avg_hr": row["avg_hr"],
        "summary": row["summary"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
    }


@app.post("/activities/analyze-new")
def analyze_new_now() -> dict:
    """Manually trigger the AI coach to review any synced runs that haven't been analyzed yet."""
    from .run_analyzer import analyze_new_runs_and_notify

    return analyze_new_runs_and_notify()


@app.get("/activities/last/splits")
def last_splits() -> dict:
    try:
        return fetch_last_splits()
    except GarminClientError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})


@app.get("/gear")
def gear() -> dict:
    try:
        return fetch_gear()
    except GarminClientError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})


class AskIn(BaseModel):
    question: str


@app.post("/goal/coach/ask")
def coach_ask(body: AskIn) -> dict:
    """Ask the OpenAI coach a free-form question, grounded in your goal/plan/
    readiness AND the most recent 49 prior chat turns (so the coach can follow
    up across days). The chat history is persisted in `coach_message` and
    trimmed to the last 50 entries (~25 exchanges)."""
    g = get_active_goal()
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    today = date.today()
    tw = get_planned_workout(today.isoformat())
    runs = [dict(r) for r in list_runs(limit=500)]
    readiness = calculate_readiness(runs, today=today)
    recent = list_planned_workouts((today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat())
    # Upcoming 14 days so the coach can reference real dates when proposing
    # a plan change (move/shorten/rest a specific day).
    upcoming = list_planned_workouts(today.isoformat(), (today + timedelta(days=13)).isoformat())
    context = {
        "today_date": today.isoformat(),
        "goal": {"distance_km": g["distance_km"], "target_seconds": g["target_seconds"]},
        "paused": bool(g["paused_at"]),
        "pause_reason": g["pause_reason"],
        "pause_until": g["pause_until"],
        "today": dict(tw) if tw else None,
        "readiness": {"score": readiness.score, "status": readiness.status, "reasons": readiness.reasons},
        "recent_results": [
            {"date": r["plan_date"], "kind": r["kind"], "planned_km": r["distance_km"], "status": r["status"], "actual_km": r["actual_distance_km"]}
            for r in recent
        ],
        "upcoming_plan": [
            {"date": r["plan_date"], "kind": r["kind"], "distance_km": r["distance_km"], "status": r["status"]}
            for r in upcoming
        ],
    }
    question = (body.question or "")[:500]
    # Pull the last 49 turns (leaving room for the new user message) so the
    # coach can refer back to "what we discussed yesterday".
    history = [
        {"role": r["role"], "content": r["content"]}
        for r in list_recent_coach_messages(limit=49)
    ]
    # Persist the user message FIRST so the conversation never desyncs even if
    # OpenAI then fails (next ask will still see this turn).
    add_coach_message("user", question)
    result = coach_answer(context, question, history=history)
    if result is None:
        return JSONResponse(status_code=503, content={"error": "coach unavailable — set OPENAI_API_KEY and ensure the account has credits"})
    answer = result.get("answer", "")
    proposed = result.get("proposed_change")
    add_coach_message("assistant", answer)
    trim_coach_messages(keep=50)
    return {"question": question, "answer": answer, "proposed_change": proposed}


class ApplyChangeIn(BaseModel):
    change: dict


@app.post("/goal/coach/apply")
def coach_apply(body: ApplyChangeIn) -> dict:
    """Apply a coach-proposed plan change (after the athlete confirms it in the
    UI). Bounded: future dates only, kinds restricted, distance clamped to the
    plan cap. Re-pushes to Garmin for any affected day already on the watch."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    from .daily_coach import apply_plan_change

    res = apply_plan_change(body.change or {})
    if res.get("status") != "applied":
        return JSONResponse(status_code=400, content=res)
    return res


@app.get("/goal/coach/history")
def coach_history(limit: int = 50) -> dict:
    """Return the persisted coach chat in chronological order (oldest first)."""
    rows = list_recent_coach_messages(limit=max(1, min(limit, 200)))
    return {
        "messages": [
            {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
            for r in rows
        ]
    }


@app.delete("/goal/coach/history")
def coach_history_clear() -> dict:
    """Clear the persisted coach chat."""
    clear_coach_messages()
    return {"status": "cleared"}


class PauseIn(BaseModel):
    reason: str | None = None
    until: str | None = None  # ISO date YYYY-MM-DD; null = indefinite


@app.post("/goal/pause")
def goal_pause(body: PauseIn) -> dict:
    """Pause the active goal — morning auto-adapt + auto-push to Garmin are
    skipped while paused (a snapshot is still recorded so the trend keeps a
    continuous baseline). Future planned workouts are flagged status='paused'
    so the dashboard renders them as paused instead of stale prescriptions.
    Optional `until` ISO date auto-resumes on that day."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    reason = (body.reason or "").strip()[:200] or None
    until = (body.until or "").strip() or None
    if until:
        try:
            date.fromisoformat(until)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": f"invalid until date: {until!r} (use YYYY-MM-DD)"})
    pause_active_goal(reason, until)
    marked = mark_planned_paused_from(date.today().isoformat())
    return {"status": "paused", "reason": reason, "until": until, "days_marked_paused": marked}


@app.post("/goal/resume")
def goal_resume() -> dict:
    """Resume the active goal: clear pause state, shift the plan's start_date
    forward by the days paused (so progression picks up where it left off),
    drop paused rows, and re-materialize from today onward."""
    g = get_active_goal()
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    if g["paused_at"] is None:
        return {"status": "not_paused"}
    return resume_active_goal_and_shift()


@app.get("/openai/models")
def openai_models() -> dict:
    """Chat-capable models on the account + the currently selected one."""
    return list_models()


_NOTIFY_OPTIONS = ["none", "email", "callmebot"]


@app.get("/config/notify")
def get_notify_channel() -> dict:
    return {"channel": current_channel(), "default": settings.notify_channel, "options": _NOTIFY_OPTIONS}


class ChannelIn(BaseModel):
    channel: str


@app.post("/config/notify")
def set_notify_channel(body: ChannelIn) -> dict:
    c = (body.channel or "").strip().lower()
    if c not in _NOTIFY_OPTIONS:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"channel must be one of {_NOTIFY_OPTIONS}"})
    set_config("notify_channel", c)
    return {"ok": True, "channel": c}


@app.post("/goal/week/push")
def push_goal_week(days: int = Query(7, ge=1, le=14), schedule: bool = True, force: bool = False) -> dict:
    """Push the next N days of the goal plan to Garmin (scheduled on each day).

    By default skips days already on Garmin (tracked via `garmin_workout_id`) to
    avoid duplicates. Pass `force=true` to re-push everything."""
    if get_active_goal() is None:
        return JSONResponse(status_code=404, content={"error": "no active goal"})
    ensure_horizon()
    today = date.today()
    rows = list_planned_workouts(today.isoformat(), (today + timedelta(days=days - 1)).isoformat())
    titles = {"easy": "Easy run", "long": "Long run", "recovery": "Recovery run", "quality": "Quality run"}
    results = []
    pushed = 0
    for r in rows:
        if r["kind"] == "rest" or float(r["distance_km"] or 0) <= 0:
            results.append({"date": r["plan_date"], "status": "skipped_rest_day"})
            continue
        if not force and r["garmin_workout_id"]:
            results.append({"date": r["plan_date"], "status": "already_pushed", "workout_id": r["garmin_workout_id"]})
            continue
        # Force re-push: best-effort delete the previously pushed workout from
        # Garmin first (ignoring 404s for ones already deleted out-of-band).
        # A failed delete leaves a dangling Garmin workout — surface it on the
        # per-day result so the caller can react.
        delete_status: dict | None = None
        if force and r["garmin_workout_id"]:
            try:
                delete_status = delete_garmin_workout(str(r["garmin_workout_id"]))
            except Exception as exc:
                import sys as _sys

                print(
                    f"[main.push_goal_week] delete failed: {type(exc).__name__}: {exc}",
                    file=_sys.stderr,
                    flush=True,
                )
                delete_status = {"status": "error", "error": str(exc)[:200]}
        d = date.fromisoformat(r["plan_date"])
        workout = planned_activity_to_structured_workout(
            {"title": titles.get(r["kind"], "Run"), "distance_km": r["distance_km"], "target_pace_sec": r["target_pace_sec"], "details": r["details"] or "", "date": r["plan_date"]},
            workout_date=d,
        )
        save_workout_json(workout)
        try:
            result = push_workout_to_garmin(workout, schedule_date=d if schedule else None)
            result["date"] = r["plan_date"]
            if delete_status is not None and delete_status.get("status") != "ok":
                result["prior_delete"] = delete_status
            results.append(result)
            wid = result.get("workout_id")
            if result.get("status") == "ok" and wid:
                mark_garmin_pushed(r["plan_date"], str(wid))
                pushed += 1
        except WorkoutPublishError as exc:
            results.append({"date": r["plan_date"], "status": "error", "error": str(exc)})
    return {"days": days, "pushed": pushed, "results": results}


class ModelIn(BaseModel):
    model: str


@app.post("/config/model")
def set_model(body: ModelIn) -> dict:
    """Switch the coach model at runtime (tested before it's committed)."""
    m = (body.model or "").strip()
    if not m:
        return JSONResponse(status_code=400, content={"ok": False, "error": "model required"})
    ok, err = ping(m)
    if not ok:
        return JSONResponse(status_code=400, content={"ok": False, "model": m, "error": err or "model test failed"})
    set_config("openai_model", m)
    return {"ok": True, "model": m}


@app.get("/assistant/context")
def assistant_context() -> dict:
    # Call route functions with real values, not FastAPI Query defaults.
    return {
        "readiness": readiness(),
        "recent_runs": activities(limit=20),
        "today_plan": today_plan(goal=settings.training_goal),
        "week_plan": weekly_plan(target_distance_km=settings.target_distance_km, goal=settings.training_goal),
    }


def _first_runnable_activity(plan: dict) -> dict | None:
    for activity in plan.get("activities", []):
        if float(activity.get("distance_km") or 0) > 0:
            return activity
    return None


@app.get("/workouts/today/json")
def today_workout_json(goal: str = settings.training_goal, today: date | None = None) -> dict:
    plan = today_plan(goal=goal, today=today)
    workout = planned_activity_to_structured_workout(plan["recommendation"], workout_date=date.fromisoformat(plan["date"]))
    path = save_workout_json(workout)
    return {"workout": workout, "saved_to": str(path)}


@app.get("/workouts/today/garmin-payload")
def today_workout_garmin_payload(goal: str = settings.training_goal, today: date | None = None) -> dict:
    plan = today_plan(goal=goal, today=today)
    workout = planned_activity_to_structured_workout(plan["recommendation"], workout_date=date.fromisoformat(plan["date"]))
    if not workout.get("steps"):
        return {"status": "rest_day", "workout": workout, "garmin_payload": None}
    return {"workout": workout, "garmin_payload": to_garmin_workout_payload(workout)}


@app.get("/workouts/today/download")
def download_today_workout(goal: str = settings.training_goal, today: date | None = None):
    plan = today_plan(goal=goal, today=today)
    workout = planned_activity_to_structured_workout(plan["recommendation"], workout_date=date.fromisoformat(plan["date"]))
    path = save_workout_json(workout)
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.post("/workouts/today/push-to-garmin")
def push_today_workout_to_garmin(goal: str = settings.training_goal, today: date | None = None, schedule: bool = True) -> dict:
    plan_date = today or date.today()
    plan = today_plan(goal=goal, today=plan_date)
    workout = planned_activity_to_structured_workout(plan["recommendation"], workout_date=plan_date)
    save_workout_json(workout)
    try:
        result = push_workout_to_garmin(workout, schedule_date=plan_date if schedule else None)
    except WorkoutPublishError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc), "workout": workout})
    # Mark the planned_workout row (if one exists for this date in the goal
    # flow) so the morning job's duplicate-push guard doesn't re-push.
    wid = result.get("workout_id") if isinstance(result, dict) else None
    if wid:
        mark_garmin_pushed(plan_date.isoformat(), str(wid))
    return result


@app.post("/workouts/week/export-json")
def export_week_workouts(goal: str = settings.training_goal, start_date: date | None = None) -> dict:
    week = weekly_plan(target_distance_km=settings.target_distance_km, goal=goal, start_date=start_date)
    exported = []
    for activity in week.get("activities", []):
        workout = planned_activity_to_structured_workout(activity, workout_date=date.fromisoformat(activity["date"]))
        path = save_workout_json(workout)
        exported.append({"date": workout["date"], "name": workout["name"], "type": workout["type"], "path": str(path)})
    return {"count": len(exported), "exported": exported}


@app.post("/workouts/week/push-to-garmin")
def push_week_workouts_to_garmin(goal: str = settings.training_goal, start_date: date | None = None, schedule: bool = True) -> dict:
    week = weekly_plan(target_distance_km=settings.target_distance_km, goal=goal, start_date=start_date)
    results = []
    for activity in week.get("activities", []):
        workout_date = date.fromisoformat(activity["date"])
        workout = planned_activity_to_structured_workout(activity, workout_date=workout_date)
        save_workout_json(workout)
        if not workout.get("steps"):
            results.append({"date": workout["date"], "name": workout["name"], "status": "skipped_rest_day"})
            continue
        try:
            result = push_workout_to_garmin(workout, schedule_date=workout_date if schedule else None)
            result["date"] = workout["date"]
            results.append(result)
        except WorkoutPublishError as exc:
            results.append({"date": workout["date"], "name": workout["name"], "status": "error", "error": str(exc)})
    return {"results": results}
