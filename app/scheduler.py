from __future__ import annotations

import sys
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .settings import settings
from .sync_service import run_garmin_sync

scheduler = BackgroundScheduler()


def _log_exc(where: str, exc: BaseException) -> None:
    print(f"[scheduler.{where}] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

_TITLES = {"easy": "Easy run", "long": "Long run", "recovery": "Recovery run", "quality": "Quality run"}


def _morning_update() -> None:
    """Each morning: refresh Garmin data, adapt today's workout, push it to the watch."""
    from .db import get_active_goal

    goal = get_active_goal()
    if goal is None:
        return

    # If the plan is paused (injury / travel / illness), skip the adapt + push
    # parts of the morning routine. Auto-resume if the user-set `pause_until`
    # date has passed; otherwise log a sync_log row for visibility and exit
    # early. The Garmin sync + snapshot still run so the trend stays current.
    paused = bool(goal["paused_at"])
    if paused and goal["pause_until"] and goal["pause_until"] < date.today().isoformat():
        # Auto-resume: clear pause AND shift the plan's start_date forward by
        # the days paused (same logic as the manual /goal/resume endpoint) so
        # the progression continues from where it was, not from where the
        # calendar happens to be on this morning.
        try:
            from .goal_planner import resume_active_goal_and_shift

            resume_active_goal_and_shift()
        except Exception as exc:
            _log_exc("auto_resume_with_shift", exc)
        paused = False
        goal = get_active_goal()  # refresh so downstream sees paused_at=None

    # Suppress run-review emails from the morning sync so we don't send two
    # emails seconds apart (the morning summary below is the primary one);
    # any new run still gets analyzed and saved for the dashboard.
    try:
        run_garmin_sync(days=45, notify_analysis=False)
    except Exception as exc:
        _log_exc("run_garmin_sync", exc)

    if paused:
        # Still record today's snapshot so the progress trend is continuous,
        # then bail out before any adapt / notify / push.
        try:
            from .db import add_sync_log

            reason = goal["pause_reason"] or "no reason given"
            until = goal["pause_until"] or "indefinite"
            add_sync_log("info", f"plan paused (reason: {reason}; until: {until}); morning adapt+push skipped", 0)
        except Exception as exc:
            _log_exc("add_sync_log[paused]", exc)
        try:
            from .progress import record_snapshot

            record_snapshot(goal)
        except Exception as exc:
            _log_exc("record_snapshot[paused]", exc)
        return

    try:
        from .progress import record_snapshot

        record_snapshot(goal)
    except Exception as exc:
        _log_exc("record_snapshot", exc)

    try:
        from .daily_coach import adapt_today, ensure_horizon

        ensure_horizon()
        workout = adapt_today(use_live_metrics=True)
    except Exception as exc:
        _log_exc("adapt_today", exc)
        # Record the failure so it shows up in /sync/status — otherwise the
        # user sees no workout, no email, and no operator-visible signal.
        try:
            from .db import add_sync_log

            add_sync_log("error", f"morning adapt failed: {type(exc).__name__}: {exc}", 0)
        except Exception as exc2:
            _log_exc("add_sync_log", exc2)
        return

    try:
        from .notify import send_morning_summary

        if workout:
            send_morning_summary(workout)
    except Exception as exc:
        _log_exc("send_morning_summary", exc)
        # Also surface to /sync/status so a chronic SMTP/notify outage isn't
        # invisible (user otherwise just stops getting emails with no signal).
        try:
            from .db import add_sync_log

            add_sync_log("warn", f"morning notify failed: {type(exc).__name__}: {exc}", 0)
        except Exception as exc2:
            _log_exc("add_sync_log[notify]", exc2)

    if not settings.goal_auto_push or not workout:
        return
    if workout.get("kind") == "rest" or float(workout.get("distance_km") or 0) <= 0:
        return

    # Push only today's adapted workout. Projected future days live in the
    # dashboard only; the user can act on them when each becomes "today".
    try:
        from .db import get_planned_workout, mark_garmin_pushed
        from .workout_publisher import planned_activity_to_structured_workout, push_workout_to_garmin

        today = date.today()
        existing = get_planned_workout(today.isoformat())
        if existing and existing["garmin_workout_id"]:
            return  # today already on Garmin and the adapter didn't change it
        structured = planned_activity_to_structured_workout(
            {
                "title": _TITLES.get(workout["kind"], "Run"),
                "distance_km": workout["distance_km"],
                "details": workout.get("details", ""),
                "date": today.isoformat(),
            },
            workout_date=today,
        )
        result = push_workout_to_garmin(structured, schedule_date=today)
        wid = result.get("workout_id") if isinstance(result, dict) else None
        scheduled = bool(result.get("scheduled")) if isinstance(result, dict) else False
        if wid and scheduled:
            mark_garmin_pushed(today.isoformat(), str(wid))
        elif wid and not scheduled:
            # Workout was created but the schedule POST failed — don't mark as
            # pushed (next run will retry), and write a sync_log row so /sync
            # /status shows the partial state instead of looking healthy.
            from .db import add_sync_log

            schedule_error = result.get("schedule_error") if isinstance(result, dict) else None
            add_sync_log(
                "warn",
                f"Garmin workout {wid} created but schedule failed: {schedule_error}",
                0,
            )
    except Exception as exc:
        _log_exc("auto_push_today", exc)
        try:
            from .db import add_sync_log

            add_sync_log("warn", f"auto-push failed: {type(exc).__name__}: {exc}", 0)
        except Exception as exc2:
            _log_exc("add_sync_log[auto_push]", exc2)


def _morning_hour_minute() -> tuple[int, int]:
    try:
        h, m = settings.morning_update_time.split(":")
        return int(h), int(m)
    except Exception as exc:
        _log_exc("morning_update_time_parse", exc)
        return 6, 0


def start_scheduler() -> None:
    if scheduler.running:
        return
    if settings.auto_sync_enabled:
        scheduler.add_job(
            run_garmin_sync,
            "interval",
            minutes=settings.sync_interval_minutes,
            id="garmin_sync",
            replace_existing=True,
        )
    hour, minute = _morning_hour_minute()
    scheduler.add_job(
        _morning_update,
        "cron",
        hour=hour,
        minute=minute,
        timezone=settings.timezone,
        id="morning_update",
        replace_existing=True,
    )
    scheduler.start()
