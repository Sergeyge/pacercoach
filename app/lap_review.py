"""Lap-level reading of a completed run.

A structured session's whole-run average pace is a blend of warm-up, work reps,
jog recoveries and cool-down, so it is not comparable to the day's rep target:
a perfectly executed 3 x 8 min at 5:36/km inside an 8.6 km session averages out
near easy pace. Comparing the two is what made a review call a well-run tempo
day "33 s/km slower than target". These helpers pull the work reps back out of
the lap splits so the reps are judged against the pace they were prescribed at.
"""

from __future__ import annotations

from typing import Any

# Garmin's per-lap `intensityType` on a run that followed a structured workout.
_ROLE_BY_INTENSITY: dict[str, str] = {
    "ACTIVE": "work",
    "INTERVAL": "work",
    "REST": "recovery",
    "RECOVERY": "recovery",
    "WARMUP": "warmup",
    "COOLDOWN": "cooldown",
}

# Seeing any of these proves the laps came from a structured workout rather than
# from plain auto-splits — a free run's laps are all "ACTIVE" (or untyped), which
# on its own says nothing about which laps were the work.
_STRUCTURE_MARKERS = frozenset({"WARMUP", "COOLDOWN", "REST", "RECOVERY"})


def _intensity(lap: dict[str, Any]) -> str:
    return str(lap.get("intensity_type") or "").strip().upper()


def assign_roles(laps: list[dict[str, Any]], shape: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Tag each lap 'warmup'/'work'/'recovery'/'cooldown'/'body' (None if unknown).

    Returns the tagged laps and the detection method used, or None for that
    method when the reps could not be identified — the caller must then say the
    rep paces are unknown rather than fall back to the whole-run average.
    """
    out = [dict(lap) for lap in laps]
    if any(_intensity(lap) in _STRUCTURE_MARKERS for lap in out):
        for lap in out:
            lap["role"] = _ROLE_BY_INTENSITY.get(_intensity(lap))
        _retag_non_reps(out, shape)
        return out, "garmin_intensity"

    # No intensity types (older devices, or the athlete ran the session freely
    # off a manual lap button): match lap durations against the prescribed rep
    # length. Deliberately strict — a wrong guess about which laps were the work
    # is worse than admitting the reps could not be read — and per-lap only, so a
    # rep that auto-lap split across a kilometre mark goes undetected rather than
    # risk gluing a warm-up remainder onto a rep's first half.
    work_seconds = int((shape or {}).get("work_seconds") or 0)
    reps = int((shape or {}).get("reps") or 0)
    if work_seconds > 0 and reps > 0:
        tolerance = max(20, round(work_seconds * 0.15))
        matched = {
            i for i, lap in enumerate(out)
            if abs((lap.get("duration_sec") or 0) - work_seconds) <= tolerance
        }
        if 1 <= len(matched) <= reps:
            for i, lap in enumerate(out):
                lap["role"] = "work" if i in matched else None
            return out, "rep_duration_match"

    for lap in out:
        lap["role"] = None
    return out, None


def _retag_non_reps(laps: list[dict[str, Any]], shape: dict[str, Any] | None) -> None:
    """Take back the 'work' tag from laps that are not one of the prescribed reps.

    Garmin marks the easy body of a strides day ACTIVE, exactly like a 20-second
    stride, so rep length is what separates them — but it takes two passes, in
    this order:

    * Per lap, because the body lap sits directly against the first stride with
      no recovery between them: grouping first would swallow that stride into the
      body and report five strides out of six. A single lap far LONGER than a rep
      cannot be part of one whatever its neighbours are, so this pass is safe
      before any grouping.
    * Then per contiguous group, because auto-lap splits a rep at every kilometre
      and each fragment on its own looks nothing like the prescribed rep. Only
      the whole group can show a rep abandoned early.

    Retagged laps become 'body' on a shape built around an easy body, otherwise
    'extra'.
    """
    work_seconds = int((shape or {}).get("work_seconds") or 0)
    if work_seconds <= 0:
        return
    label = "body" if (shape or {}).get("easy_body") else "extra"
    for lap in laps:
        if lap.get("role") == "work" and (lap.get("duration_sec") or 0) > 2.5 * work_seconds:
            lap["role"] = label
    for group in _group_reps(laps):
        _, duration, _, _ = _totals(group)
        if duration < 0.4 * work_seconds:
            for lap in group:
                lap["role"] = label


def review_laps(
    laps: list[dict[str, Any]] | None,
    target_pace_sec: Any = None,
    shape: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Laps tagged by role, plus the measured execution of the work reps.

    The summary is None when the reps could not be identified — the caller must
    then say the rep paces are unknown rather than fall back to the whole-run
    average, which is the blend this module exists to keep out of reviews.
    """
    labelled, method = assign_roles(laps or [], shape)
    return labelled, _summarize_work(labelled, method, target_pace_sec, shape)


def _group_reps(labelled: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Merge contiguous work laps into one rep per group.

    Auto-lap splits a rep at every kilometre, so an 8-minute rep normally arrives
    as TWO laps — 1.00 km, then the remainder — and counting laps would report
    "6 reps completed" against 3 planned, with each half's pace as a rep. What
    ends a rep is a lap of another role: a jog recovery, or the cool-down.
    """
    reps: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for lap in labelled:
        if lap.get("role") == "work":
            current.append(lap)
        elif current:
            reps.append(current)
            current = []
    if current:
        reps.append(current)
    return reps


def _totals(laps: list[dict[str, Any]]) -> tuple[float, int, int | None, int | None]:
    """Distance, duration, pace and duration-weighted mean HR over some laps."""
    distance = sum(float(lap.get("distance_km") or 0) for lap in laps)
    duration = sum(int(lap.get("duration_sec") or 0) for lap in laps)
    pace = round(duration / distance) if distance > 0 and duration > 0 else None
    timed_hr = [(lap["avg_hr"], lap.get("duration_sec") or 0) for lap in laps if lap.get("avg_hr")]
    hr_seconds = sum(sec for _, sec in timed_hr)
    hr = round(sum(v * sec for v, sec in timed_hr) / hr_seconds) if hr_seconds else None
    return distance, duration, pace, hr


def _summarize_work(
    labelled: list[dict[str, Any]],
    method: str | None,
    target_pace_sec: Any,
    shape: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Aggregate the work reps. Both the per-rep and the overall pace are total
    time over total distance, never a mean of lap paces, so neither an auto-lap
    split mid-rep nor a short final rep skews the number."""
    reps = [g for g in _group_reps(labelled) if any(lap.get("pace_sec") for lap in g)]
    if not reps:
        return None
    per_rep = [_totals(g) for g in reps]
    distance, duration, mean, _ = _totals([lap for g in reps for lap in g])
    if mean is None:
        return None
    summary: dict[str, Any] = {
        "detection": method,
        "reps_completed": len(reps),
        "reps_planned": int(shape["reps"]) if shape and shape.get("reps") else None,
        "work_distance_km": round(distance, 2),
        "work_duration_sec": duration,
        "mean_work_pace_sec": mean,
        "rep_paces_sec": [pace for _, _, pace, _ in per_rep],
        "rep_durations_sec": [dur for _, dur, _, _ in per_rep],
        "rep_avg_hr": [hr for _, _, _, hr in per_rep],
    }
    try:
        target = int(target_pace_sec)
    except (TypeError, ValueError):
        target = 0
    if target > 0:
        summary["target_pace_sec"] = target
        summary["mean_delta_sec_per_km"] = mean - target
        summary["rep_deltas_sec_per_km"] = [
            pace - target if pace else None for _, _, pace, _ in per_rep
        ]
    return summary
