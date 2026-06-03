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


def planned_activity_to_structured_workout(activity: dict[str, Any], workout_date: date | None = None) -> dict[str, Any]:
    """Convert planner output into a structured running workout object.

    This is our internal neutral model. It can be exported to JSON and translated
    to Garmin Connect's workout-service payload.

    All run targets are PACE-based (Garmin pace.zone), derived from the planned
    `target_pace_sec`. We deliberately never emit heart-rate-zone targets — pace
    is the prescription the plan computes and the athlete trains by. Warm-up,
    cool-down, and the recovery jogs inside interval sessions are left as
    `no_target` (easy by feel) rather than pinned to a pace.
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

    lower = title.lower()
    if "tempo" in lower or "interval" in lower or "quality" in lower:
        name = title
        steps = [
            {"name": "Warm Up", "kind": "warmup", "duration_type": "time", "duration_seconds": 600, "target": "no_target"},
            {"name": "Tempo", "kind": "run", "repeat": 3, "duration_type": "time", "duration_seconds": 480, "target": pace_token},
            {"name": "Recovery", "kind": "recovery", "repeat": 3, "duration_type": "time", "duration_seconds": 180, "target": "no_target"},
            {"name": "Cool Down", "kind": "cooldown", "duration_type": "time", "duration_seconds": 600, "target": "no_target"},
        ]
    else:
        name = title
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
    if step.get("duration_type") == "distance":
        return {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}, float(step["distance_meters"])
    return {"conditionTypeId": 4, "conditionTypeKey": "time", "displayOrder": 4, "displayable": True}, float(step.get("duration_seconds", 0))


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


def to_garmin_workout_payload(workout: dict[str, Any]) -> dict[str, Any]:
    steps = []
    order = 1
    for step in workout.get("steps", []):
        repeat = int(step.get("repeat") or 1)
        for _ in range(repeat):
            end_condition, end_value = _end_condition(step)
            target_type, target_low, target_high = _target_payload(str(step.get("target") or "no_target"))
            steps.append(
                {
                    "type": "ExecutableStepDTO",
                    "stepId": None,
                    "stepOrder": order,
                    "childStepId": None,
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
            )
            order += 1

    estimated_distance = float(workout.get("planned_distance_km") or 0) * 1000
    estimated_duration = sum(float(s.get("duration_seconds") or 0) * int(s.get("repeat") or 1) for s in workout.get("steps", []))
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
