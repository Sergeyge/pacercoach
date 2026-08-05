from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

WORKOUT_DIR = Path(__file__).resolve().parent.parent / "workouts"


class WorkoutPublishError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_value(minutes: float) -> float:
    return round(minutes * 60, 1)


def _distance_value(km: float) -> float:
    return round(km * 1000, 1)


def _pace_target(pace_sec: Any) -> str:
    """Encode a per-km pace (seconds) as a neutral pace target token, or
    'no_target' when no usable pace is given. Decoded in `_target_payload`."""
    try:
        s = int(pace_sec)
    except (TypeError, ValueError):
        return "no_target"
    if s <= 0:
        return "no_target"
    return f"pace:{s}"


def _shaped_steps(shape: dict[str, Any], distance_km: float, pace_token: str, pace_sec: Any) -> list[dict[str, Any]]:
    """Build the watch steps for a quality shape (see `goal_planner.QUALITY_SHAPES`).

    Two layouts, selected by the shape's `easy_body`:

    * `easy_body` (a strides day) — the planned distance MINUS the timed tail is
      run at easy pace and the reps are appended. The reps and cool-down are
      time-based, so their distance (converted at the easy pace) is subtracted from
      the planned total to keep overall volume on plan. Whether the reps carry the
      pace target is `reps_at_pace`, which strides set False: they are run fast by
      feel, and the jog recoveries are deliberately untargeted, so pinning either
      to a pace would misrepresent the session.
    * otherwise (tempo / intervals / sharpener) — a time-based warm-up, the reps
      at the day's pace, then a cool-down. These are prescribed by duration, so
      the planned distance is carried as metadata only.
    """
    reps = int(shape["reps"])
    work = int(shape["work_seconds"])
    recovery = int(shape["recovery_seconds"])
    rep_block = {
        "kind": "repeat",
        "repeat": reps,
        "steps": [
            {
                "name": "Stride" if shape["easy_body"] else "Work",
                "kind": "run",
                "duration_type": "time",
                "duration_seconds": work,
                "target": pace_token if shape["reps_at_pace"] else "no_target",
            },
            {
                "name": "Jog recovery",
                "kind": "recovery",
                "duration_type": "time",
                "duration_seconds": recovery,
                "target": "no_target",
            },
        ],
    }
    cooldown = {
        "name": "Cool Down",
        "kind": "cooldown",
        "duration_type": "time",
        "duration_seconds": int(shape["cooldown_seconds"]),
        "target": "no_target",
    }

    if not shape["easy_body"]:
        return [
            {
                "name": "Warm Up",
                "kind": "warmup",
                "duration_type": "time",
                "duration_seconds": int(shape["warmup_seconds"]),
                "target": "no_target",
            },
            rep_block,
            cooldown,
        ]

    tail_seconds = reps * (work + recovery) + int(shape["cooldown_seconds"])
    try:
        easy_pace = int(pace_sec)
    except (TypeError, ValueError):
        easy_pace = 0
    if easy_pace > 0:
        tail_km = tail_seconds / easy_pace
    else:
        # No usable pace means the tail can't be converted to distance, so the
        # volume-conservation subtraction below would silently do nothing and the
        # athlete would run the full planned distance plus the whole tail. Say so.
        tail_km = 0.0
        print(
            f"[workout_publisher._shaped_steps] no target pace for a {shape['structure']} session; "
            f"pushing the full {distance_km:g} km body, so total volume runs long by ~{tail_seconds / 60:.0f} min",
            file=sys.stderr,
            flush=True,
        )
    # Never shrink the easy body below 1 km — on a very short planned day the
    # session keeps its strides and simply runs slightly longer than planned.
    body_km = max(1.0, round(distance_km - tail_km, 2))
    return [
        {
            "name": "Easy",
            "kind": "run",
            "duration_type": "distance",
            "distance_meters": _distance_value(body_km),
            "target": pace_token,
        },
        rep_block,
        cooldown,
    ]


def planned_activity_to_structured_workout(
    activity: dict[str, Any],
    workout_date: date | None = None,
    *,
    sniff_title: bool = True,
) -> dict[str, Any]:
    """Convert planner output into a structured running workout object.

    This is our internal neutral model. It can be exported to JSON and translated
    to Garmin Connect's workout-service payload.

    All run targets are PACE-based (Garmin pace.zone), derived from the planned
    `target_pace_sec`. We deliberately never emit heart-rate-zone targets — pace
    is the prescription the plan computes and the athlete trains by. Warm-up,
    cool-down, and the recovery jogs inside interval sessions are left as
    `no_target` (easy by feel) rather than pinned to a pace.

    `activity["structure"]` names the session shape (see
    `goal_planner.QUALITY_SHAPES`). The title is sniffed only as a fallback for
    activities that carry no structure — the legacy `/workouts/*` flows, which
    build their own dicts. Callers that resolve the shape themselves pass
    `sniff_title=False`, so a `structure` of None means "plain steady run" rather
    than "guess from the title": a shapeless quality day is titled "Quality run",
    which the sniffer would otherwise read as a tempo session.
    """
    workout_date = workout_date or date.fromisoformat(activity["date"])
    title = str(activity.get("title") or "Run")
    distance_km = float(activity.get("distance_km") or 0)
    details = str(activity.get("details") or "")
    pace_token = _pace_target(activity.get("target_pace_sec"))

    if distance_km <= 0:
        return {
            "date": workout_date.isoformat(),
            "name": title,
            "sport": "running",
            "type": "rest",
            "steps": [],
            "notes": details,
        }

    from .goal_planner import QUALITY_SHAPES, SHAPES_BY_STRUCTURE

    structure = str(activity.get("structure") or "").strip().lower()
    if not structure and sniff_title:
        lower = title.lower()
        if "strides" in lower:
            structure = QUALITY_SHAPES["base"]["structure"]
        elif "interval" in lower:
            structure = QUALITY_SHAPES["peak"]["structure"]
        elif "tempo" in lower or "quality" in lower:
            structure = QUALITY_SHAPES["build"]["structure"]

    name = title
    shape = SHAPES_BY_STRUCTURE.get(structure)
    if shape:
        steps = _shaped_steps(shape, distance_km, pace_token, activity.get("target_pace_sec"))
    else:
        if structure:
            print(
                f"[workout_publisher] unknown session structure {structure!r}; "
                "pushing a plain steady run",
                file=sys.stderr,
                flush=True,
            )
        steps = [
            {
                "name": title,
                "kind": "run",
                "duration_type": "distance",
                "distance_meters": _distance_value(distance_km),
                "target": pace_token,
            }
        ]

    return {
        "date": workout_date.isoformat(),
        "name": name,
        "sport": "running",
        "type": "workout",
        "planned_distance_km": distance_km,
        "notes": details,
        "steps": steps,
    }


def structured_workout_for_planned_row(row: Any, plan_date: date) -> dict[str, Any]:
    """Build the watch workout for a planned_workout row (or an `adapt_today`
    result), taking the session shape from the row so a base-phase quality day
    becomes an easy run + strides instead of a tempo session.

    Every goal-flow push path goes through here, so the session the athlete gets
    on the watch cannot diverge between the morning job and a manual push.
    """
    from .goal_planner import row_structure, title_for

    kind = row["kind"]
    structure = row_structure(row, plan_date)
    return planned_activity_to_structured_workout(
        {
            "title": title_for(kind, structure=structure),
            "distance_km": row["distance_km"],
            "target_pace_sec": row["target_pace_sec"],
            "details": row["details"] or "",
            "date": plan_date.isoformat(),
            "structure": structure,
        },
        workout_date=plan_date,
        # The shape is already resolved here — None genuinely means "plain steady
        # run", so the title must not be second-guessed.
        sniff_title=False,
    )


def save_workout_json(workout: dict[str, Any]) -> Path:
    WORKOUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_date = str(workout.get("date", date.today().isoformat()))
    safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(workout.get("name", "workout")))
    path = WORKOUT_DIR / f"{safe_date}_{safe_name}.json"
    path.write_text(json.dumps(workout, indent=2), encoding="utf-8")
    return path


def _step_type(kind: str) -> dict[str, Any]:
    mapping = {
        "warmup": (1, "warmup", 1),
        "cooldown": (2, "cooldown", 2),
        "recovery": (4, "recovery", 4),
        "run": (3, "interval", 3),
    }
    step_type_id, key, order = mapping.get(kind, mapping["run"])
    return {"stepTypeId": step_type_id, "stepTypeKey": key, "displayOrder": order}


def _end_condition(step: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
    # Garmin condition type ids: 1=lap.button, 2=time, 3=distance, 4=calories,
    # 7=iterations. Garmin resolves the numeric id (not the key), so a wrong id
    # silently relabels the step's duration unit on the watch.
    if step.get("duration_type") == "distance":
        return {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}, float(step["distance_meters"])
    return {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}, float(step.get("duration_seconds", 0))


def _target_payload(target: str) -> tuple[dict[str, Any] | None, float | None, float | None]:
    # Garmin private APIs are not officially documented. These values mirror the
    # common Garmin Connect workout payload shape and may need adjustment if
    # Garmin changes their web API.
    #
    # Pace targets ("pace:<sec_per_km>") become a Garmin pace.zone with a small
    # window around the goal pace, expressed as speeds in metres/second.
    # targetValueOne = lower speed (the slower bound), targetValueTwo = upper
    # speed (the faster bound). Heart-rate zones are intentionally not emitted —
    # the plan prescribes pace, so all run targets are pace-based.
    if target.startswith("pace:"):
        try:
            pace_sec = int(target.split(":", 1)[1])
        except (ValueError, IndexError):
            return None, None, None
        if pace_sec <= 0:
            return None, None, None
        window = 8  # ± seconds per km around the goal pace
        slow_speed = round(1000.0 / (pace_sec + window), 3)  # slower → lower m/s
        fast_speed = round(1000.0 / max(1, pace_sec - window), 3)  # faster → higher m/s
        return {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}, slow_speed, fast_speed
    return None, None, None


def _executable_step(step: dict[str, Any], order: int, child_step_id: int | None) -> dict[str, Any]:
    end_condition, end_value = _end_condition(step)
    target_type, target_low, target_high = _target_payload(str(step.get("target") or "no_target"))
    return {
        "type": "ExecutableStepDTO",
        "stepId": None,
        "stepOrder": order,
        "childStepId": child_step_id,
        "description": step.get("name"),
        "stepType": _step_type(str(step.get("kind") or "run")),
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "preferredEndConditionUnit": None,
        "targetType": target_type,
        "targetValueOne": target_low,
        "targetValueTwo": target_high,
        "zoneNumber": None,  # pace.zone targets don't use a zone number
    }


def _estimated_duration_seconds(steps: list[dict[str, Any]]) -> float:
    total = 0.0
    for step in steps:
        repeat = int(step.get("repeat") or 1)
        if step.get("kind") == "repeat":
            total += repeat * _estimated_duration_seconds(step.get("steps") or [])
        else:
            total += repeat * float(step.get("duration_seconds") or 0)
    return total


def to_garmin_workout_payload(workout: dict[str, Any]) -> dict[str, Any]:
    order = 1
    group_id = 0

    def build(source_steps: list[dict[str, Any]], child_step_id: int | None = None) -> list[dict[str, Any]]:
        # stepOrder is a single pre-order sequence across the whole workout,
        # including steps nested inside repeat groups — that sequence is what
        # Garmin executes, so interleaving (e.g. tempo/recovery) must happen
        # here, not by flattening repeats consecutively.
        nonlocal order, group_id
        built: list[dict[str, Any]] = []
        for step in source_steps:
            if step.get("kind") == "repeat":
                group_id += 1
                iterations = int(step.get("repeat") or 1)
                group = {
                    "type": "RepeatGroupDTO",
                    "stepId": None,
                    "stepOrder": order,
                    "childStepId": group_id,
                    "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                    "numberOfIterations": iterations,
                    "smartRepeat": False,
                    "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                    "endConditionValue": float(iterations),
                }
                order += 1
                group["workoutSteps"] = build(step.get("steps") or [], child_step_id=group_id)
                built.append(group)
            else:
                for _ in range(int(step.get("repeat") or 1)):
                    built.append(_executable_step(step, order, child_step_id))
                    order += 1
        return built

    steps = build(workout.get("steps", []))

    estimated_distance = float(workout.get("planned_distance_km") or 0) * 1000
    estimated_duration = _estimated_duration_seconds(workout.get("steps", []))
    payload = {
        "workoutId": None,
        "ownerId": None,
        "workoutName": workout.get("name") or "Run",
        "description": workout.get("notes") or "Generated by Running Assistant",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
        "subSportType": None,
        "estimatedDurationInSecs": round(estimated_duration) if estimated_duration else None,
        "estimatedDistanceInMeters": round(estimated_distance, 1) if estimated_distance else None,
        "estimatedCalories": None,
        "createdDate": _now_iso(),
        "updatedDate": _now_iso(),
        "workoutProvider": "RUNNING_ASSISTANT",
        "workoutSourceId": None,
        "workoutNameI18nKey": None,
        "consumer": None,
        "atpPlanId": None,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
                "workoutSteps": steps,
            }
        ],
    }
    return payload


def _login_garmin():
    from .garmin_client import GarminClientError, get_garmin_client

    try:
        return get_garmin_client()
    except GarminClientError as exc:
        raise WorkoutPublishError(str(exc)) from exc


def delete_garmin_workout(workout_id: str) -> dict[str, Any]:
    """Delete a workout from Garmin Connect (also removes its schedule entries).
    Best-effort: returns a status dict instead of raising — used when we re-push
    an adapted day and don't want to leave the previous workout as a duplicate."""
    if not workout_id:
        return {"status": "skipped", "reason": "no workout_id"}
    try:
        client = _login_garmin()
    except WorkoutPublishError as exc:
        return {"status": "error", "error": str(exc)}
    try:
        client.client.delete("connectapi", f"/workout-service/workout/{workout_id}", api=True)
        return {"status": "ok", "workout_id": workout_id}
    except Exception as exc:
        return {"status": "error", "workout_id": workout_id, "error": str(exc)[:200]}


def push_workout_to_garmin(workout: dict[str, Any], schedule_date: date | None = None) -> dict[str, Any]:
    if not workout.get("steps"):
        raise WorkoutPublishError("Cannot push a rest day to Garmin as a workout")

    client = _login_garmin()
    payload = to_garmin_workout_payload(workout)

    # Garmin Connect private web API via the community client. In garminconnect
    # 0.3.x, connectapi() is GET-only; writes go through the low-level
    # client.client.post/delete("connectapi", path, ...). These target
    # https://connectapi.garmin.com, attach the OAuth token, and (api=True)
    # return parsed JSON, raising on a non-2xx response.
    try:
        created = client.client.post("connectapi", "/workout-service/workout", json=payload, api=True)
    except Exception as exc:
        raise WorkoutPublishError(f"Garmin workout create failed: {exc}") from exc

    created = created or {}
    workout_id = created.get("workoutId") or created.get("id")
    if not workout_id:
        raise WorkoutPublishError(f"Garmin workout create returned no workout id: {created}")

    scheduled = False
    schedule_error = None
    if schedule_date and workout_id:
        # This endpoint is unofficial and may differ per Garmin Connect release.
        # A chronic schedule failure would otherwise hide silently — the workout
        # gets created, status is "ok", and the caller's `mark_garmin_pushed`
        # runs anyway. Log so an operator can see it; callers should also check
        # `result["scheduled"]` before treating the day as fully pushed.
        try:
            client.client.post(
                "connectapi",
                f"/workout-service/schedule/{workout_id}",
                json={"date": schedule_date.isoformat()},
                api=True,
            )
            scheduled = True
        except Exception as exc:
            schedule_error = str(exc)
            print(
                f"[workout_publisher.schedule] {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    return {
        "status": "ok",
        "workout_id": workout_id,
        "workout_name": payload["workoutName"],
        "scheduled": scheduled,
        "schedule_date": schedule_date.isoformat() if schedule_date else None,
        "schedule_error": schedule_error,
        "note": "After Garmin Connect syncs, the workout should be available on compatible watches. If scheduling fails, open Garmin Connect and add the created workout to the calendar manually.",
    }
