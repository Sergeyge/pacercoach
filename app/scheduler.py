from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .settings import settings
from .sync_service import run_garmin_sync

scheduler = BackgroundScheduler()

# Absolute ceiling on retry attempts. The wall-clock cutoff normally stops the
# chain first; this only exists so a misconfigured interval or cutoff cannot
# produce an endless run of Garmin calls.
_ATTEMPT_CEILING = 48

# app_config key recording when the weekly pace recalibration last ATTEMPTED to
# run. Deliberately the attempt and not the success: `paces_recalibrated_at` in
# the plan only advances on a real reading, so keying off that would retry every
# morning through a Garmin outage and put a warn row in `sync_log` each time.
_PACES_ATTEMPT_KEY = "paces_recalibrate_last_attempt"

# Roughly weekly, measured from the last attempt rather than pinned to a weekday.
# A threshold moves over weeks, so more often just makes the coming week's tempo
# target shift under the athlete; and "7 days since we last tried" means a
# morning that never completed catches up the next day instead of losing the week.
_PACES_EVERY_DAYS = 7

# app_config key recording the date the morning routine last ran to completion.
# Used instead of `garmin_workout_id` as the "already handled today" marker,
# because that id is only set when the PUSH succeeds — so a failed push would look
# identical to a morning that never ran, and the catch-up below would re-send the
# email on every restart.
_MORNING_DONE_KEY = "morning_routine_done_date"


def _log_exc(where: str, exc: BaseException) -> None:
    print(f"[scheduler.{where}] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        h, m = int(h), int(m)
    except Exception as exc:
        _log_exc("parse_hhmm", exc)
        return default
    if not (0 <= h <= 23 and 0 <= m <= 59):
        print(f"[scheduler] ignoring out-of-range time {value!r}; using {default[0]:02d}:{default[1]:02d}", file=sys.stderr, flush=True)
        return default
    return h, m


def _max_morning_attempts() -> int:
    """How many attempts fit in the wait window, so the cap follows
    MORNING_RETRY_UNTIL rather than a fixed number.

    At the shipped defaults (20 min, 06:00 -> 09:00) this gives 11 attempts and the
    cutoff is what stops the chain. Below about a 4-minute interval `_ATTEMPT_CEILING`
    binds first and the chain ends early — deliberate, since the ceiling exists to
    bound Garmin calls.
    """
    start_h, start_m = _morning_hour_minute()
    end_h, end_m = _parse_hhmm(settings.morning_retry_until, (9, 0))
    window = max(0, (end_h * 60 + end_m) - (start_h * 60 + start_m))
    every = max(1, settings.morning_retry_minutes)
    return max(1, min(_ATTEMPT_CEILING, window // every + 2))


def _mark_morning_done(today: date | None = None) -> None:
    try:
        from .db import set_config

        set_config(_MORNING_DONE_KEY, (today or _local_now().date()).isoformat())
    except Exception as exc:
        _log_exc("mark_morning_done", exc)


def _morning_already_done(today: date) -> bool:
    try:
        from .db import get_config

        return get_config(_MORNING_DONE_KEY, "") == today.isoformat()
    except Exception as exc:
        _log_exc("morning_already_done", exc)
        return False


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _before_retry_cutoff(now: datetime | None = None) -> bool:
    """Whether there is still time this morning to wait for recovery data."""
    now = now or _local_now()
    return (now.hour, now.minute) < _parse_hhmm(settings.morning_retry_until, (9, 0))


def _schedule_metrics_retry(attempt: int) -> bool:
    """Re-run the morning update shortly, to pick up recovery data that hasn't
    synced yet. Returns True if a retry was scheduled."""
    if attempt >= _max_morning_attempts() or not _before_retry_cutoff():
        return False
    run_at = _local_now() + timedelta(minutes=max(1, settings.morning_retry_minutes))
    try:
        scheduler.add_job(
            _morning_update,
            "date",
            run_date=run_at,
            args=[attempt + 1],
            id=f"morning_update_retry_{attempt + 1}",
            replace_existing=True,
        )
    except Exception as exc:
        _log_exc("schedule_metrics_retry", exc)
        return False
    print(
        f"[scheduler] recovery metrics not synced yet (attempt {attempt}); "
        f"retrying at {run_at:%H:%M} {settings.timezone}",
        file=sys.stderr,
        flush=True,
    )
    return True


def _recalibrate_paces_weekly(today: date) -> None:
    """Re-derive the plan's training paces from current fitness, about weekly.

    The paces are otherwise fixed at goal creation, so a lactate threshold Garmin
    establishes or revises later never reaches the plan and build-phase tempo
    keeps running at whatever the goal implied months ago.

    Runs before `adapt_today` so today's session already carries any new pace.
    Never raises, and a change is written to `sync_log` — a training pace that
    moved on its own must be something the athlete can find an explanation for.
    """
    try:
        from .db import get_config, set_config

        last = get_config(_PACES_ATTEMPT_KEY, "") or ""
        if last:
            try:
                if (today - date.fromisoformat(last)).days < _PACES_EVERY_DAYS:
                    return
            except ValueError:
                pass  # unreadable marker — treat as never attempted
        set_config(_PACES_ATTEMPT_KEY, today.isoformat())
    except Exception as exc:
        _log_exc("recalibrate_paces_weekly[marker]", exc)
        return

    try:
        from .goal_planner import pace_text, recalibrate_paces

        res = recalibrate_paces(today=today)
    except Exception as exc:
        _log_exc("recalibrate_paces_weekly", exc)
        return

    if res.get("status") != "ok":
        _log_exc("recalibrate_paces_weekly", RuntimeError(str(res.get("error"))))
        return

    changed = res.get("changed") or {}
    if not changed:
        print(
            f"[scheduler] weekly pace check: no change "
            f"(threshold {res.get('threshold_source')})",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        from .db import add_sync_log

        moved = "; ".join(
            f"{k} {pace_text(v.get('from')) or 'unset'} → {pace_text(v.get('to')) or 'unset'}"
            for k, v in changed.items()
        )
        add_sync_log(
            "info",
            f"training paces recalibrated from current fitness ({moved}); "
            f"{res.get('days_refreshed', 0)} planned day(s) updated",
            0,
        )
    except Exception as exc:
        _log_exc("recalibrate_paces_weekly[log]", exc)


def _announce_morning(workout: dict | None, freshness: dict) -> None:
    """Tell the athlete about today — but only when there is something to say and
    this morning's own data backs it. Never raises.

    Two silent cases, both written to `sync_log` so a quiet morning is never
    mistaken for a notify outage:

      * **No verified recovery data.** The 06:00 message reads as "we looked at
        how you slept and this is today's session". Past MORNING_RETRY_UNTIL the
        routine still adapts and pushes from training load alone — the watch must
        not be left empty — but that prescription was never condition-checked, so
        it is not announced. The dashboard still shows it.
      * **Rest day.** Nothing prescribed to announce.
    """
    if not workout:
        return
    from .db import add_sync_log

    if not freshness.get("fresh"):
        reason = freshness.get("reason") or "watch had not synced since waking"
        add_sync_log(
            "info",
            f"no verified recovery data by {settings.morning_retry_until} ({reason}); "
            "today adapted from training load but not announced",
            0,
        )
        return

    from .notify import send_morning_summary

    # `send_morning_summary` owns the rest-day rule, so the manual endpoint can
    # still force one.
    if send_morning_summary(workout).get("status") == "skipped_rest_day":
        add_sync_log("info", "rest day — morning summary not sent", 0)


def _morning_update(attempt: int = 1) -> None:
    """Each morning: refresh Garmin data, adapt today's workout, push it to the watch.

    The adaptation is only committed to the athlete — notified and pushed — once
    this morning's recovery metrics have actually synced. Until then the job
    reschedules itself, so the session that lands on the watch is normally built
    from post-sleep data rather than from whatever happened to be available at
    06:00. Once MORNING_RETRY_UNTIL passes — or the attempt cap is reached — it
    proceeds regardless, saying in the note why it had no recovery data.
    """
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

    if paused:
        # Still sync + record today's snapshot so the progress trend is
        # continuous, then bail out before any adapt / notify / push.
        try:
            run_garmin_sync(days=45, notify_analysis=False)
        except Exception as exc:
            _log_exc("run_garmin_sync[paused]", exc)
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

    # Read this morning's recovery metrics FIRST and decide whether to proceed,
    # because everything below has side effects the athlete can see: adapt_today
    # writes the prescription and, when the session changes, unschedules the
    # workout already on the watch. Gating after that would leave an empty watch
    # calendar all morning on exactly the days the data is late.
    try:
        from .daily_coach import morning_metrics

        recovery = morning_metrics()
    except Exception as exc:
        # `morning_metrics` is written not to raise, but this is the one step whose
        # failure would abort the whole morning before anything else logs, so it
        # gets the same treatment as its siblings: proceed blind rather than die
        # silently, and leave an operator-visible trace.
        _log_exc("morning_metrics", exc)
        try:
            from .db import add_sync_log

            add_sync_log("error", f"morning recovery read failed: {type(exc).__name__}: {exc}", 0)
        except Exception as exc2:
            _log_exc("add_sync_log[morning_metrics]", exc2)
        recovery = ({}, {"fresh": False, "reason": f"{type(exc).__name__}: {exc}"[:200]})

    if not recovery[1].get("fresh") and _schedule_metrics_retry(attempt):
        return  # nothing synced, nothing written, nothing deleted — try again shortly

    # Suppress run-review emails from the morning sync so we don't send two
    # emails seconds apart (the morning summary below is the primary one);
    # any new run still gets analyzed and saved for the dashboard.
    try:
        run_garmin_sync(days=45, notify_analysis=False)
    except Exception as exc:
        _log_exc("run_garmin_sync", exc)

    try:
        from .progress import record_snapshot

        record_snapshot(goal)
    except Exception as exc:
        _log_exc("record_snapshot", exc)

    # Warm the zone and fitness caches while we are already talking to Garmin.
    # Both are read by the coach chat, which is cache-only on purpose: a cold
    # zone read costs a time-in-zone call per recent activity and a cold fitness
    # read six calls, and neither belongs in front of an athlete waiting on a
    # reply. Failures are cached as failures, so this never blocks the morning.
    try:
        from .garmin_metrics import cached_fitness_summary
        from .zones import hr_zones

        hr_zones()
        cached_fitness_summary()
    except Exception as exc:
        _log_exc("warm_zone_fitness_caches", exc)

    # After the zone cache is warm (the threshold is read through it) and before
    # today's session is built, so a new pace reaches today rather than tomorrow.
    _recalibrate_paces_weekly(_local_now().date())

    try:
        from .daily_coach import adapt_today, ensure_horizon

        ensure_horizon()
        workout = adapt_today(use_live_metrics=True, recovery=recovery)
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

    if workout and workout.get("skipped"):
        # Already run (or paused mid-day) — nothing to prescribe or announce.
        _mark_morning_done()
        return

    try:
        _announce_morning(workout, recovery[1])
    except Exception as exc:
        _log_exc("announce_morning", exc)
        # Also surface to /sync/status so a chronic SMTP/notify outage isn't
        # invisible (user otherwise just stops getting emails with no signal).
        try:
            from .db import add_sync_log

            add_sync_log("warn", f"morning notify failed: {type(exc).__name__}: {exc}", 0)
        except Exception as exc2:
            _log_exc("add_sync_log[notify]", exc2)

    # Today has been handled — announced, or deliberately left unannounced. From
    # here on a restart must not repeat the routine even if the push below fails.
    _mark_morning_done()

    if not settings.goal_auto_push or not workout:
        return
    if workout.get("kind") == "rest" or float(workout.get("distance_km") or 0) <= 0:
        return

    # Push only today's adapted workout. Projected future days live in the
    # dashboard only; the user can act on them when each becomes "today".
    try:
        from .db import get_planned_workout, mark_garmin_pushed
        from .workout_publisher import push_workout_to_garmin, structured_workout_for_planned_row

        today = date.today()
        existing = get_planned_workout(today.isoformat())
        if existing and existing["garmin_workout_id"]:
            return  # today already on Garmin and the adapter didn't change it
        structured = structured_workout_for_planned_row(workout, today)
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


def _schedule_catch_up_if_missed() -> None:
    """If the morning routine was due today but never completed, run it shortly
    after startup.

    Retries live only in memory, so a restart inside the retry window would
    otherwise drop the day entirely — the cron trigger won't fire again until
    tomorrow. Bounded by the same cutoff as the retries: past that, a restart
    (an evening deploy, say) must not send a "this morning" email or schedule a
    workout for a day that is effectively over.
    """
    now = _local_now()
    if (now.hour, now.minute) < _morning_hour_minute():
        return  # the cron job hasn't been due yet today
    if not _before_retry_cutoff(now):
        return  # too late in the day to stand in for the morning run
    if _morning_already_done(now.date()):
        return
    try:
        from .db import get_active_goal, get_planned_workout

        goal = get_active_goal()
        if goal is None or goal["paused_at"]:
            return
        row = get_planned_workout(now.date().isoformat())
        # A day already run (or paused) needs no prescription, and rewriting it
        # would reset its status and orphan the recorded actuals.
        if row is not None and row["status"] in ("completed", "paused"):
            return
        run_at = now + timedelta(minutes=1)
        scheduler.add_job(
            _morning_update, "date", run_date=run_at, args=[1], id="morning_update_catch_up", replace_existing=True
        )
        print(f"[scheduler] this morning's routine never completed — catching up at {run_at:%H:%M}", file=sys.stderr, flush=True)
    except Exception as exc:
        _log_exc("schedule_catch_up_if_missed", exc)


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
    _schedule_catch_up_if_missed()
    scheduler.start()


def next_sync_at() -> str | None:
    """ISO timestamp of the next automatic Garmin sync, or None when auto-sync is off."""
    try:
        job = scheduler.get_job("garmin_sync")
    except Exception as exc:
        _log_exc("next_sync_at", exc)
        return None
    run_at = getattr(job, "next_run_time", None) if job else None
    return run_at.isoformat() if run_at else None
