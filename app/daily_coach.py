from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import Any

from .db import (
    clear_garmin_pushed,
    get_active_goal,
    get_active_plan,
    get_planned_workout,
    link_actuals,
    list_planned_workouts,
    list_runs,
    upsert_planned_workout,
)
from .garmin_metrics import (
    POST_SLEEP_SUMMARY_KEYS,
    _summarize_recovery,
    fetch_recovery,
    has_verified_signal,
    no_freshness,
    recovery_freshness,
)
from .goal_planner import (
    details_for,
    is_strides_session,
    materialize,
    pace_for,
    phase_context,
    phase_name_for,
    row_structure,
    structure_for,
    title_for,
)
from .openai_client import coach_adjust
from .readiness import calculate_readiness

# Session kinds in ascending order of training stress. THE ORDER IS LOAD-BEARING:
# when readiness eases a day, the coach may move further down this list but never
# back up, so it cannot restore a session readiness has just removed. Without that
# the guardrail was one-directional — it stopped the coach softening a good day
# while leaving it free to put intervals back on a red one.
ALLOWED_KINDS = ["rest", "recovery", "easy", "long", "quality"]
_HARDNESS = {kind: rank for rank, kind in enumerate(ALLOWED_KINDS)}


def ensure_horizon(today: date | None = None, days: int = 14) -> None:
    """Make sure the next `days` of rule-based workouts exist."""
    today = today or date.today()
    plan = get_active_plan()
    if plan is not None:
        materialize(plan, today, days=days)


def morning_metrics(today: date | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Best-effort live Garmin recovery snapshot, plus a freshness report.

    Returns `(summary, freshness)`. Only metrics that `recovery_freshness`
    confirmed carry today's date survive into the summary — stale, undatable and
    missing ones are stripped. That is what makes the summary safe to score:
    `calculate_readiness` cannot tell yesterday's sleep score from today's, so
    without this filter a two-day-old bad night could cancel today's session.

    When nothing is confirmed fresh — including when a date-verified payload turns
    out to carry no scoreable value — the summary is empty and `fresh` is False,
    because a watch that hasn't synced since waking also means the all-day metrics
    (body battery, stress, resting HR) are carried over from before bed.

    Never raises: the returned freshness report carries the cause instead, so the
    caller can distinguish a slow sync from a broken Garmin connection. This
    module imports `garmin_metrics` at module scope on purpose — building the
    fallback report must not depend on an import that could be the thing failing.
    """
    try:
        raw = fetch_recovery(today) or {}
        m = raw.get("metrics", {}) or {}
        freshness = recovery_freshness(
            raw.get("date") or (today or date.today()).isoformat(),
            tr=m.get("training_readiness"),
            hrv=m.get("hrv"),
            sleep=m.get("sleep"),
            errors=raw.get("errors") or None,
        )
        if freshness["errors"]:
            print(
                f"[daily_coach.morning_metrics] Garmin refused recovery calls: {freshness['errors']}",
                file=sys.stderr,
                flush=True,
            )
        if not freshness["fresh"]:
            return {}, freshness

        # Full compact summary (readiness score+level, HRV status+ms, sleep
        # score+hours, stress, body battery, resting HR) — the same shape the
        # dashboard snapshot uses. Drop nulls to keep the LLM context tight.
        summary = _summarize_recovery(
            m.get("training_readiness"),
            m.get("hrv"),
            m.get("sleep"),
            m.get("stress"),
            m.get("body_battery"),
            m.get("resting_heart_rate"),
        )
        for metric, keys in POST_SLEEP_SUMMARY_KEYS.items():
            if metric not in freshness["have"]:
                for key in keys:
                    summary.pop(key, None)
        summary = {k: v for k, v in summary.items() if v is not None}

        # A payload can be stamped with today's date and still carry no usable
        # number — an untracked night, or an HRV status Garmin hasn't established.
        # Treat that as not fresh: otherwise the morning job stops waiting on data
        # that scores nothing, and then tells the athlete their watch hasn't
        # synced. This is what keeps `fresh` and `Readiness.physiological` from
        # meaning two different things.
        if not has_verified_signal(summary):
            print(
                f"[daily_coach.morning_metrics] {freshness['have']} carried no scoreable value; "
                "treating this morning as not yet synced",
                file=sys.stderr,
                flush=True,
            )
            return {}, {
                **freshness,
                "fresh": False,
                "reason": f"payload for {', '.join(freshness['have'])} carried no scoreable value",
            }
        # The all-day metrics (stress, body battery, resting HR) can't be
        # date-checked, but reaching here means the watch has synced since waking,
        # so they are current too. `readiness` never lets them assert freshness
        # on their own.
        return summary, freshness
    except Exception as exc:
        print(f"[daily_coach.morning_metrics] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        reason = f"{type(exc).__name__}: {exc}"[:200]
        return {}, no_freshness(reason, errors={"garmin": reason})


def _blind_disclosure(freshness: dict[str, Any] | None) -> str:
    """The sentence appended to a coach note when no recovery data fed the
    decision. Names the actual cause so the athlete fixes the right thing."""
    f = freshness or {}
    if f.get("errors"):
        return (
            "(Garmin refused this morning's recovery requests, so this reflects training load only "
            "— the connection may need re-authorising rather than your watch re-syncing.)"
        )
    if f.get("reason"):
        # A whole-call failure, or a payload that arrived but scored nothing.
        # Either way it is not "your watch hasn't synced yet".
        return (
            f"(This morning's recovery data could not be used ({f['reason']}), so this reflects "
            "training load only.)"
        )
    if f.get("stale"):
        return (
            f"(The only recovery data available was from an earlier day ({', '.join(f['stale'])}), "
            "so it was ignored and this reflects training load only.)"
        )
    if f.get("unverified"):
        return (
            "(Garmin returned recovery data that could not be confirmed as today's, so it was "
            "ignored and this reflects training load only.)"
        )
    return (
        "(No recovery data had synced yet, so this reflects training load only — use the "
        "condition check once your watch syncs.)"
    )


def _week_ahead(today: date, days: int = 7) -> list[dict[str, Any]]:
    """The upcoming planned days (tomorrow onward) so the morning coach can
    keep the week balanced — e.g. stay light the day before the long run."""
    start = (today + timedelta(days=1)).isoformat()
    end = (today + timedelta(days=days)).isoformat()
    return [
        {"date": r["plan_date"], "kind": r["kind"], "distance_km": r["distance_km"], "status": r["status"]}
        for r in list_planned_workouts(start, end)
    ]


def _this_week(today: date, plan, prog: dict[str, Any]) -> dict[str, Any]:
    """Mon-Sun volume picture: the plan's target for this week vs. what's
    planned and already completed."""
    week_start = today - timedelta(days=today.weekday())
    rows = list_planned_workouts(week_start.isoformat(), (week_start + timedelta(days=6)).isoformat())
    out = {
        "week_start": week_start.isoformat(),
        "planned_km": round(sum(float(r["distance_km"] or 0) for r in rows), 1),
        "completed_km": round(
            sum(float(r["actual_distance_km"] or 0) for r in rows if r["status"] == "completed"), 1
        ),
    }
    try:
        from .goal_planner import weekly_volume

        plan_start = date.fromisoformat(prog["start_date"])
        week_index = max(0, (today - plan_start).days // 7)
        out["target_km"] = weekly_volume(float(plan["base_weekly_km"]), week_index, prog)
    except Exception as exc:
        print(f"[daily_coach._this_week] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return out


def _recent_results(today: date, days: int = 7) -> list[dict[str, Any]]:
    start = (today - timedelta(days=days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    out = []
    for r in list_planned_workouts(start, end):
        out.append(
            {
                "date": r["plan_date"],
                "kind": r["kind"],
                "planned_km": r["distance_km"],
                "status": r["status"],
                "actual_km": r["actual_distance_km"],
                "actual_pace_sec": r["actual_pace_sec"],
            }
        )
    return out


def _rule_adjust(
    base: dict[str, Any],
    paces: dict[str, Any],
    readiness,
    recent: list[dict[str, Any]],
    phase_ctx: dict[str, Any] | None = None,
    long_km: float | None = None,
):
    """Deterministic guardrail workout + safe bounds for the LLM layer.

    `long_km` is this week's planned long-run distance, used to raise the ceiling
    when a missed long run may be moved onto today.
    """
    kind, dist, pace = base["kind"], base["distance_km"], base["target_pace_sec"]
    notes: list[str] = []
    phase = phase_ctx.get("phase") if phase_ctx else None
    in_taper = phase == "taper"
    # Two distinct things readiness can do, with different consequences below:
    # change the session TYPE (which reopens the type restriction, capped at the
    # new type's hardness) versus merely trim the VOLUME (which relaxes the
    # distance bounds but must NOT reopen the type — a yellow strides day keeps
    # its strides, it just gets shorter).
    kind_eased = False
    volume_eased = False

    if readiness.status == "red":
        kind, dist, pace = "rest", 0.0, None
        notes.append("readiness red → rest")
        kind_eased = volume_eased = True
    elif readiness.status == "yellow":
        # A base-phase strides day already runs at easy pace, so there is no
        # intensity to ease out of — trimming the volume is the whole adjustment.
        if kind == "quality" and not is_strides_session(kind, phase=phase):
            kind, pace = "easy", paces.get("easy")
            notes.append("readiness yellow → quality eased to aerobic")
            kind_eased = True
        dist = round(dist * 0.8, 1)
        notes.append("volume trimmed ~20% for caution")
        volume_eased = True

    # A missed long run may be moved onto an easy/recovery day. This fires only
    # when readiness is green — exactly where the kind lock below would otherwise
    # be in force — so 'long' has to be admitted explicitly and the ceiling raised
    # to the long run's own distance, or the suggestion could not be acted on.
    offer_long = (
        any(r["kind"] == "long" and r["status"] == "missed" for r in recent)
        and kind in ("easy", "recovery")
        and readiness.status == "green"
        and not in_taper
    )
    if offer_long:
        notes.append("a long run was missed this week — consider shifting it here")

    if in_taper:
        notes.append(
            f"taper week {phase_ctx.get('phase_week')}/{phase_ctx.get('phase_weeks')} — protect freshness, never add volume"
        )

    eased = kind_eased or volume_eased

    if eased:
        # Readiness eased today, so the eased prescription is the ceiling: the
        # coach may go further down but not restore what was just removed.
        max_km = round(dist, 1)
    elif in_taper:
        # During taper the plan is a hard ceiling — a missed session is never
        # "made up" this close to the race.
        max_km = round(base["distance_km"], 1)
    else:
        max_km = round(max(dist, base["distance_km"]) * 1.1, 1)
        if offer_long and long_km:
            max_km = max(max_km, round(long_km, 1))

    if kind_eased:
        # The session type itself was eased, so the coach may ease it further —
        # but only to types at or below the new one. On a red day that leaves
        # ['rest'] alone. An unrecognised kind falls back to the 'easy' ceiling
        # rather than to no ceiling: a safety bound must fail closed.
        ceiling = _HARDNESS.get(kind, _HARDNESS["easy"])
        if kind not in _HARDNESS:
            print(
                f"[daily_coach._rule_adjust] unrecognised kind {kind!r}; capping the coach at 'easy'",
                file=sys.stderr,
                flush=True,
            )
        allowed = [k for k in ALLOWED_KINDS if _HARDNESS[k] <= ceiling]
    elif volume_eased:
        # Volume was trimmed but the session type still stands (a strides day has
        # no intensity to ease out of). The coach may keep it or call the day off,
        # but not swap it for a different session — demoting a strides day to a
        # plain easy run is the exact loss this guardrail exists to prevent.
        allowed = ["rest", kind]
    elif offer_long:
        allowed = [kind, "long"]
    else:
        # Nothing this morning warrants a change of session type, so it is fixed:
        # the coach may still tune volume and wording, but it may not swap the
        # planned session for a different kind. Without this lock a green, on-plan
        # day could still be rewritten — which is how base-phase strides days were
        # being silently demoted to plain easy runs, with no later step ever
        # reconciling the loss.
        allowed = [kind]

    if eased:
        # Readiness justifies easing, so the coach may go all the way to rest.
        min_km = 0.0
    else:
        # A locked day may be tuned, not cancelled. Without a floor the coach
        # could send distance 0 with the planned kind, and since every push path
        # tests `distance_km <= 0` the day would become a rest day on the watch —
        # cancelling a session the lock says it may not change.
        min_km = round(dist * 0.7, 1)

    bounds = {"max_distance_km": max_km, "min_distance_km": min_km, "allowed_kinds": allowed}
    final = {
        "kind": kind,
        "distance_km": dist,
        "target_pace_sec": pace,
        "details": details_for(kind, pace, phase=phase),
        "coach_note": "; ".join(notes) if notes else "On plan — execute as prescribed.",
    }
    return final, bounds


def _merge_clamp(
    ai: dict[str, Any],
    fallback: dict[str, Any],
    bounds: dict[str, Any],
    phase: str | None = None,
    paces: dict[str, Any] | None = None,
):
    """Fold the coach's answer into the rule suggestion, enforcing the bounds.

    Anything the bounds reject is replaced by the plan's own value, and the prose
    and note are replaced along with it — a session described by the coach's
    rejected reasoning would tell the athlete their day was changed when it
    wasn't, which is the drift this whole layer exists to prevent.
    """
    ai_kind = ai.get("kind")
    kind = ai_kind if ai_kind in bounds["allowed_kinds"] else fallback["kind"]
    kind_rejected = bool(ai_kind) and ai_kind != kind
    if kind_rejected:
        print(
            f"[daily_coach._merge_clamp] coach proposed kind {ai_kind!r}; keeping {kind!r} "
            f"(allowed: {bounds['allowed_kinds']})",
            file=sys.stderr,
            flush=True,
        )
    try:
        dist = float(ai.get("distance_km"))
    except (TypeError, ValueError):
        dist = fallback["distance_km"]
    floor = float(bounds["min_distance_km"])
    clamped = max(floor, min(round(dist, 1), bounds["max_distance_km"]))
    dist_clamped = abs(clamped - round(dist, 1)) > 0.01
    if dist_clamped:
        print(
            f"[daily_coach._merge_clamp] coach proposed {round(dist, 1)} km; clamped to {clamped} km "
            f"(allowed {floor}-{bounds['max_distance_km']} km)",
            file=sys.stderr,
            flush=True,
        )
    dist = clamped

    # A run kind with zero distance is pushed as a rest day, so say rest outright
    # rather than storing an incoherent "quality, 0 km". Only reachable when the
    # bounds already permit rest, i.e. when readiness eased the day.
    kind_zeroed = False
    if dist <= 0 and kind != "rest" and "rest" in bounds["allowed_kinds"]:
        kind, kind_zeroed = "rest", True

    # The pace always follows the chosen kind. A coach-supplied pace is never used:
    # it is the one field that can change a session's intensity without changing
    # anything the bounds check, so an "easy" day could be prescribed at threshold.
    pace = pace_for(kind, paces or {}, phase=phase) if paces is not None else None
    if pace is None and kind == fallback["kind"]:
        pace = fallback["target_pace_sec"]

    try:
        ai_pace = int(ai.get("target_pace_sec")) if ai.get("target_pace_sec") else None
    except (TypeError, ValueError):
        ai_pace = None
    # A faster pace than the day's (lower sec/km) is an attempt to harden the
    # session, and discards the coach's wording along with it. One exception: the
    # plan's own pace for this kind, which on a base-phase strides day is the
    # near-goal quality pace rather than the easy pace the day actually runs at.
    # A coach quoting that is reading the wrong field, not asking for a harder
    # session — and treating it as one discarded the note on every strides day.
    quoted_plan_pace = ai_pace is not None and ai_pace == (paces or {}).get(kind)
    pace_hardened = bool(ai_pace) and pace is not None and ai_pace < pace and not quoted_plan_pace
    if ai_pace and pace is not None and ai_pace != pace:
        print(
            f"[daily_coach._merge_clamp] coach proposed pace {ai_pace}; using the plan's {pace} for {kind!r}"
            + (" (rejected as harder than prescribed)" if pace_hardened else ""),
            file=sys.stderr,
            flush=True,
        )
    # Anything the bounds rejected makes the coach's wording describe a session we
    # are not doing, so the plan's own wording and the rule note replace it. A
    # clamped distance counts: without it the guardrail stored 3.6 km while the
    # email and the Garmin description both said "take a full rest day".
    overridden = kind_rejected or kind_zeroed or dist_clamped or pace_hardened
    details = (None if overridden else ai.get("details")) or details_for(kind, pace, phase=phase)
    note = (None if overridden else ai.get("coach_note")) or fallback["coach_note"]
    return {"kind": kind, "distance_km": dist, "target_pace_sec": pace, "details": details, "coach_note": note}


def adapt_today(
    today: date | None = None,
    use_live_metrics: bool = True,
    recovery: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Compute (and store) today's adapted workout. Returns the workout dict or
    None if there's no active goal/plan.

    `recovery` is an already-fetched `morning_metrics()` result. The morning job
    checks freshness before committing anything, and passes its snapshot in here
    so the decision and the gate see the same data without a second Garmin call.

    A day the athlete has already completed is returned untouched: re-adapting it
    would reset `status` to 'planned', orphan the recorded actuals, and schedule a
    workout for a session already run.
    """
    today = today or date.today()
    ds = today.isoformat()
    goal = get_active_goal()
    plan = get_active_plan()
    if goal is None or plan is None:
        return None

    link_actuals(today)
    # Always ensure the horizon exists (not just when today is missing) so the
    # week-ahead context below has real rows to show the coach.
    ensure_horizon(today)
    base = get_planned_workout(ds)
    if base is None:
        return None
    base = dict(base)

    # Snapshot what is currently stored — and therefore what was pushed to the
    # watch — BEFORE the in-place corrections below. Reading these afterwards made
    # a correction invisible to the change detection at the end, so the stale
    # Garmin workout was never replaced.
    prior_kind = base["kind"]
    prior_dist = float(base["distance_km"] or 0)
    prior_pace = base["target_pace_sec"] or None
    prior_structure = base.get("structure") or None
    prior_workout_id = base.get("garmin_workout_id")

    if base["status"] in ("completed", "paused"):
        return {
            "date": ds,
            **{k: base[k] for k in ("kind", "distance_km", "target_pace_sec", "details", "coach_note", "source")},
            "title": title_for(base["kind"], structure=row_structure(base, today)),
            "structure": row_structure(base, today),
            "status": base["status"],
            "engine": "unchanged",
            "skipped": f"today is already {base['status']}",
        }

    prog = json.loads(plan["progression"])
    paces = prog.get("paces", {})
    runs = [dict(r) for r in list_runs(limit=500)]
    # Metrics first: readiness is only physiological if they feed into it.
    if recovery is not None:
        metrics, metrics_freshness = recovery
    elif use_live_metrics:
        metrics, metrics_freshness = morning_metrics(today)
    else:
        from .garmin_metrics import no_freshness

        metrics, metrics_freshness = {}, no_freshness("live metrics not requested")
    readiness = calculate_readiness(runs, today=today, metrics=metrics)
    recent = _recent_results(today)
    phase_ctx = phase_context(prog, today)
    phase = phase_ctx.get("phase") if phase_ctx else None

    # A strides day's body is easy pace by definition. Rows written before that
    # rule existed — and rows the projection refresh won't touch because they are
    # already pushed or coach-adapted — still carry the near-goal quality pace,
    # which would pin the easy body to threshold. Correct it before it is used.
    if is_strides_session(base["kind"], phase=phase):
        correct_pace = pace_for(base["kind"], paces, phase=phase)
        if correct_pace and base["target_pace_sec"] != correct_pace:
            print(
                f"[daily_coach.adapt_today] {ds}: strides day carried pace "
                f"{base['target_pace_sec']} (near-goal); correcting to easy pace {correct_pace}",
                file=sys.stderr,
                flush=True,
            )
            base["target_pace_sec"] = correct_pace
            base["details"] = details_for(base["kind"], correct_pace, phase=phase)

    long_km = None
    try:
        from .goal_planner import KIND_RATIO, weekly_volume

        week_index = max(0, (today - date.fromisoformat(prog["start_date"])).days // 7)
        long_km = round(weekly_volume(float(plan["base_weekly_km"]), week_index, prog) * KIND_RATIO["long"], 1)
    except Exception as exc:
        print(f"[daily_coach.adapt_today] long-run distance unavailable: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    suggestion, bounds = _rule_adjust(base, paces, readiness, recent, phase_ctx, long_km=long_km)

    context = {
        "today_date": ds,
        "goal": {"distance_km": goal["distance_km"], "target_seconds": goal["target_seconds"], "race_date": goal["race_date"]},
        "long_term_plan": phase_ctx or "no phased roadmap (plan predates phase support)",
        "today_planned": {
            "kind": base["kind"],
            "distance_km": base["distance_km"],
            "target_pace_sec": base["target_pace_sec"],
            # The actual prescription, not just the kind label. Without this the
            # coach saw only kind="quality" and demoted base-phase strides days
            # for looking like hard sessions.
            "details": base["details"],
            "session": title_for(base["kind"], structure=structure_for(base["kind"], phase=phase)),
            "is_hard_session": base["kind"] == "quality" and not is_strides_session(base["kind"], phase=phase),
        },
        "rule_suggestion": suggestion,
        "safety_bounds": bounds,
        "plan_paces_sec_per_km": paces,
        "this_week": _this_week(today, plan, prog),
        "week_ahead": _week_ahead(today),
        "readiness": {
            "score": readiness.score,
            "status": readiness.status,
            "reasons": readiness.reasons,
            "from_recovery_metrics": readiness.physiological,
        },
        "morning_metrics": metrics,
        "morning_metrics_freshness": metrics_freshness,
        "recent_results": recent,
    }
    ai = coach_adjust(context)
    final = _merge_clamp(ai, suggestion, bounds, phase, paces) if isinstance(ai, dict) else suggestion
    source = "adapted" if isinstance(ai, dict) else "rule"

    # Say so when the session was set without recovery data, rather than letting
    # it read like a considered judgement about how the athlete is this morning.
    # The reason matters: telling someone their watch hasn't synced when the real
    # problem is a revoked Garmin token sends them to fix the wrong thing.
    if use_live_metrics and not readiness.physiological:
        final["coach_note"] = final["coach_note"].rstrip() + " " + _blind_disclosure(metrics_freshness)
        try:
            from .db import add_sync_log

            errors = (metrics_freshness or {}).get("errors")
            add_sync_log(
                "error" if errors else "warn",
                (
                    f"morning adapt ran without recovery metrics — Garmin refused the calls: {errors}"
                    if errors
                    else f"morning adapt ran without recovery metrics: {metrics_freshness}"
                ),
                0,
            )
        except Exception as exc:
            print(f"[daily_coach.adapt_today] add_sync_log failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    # Detect a change the athlete would see on the watch, so the prior Garmin push
    # can be removed and the next push replaces the day cleanly. The `prior_*`
    # values were captured before this function's own corrections, and `structure`
    # is included because it decides the steps: a NULL -> 'strides' transition
    # changes the session from one steady block into a strides workout, and
    # omitting it left the stale workout on the watch.
    final_structure = structure_for(final["kind"], phase=phase)
    new_pace = final.get("target_pace_sec") or None
    changed = (
        final["kind"] != prior_kind
        or abs(float(final["distance_km"] or 0) - prior_dist) > 0.01
        or new_pace != prior_pace
        or final_structure != prior_structure
    )
    upsert_planned_workout(
        ds, final["kind"], final["distance_km"], final["target_pace_sec"], final["details"],
        source=source, coach_note=final["coach_note"], status="planned", structure=final_structure,
    )
    if changed:
        # Auto-unschedule the previous Garmin workout so the next push replaces
        # (rather than duplicates) the day on the calendar. A failed delete
        # leaves a dangling Garmin workout — log it so the user can see why.
        if prior_workout_id:
            try:
                from .workout_publisher import delete_garmin_workout

                result = delete_garmin_workout(str(prior_workout_id))
                if isinstance(result, dict) and result.get("status") != "ok":
                    print(
                        f"[daily_coach.delete_prior_workout] non-ok status: {result}",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"[daily_coach.delete_prior_workout] {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        clear_garmin_pushed(ds)
    return {
        "date": ds,
        **final,
        # Session name as prescribed (e.g. "Easy run + strides") so the morning
        # message names the same session that lands on the watch.
        "title": title_for(final["kind"], structure=final_structure),
        "structure": final_structure,
        "engine": "openai+rules" if isinstance(ai, dict) else "rules-only",
        "readiness": readiness.__dict__,
        # The diagnostic breakdown behind `readiness.physiological` — which
        # metrics landed, and whether the rest were stale, undatable, missing or
        # refused by Garmin. The morning job gates on freshness BEFORE calling
        # this function; here it is what the dashboard shows the athlete.
        "metrics_freshness": metrics_freshness,
        "morning_metrics": metrics,
    }


# --- Coach-driven plan edits (confirm-then-apply via /goal/coach/apply) -------

_EDIT_HORIZON_DAYS = 28


def _plan_paces() -> dict[str, Any]:
    """Pace map (easy/long/recovery/quality → sec/km) from the active plan."""
    plan = get_active_plan()
    if plan is None:
        return {}
    try:
        return json.loads(plan["progression"]).get("paces", {}) or {}
    except Exception:
        return {}


def _cap_distance_km() -> float:
    """Upper safety bound on any single day's distance, from the plan's cap."""
    plan = get_active_plan()
    if plan is not None:
        try:
            cap = json.loads(plan["progression"]).get("cap_km")
            if cap:
                return float(cap)
        except Exception:
            pass
    goal = get_active_goal()
    if goal is not None:
        return round(float(goal["distance_km"]) * 2.2, 1)
    return 50.0


def _validate_date(ds: Any, today: date) -> str | None:
    """Return a validated ISO date string within the edit horizon, else None."""
    try:
        d = date.fromisoformat(str(ds))
    except (TypeError, ValueError):
        return None
    if d < today or d > today + timedelta(days=_EDIT_HORIZON_DAYS):
        return None
    return d.isoformat()


def _repush_if_needed(plan_date: str) -> dict[str, Any] | None:
    """If a date already has a Garmin workout, delete it and push the new
    version so the watch reflects the edit. Returns a small status dict or None
    if nothing was pushed (web-only day)."""
    row = get_planned_workout(plan_date)
    if row is None:
        return None
    prior_id = row["garmin_workout_id"]
    if not prior_id:
        return None  # web-only day; morning/explicit push will handle it
    from .db import mark_garmin_pushed
    from .workout_publisher import (
        delete_garmin_workout,
        push_workout_to_garmin,
        structured_workout_for_planned_row,
    )

    clear_garmin_pushed(plan_date)
    try:
        delete_garmin_workout(str(prior_id))
    except Exception as exc:
        print(f"[daily_coach.apply.delete] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    if row["kind"] == "rest" or float(row["distance_km"] or 0) <= 0:
        return {"date": plan_date, "garmin": "unscheduled_rest"}
    d = date.fromisoformat(plan_date)
    structured = structured_workout_for_planned_row(row, d)
    try:
        result = push_workout_to_garmin(structured, schedule_date=d)
        wid = result.get("workout_id") if isinstance(result, dict) else None
        if wid and result.get("scheduled"):
            mark_garmin_pushed(plan_date, str(wid))
            return {"date": plan_date, "garmin": "repushed", "workout_id": wid}
        return {"date": plan_date, "garmin": "push_partial", "detail": result}
    except Exception as exc:
        print(f"[daily_coach.apply.push] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return {"date": plan_date, "garmin": "push_failed", "error": str(exc)[:200]}


def apply_plan_change(change: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Validate and apply a coach-proposed plan change. Bounded: only future
    dates within the edit horizon, kinds restricted to ALLOWED_KINDS, distance
    clamped to [0, plan cap]. Returns {status, ...} — status='applied' on
    success, 'rejected' with a reason otherwise."""
    today = today or date.today()
    if get_active_goal() is None:
        return {"status": "rejected", "reason": "no active goal"}
    if not isinstance(change, dict):
        return {"status": "rejected", "reason": "no change provided"}

    action = str(change.get("action") or "").strip()
    paces = _plan_paces()
    cap = _cap_distance_km()

    if action in ("adjust_day", "rest_day"):
        ds = _validate_date(change.get("date"), today)
        if ds is None:
            return {"status": "rejected", "reason": f"date out of range or invalid: {change.get('date')!r}"}
        if action == "rest_day":
            kind, dist = "rest", 0.0
        else:
            kind = str(change.get("kind") or "").strip().lower()
            if kind not in ALLOWED_KINDS:
                # default to keeping the existing kind if the model omitted/garbled it
                existing = get_planned_workout(ds)
                kind = existing["kind"] if existing else "easy"
            if kind == "rest":
                dist = 0.0
            else:
                try:
                    dist = float(change.get("distance_km"))
                except (TypeError, ValueError):
                    existing = get_planned_workout(ds)
                    dist = float(existing["distance_km"]) if existing else 0.0
                dist = max(0.0, min(round(dist, 1), cap))
        # Phase-aware so a coach-chat edit lands the same session the plan would:
        # a base-phase quality day keeps its easy pace and strides prescription.
        phase = phase_name_for(date.fromisoformat(ds))
        pace = None if kind == "rest" else pace_for(kind, paces, phase=phase)
        upsert_planned_workout(
            ds, kind, dist, pace, details_for(kind, pace, phase=phase),
            source="adapted", coach_note="Adjusted via coach chat.", status="planned",
            structure=structure_for(kind, phase=phase),
        )
        garmin = _repush_if_needed(ds)
        return {"status": "applied", "action": action, "date": ds, "kind": kind, "distance_km": dist, "garmin": garmin}

    if action == "swap_days":
        d1 = _validate_date(change.get("date"), today)
        d2 = _validate_date(change.get("date2"), today)
        if d1 is None or d2 is None:
            return {"status": "rejected", "reason": "both dates must be valid and within range"}
        if d1 == d2:
            return {"status": "rejected", "reason": "the two dates are the same"}
        r1 = get_planned_workout(d1)
        r2 = get_planned_workout(d2)
        if r1 is None or r2 is None:
            return {"status": "rejected", "reason": "one of the dates has no planned workout"}
        # Swap kind/distance/pace/details, mark both adapted. The session shape
        # travels with the session so a moved strides day is still pushed as one.
        upsert_planned_workout(
            d1, r2["kind"], r2["distance_km"], r2["target_pace_sec"], r2["details"],
            source="adapted", coach_note="Swapped via coach chat.", status="planned",
            structure=row_structure(r2, date.fromisoformat(d2)),
        )
        upsert_planned_workout(
            d2, r1["kind"], r1["distance_km"], r1["target_pace_sec"], r1["details"],
            source="adapted", coach_note="Swapped via coach chat.", status="planned",
            structure=row_structure(r1, date.fromisoformat(d1)),
        )
        g1 = _repush_if_needed(d1)
        g2 = _repush_if_needed(d2)
        return {"status": "applied", "action": "swap_days", "date": d1, "date2": d2, "garmin": [g1, g2]}

    return {"status": "rejected", "reason": f"unknown action: {action!r}"}
