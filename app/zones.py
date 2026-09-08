"""Training zones — HR bands from Garmin, pace bands from the plan, and the
athlete's observed speed inside each.

Three separate things, deliberately kept apart because they have different
authorities and different failure modes:

  * **HR zones** are the athlete's own Garmin configuration. We do not invent
    them: garminconnect 0.3.x has no accessor for the zone table, so we read the
    private endpoint and, failing that, recover the boundaries from an activity's
    time-in-zone payload (which carries `zoneLowBoundary` per zone). If neither
    works the answer is "unknown", never a percentage-of-max guess — a fabricated
    zone floor would make every per-zone speed below wrong in a way the athlete
    could not see.

  * **Pace zones** come from the training plan (`goal_planner.derive_paces`), so
    they are *targets*, not measured bands. `derive_paces` returns one value per
    session kind, which is why a "max speed per zone" question has no answer in
    pace terms — a target is a single number. Reported as targets, with the
    lactate-threshold pace alongside when Garmin has one, since that is the one
    genuinely physiological pace anchor available.

  * **Observed speed per zone** is computed from stored lap splits, not from
    whole runs. A run's average HR places the entire session in one zone, so
    bucketing by it reports easy-run pace for Z2 and nothing at all for Z4/Z5.
    Laps put the work reps in the zone they were actually run in. Every figure
    carries its sample size and the window it was drawn from.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

from .db import get_config, lap_coverage, list_activity_laps, set_config

# Cached zone/threshold read. Zones change when the athlete edits their Garmin
# profile or Garmin recomputes a threshold — rare, so a day is plenty, and it
# keeps a chat turn from paying for a Garmin login.
_ZONE_CACHE_KEY = "hr_zones_cache"
_ZONE_CACHE_TTL_SEC = 24 * 3600

# How far back observed per-zone speeds are drawn from. Long enough to have
# samples in the hard zones (which appear only on quality days, roughly weekly),
# short enough that the answer reflects current fitness rather than last season's.
_OBSERVED_WINDOW_DAYS = 90

# A lap must be this long to count toward a per-zone best. Garmin's auto-lap and
# manual lap presses both produce sub-100 m fragments at the end of a run or
# around a rep boundary, and such a lap's pace is dominated by where the split
# fell rather than by how fast the athlete was moving.
_MIN_LAP_KM = 0.2

# Sanity band on a lap pace (sec/km), matching `daily_coach`'s bounds on an
# athlete-entered pace: outside this is a GPS dropout or a stopped-clock lap, not
# a run. Without it one tunnel lap becomes the athlete's "max speed" forever.
_PACE_MIN_SEC = 150
_PACE_MAX_SEC = 900


def _log_exc(where: str, exc: BaseException) -> None:
    print(f"[zones.{where}] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


@contextmanager
def _quiet_garmin_logger():
    """Silence garminconnect's own `logger.exception` for the duration of a call.

    Probing for an endpoint that may not exist is normal here — the configured
    zone table 404s on accounts where only the time-in-zone fallback works — but
    the library logs every failed request as an exception, traceback and all. On
    a daily cache refresh that buries the errors an operator actually needs to
    see. Only wrap calls whose failure is an expected branch, never one whose
    failure means something is wrong: the outcome is still logged by `_log_exc`.
    """
    lg = logging.getLogger("garminconnect")
    previous = lg.level
    lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        lg.setLevel(previous)


def _speed_kmh(pace_sec: Any) -> float | None:
    try:
        p = float(pace_sec)
    except (TypeError, ValueError):
        return None
    return round(3600.0 / p, 2) if p > 0 else None


def _pace_text(pace_sec: Any) -> str | None:
    try:
        p = int(pace_sec)
    except (TypeError, ValueError):
        return None
    return f"{p // 60}:{p % 60:02d}/km" if p > 0 else None


# --- HR zones ----------------------------------------------------------------


def _zones_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    """Normalise Garmin's zone table into ascending `{zone, low_bpm, high_bpm}`.

    Garmin returns the zones as a list of per-sport configurations whose keys have
    varied across releases (`zoneNumber`/`zoneId`, `zoneLowBoundary`/`secsInZone`
    payloads share the boundary field). Only the floor is ever given, so each
    zone's ceiling is the next zone's floor minus one and the top zone is open.
    """
    if isinstance(payload, dict):
        for key in ("heartRateZones", "zones", "zoneList"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not payload:
        return None

    found: dict[int, int] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        num = entry.get("zoneNumber", entry.get("zoneId", entry.get("zone")))
        low = entry.get("zoneLowBoundary", entry.get("lowBoundary", entry.get("low")))
        try:
            num_i, low_i = int(num), int(round(float(low)))
        except (TypeError, ValueError):
            continue
        # Zone 0 is Garmin's "below zone 1" bucket in time-in-zone payloads; it
        # is not a training zone and its floor is 0, which would swallow the
        # whole HR range if kept.
        if num_i >= 1 and low_i > 0:
            found[num_i] = low_i
    if not found:
        return None

    ordered = sorted(found.items())
    out: list[dict[str, Any]] = []
    for i, (num, low) in enumerate(ordered):
        high = ordered[i + 1][1] - 1 if i + 1 < len(ordered) else None
        out.append({"zone": num, "low_bpm": low, "high_bpm": high})
    return out


def _fetch_hr_zones_live() -> dict[str, Any]:
    """Read the zone table from Garmin. Never raises.

    Two sources, in order of authority. The configured table is what the watch
    itself uses; the time-in-zone fallback carries the same boundaries but only
    for zones the athlete actually entered on that run, so a recovery jog yields
    Z1-Z2 alone. It is tried across several recent runs to widen that.
    """
    result: dict[str, Any] = {"zones": None, "source": None, "error": None, "lactate_threshold": None}
    try:
        from .garmin_client import get_garmin_client

        client = get_garmin_client()
    except Exception as exc:
        _log_exc("fetch_hr_zones_live[login]", exc)
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result

    # 1. The configured zone table. Not wrapped by garminconnect 0.3.x, so it
    #    goes through the generic GET wrapper.
    for path in ("/biometric-service/heartRateZones", "/biometric-service/heartRateZones/sport/RUNNING"):
        try:
            # Expected to 404 on accounts where the table is not exposed; the
            # fallback below is the working source there.
            with _quiet_garmin_logger():
                zones = _zones_from_payload(client.connectapi(path))
        except Exception as exc:
            _log_exc(f"fetch_hr_zones_live[{path}]", exc)
            continue
        if zones:
            result.update(zones=zones, source=f"garmin_profile ({path})")
            break

    # 2. Recover the boundaries from recent activities' time-in-zone payloads.
    if not result["zones"]:
        from .db import list_runs

        merged: dict[int, int] = {}
        for row in list_runs(limit=8):
            try:
                tiz = client.get_activity_hr_in_timezones(str(row["source_id"]))
            except Exception as exc:
                _log_exc("fetch_hr_zones_live[time_in_zones]", exc)
                continue
            for z in _zones_from_payload(tiz) or []:
                merged.setdefault(int(z["zone"]), int(z["low_bpm"]))
        if merged:
            result.update(
                zones=_zones_from_payload([{"zoneNumber": n, "zoneLowBoundary": v} for n, v in merged.items()]),
                source="garmin_activity_time_in_zones",
            )

    if not result["zones"] and not result["error"]:
        result["error"] = "Garmin returned no heart-rate zone configuration"

    # The lactate threshold is the physiological anchor for both zone systems, so
    # it rides along on the same login rather than costing a second one.
    try:
        lt = client.get_lactate_threshold(latest=True) or {}
        shr = (lt.get("speed_and_heart_rate") or {}) if isinstance(lt, dict) else {}
        # Garmin's `speed` here is SECONDS PER METRE despite the name, so the
        # pace is speed * 1000 — not 1000 / speed. Verified against this
        # account: 0.31666578 -> 5:17/km, which sits correctly between Garmin's
        # own 5K (5:04/km) and 10K (5:25/km) predictions, with the reported
        # threshold HR just under the zone-5 floor. Inverting it shipped a
        # confident "52:38/km".
        raw_speed = shr.get("speed")
        lt_pace = round(float(raw_speed) * 1000) if isinstance(raw_speed, (int, float)) and raw_speed > 0 else None
        # A threshold pace outside running range means Garmin changed the unit.
        # Drop it rather than pass it on: the coach quotes this number, and a
        # wrong one is worse than a missing one.
        if lt_pace is not None and not (_PACE_MIN_SEC <= lt_pace <= _PACE_MAX_SEC):
            print(
                f"[zones] discarding implausible lactate-threshold pace {lt_pace} sec/km "
                f"from speed={raw_speed!r} — Garmin's unit for this field may have changed",
                file=sys.stderr,
                flush=True,
            )
            lt_pace = None
        lt_hr = int(shr["heartRate"]) if isinstance(shr.get("heartRate"), (int, float)) else None
        if lt_pace or lt_hr:
            result["lactate_threshold"] = {
                "pace_sec_per_km": lt_pace,
                "pace": _pace_text(lt_pace),
                "speed_kmh": _speed_kmh(lt_pace),
                "hr_bpm": lt_hr,
                "measured_on": shr.get("calendarDate"),
            }
    except Exception as exc:
        _log_exc("fetch_hr_zones_live[lactate_threshold]", exc)

    return result


def hr_zones(fresh: bool = False, allow_fetch: bool = True) -> dict[str, Any]:
    """Cached HR zone table + lactate threshold. Never raises.

    A failed read is cached too, so a revoked Garmin token does not put a doomed
    login attempt in front of every request. The cached entry keeps `error` so
    the caller can say why zones are missing.

    `allow_fetch=False` returns whatever is cached and never touches the network.
    Interactive callers use it: a cold cache otherwise costs the zone endpoints,
    the lactate-threshold read and — on the fallback path — a time-in-zone call
    per recent activity, all while the athlete waits on a chat reply. The morning
    job primes the cache instead, where a slow Garmin call is free.
    """
    if not fresh:
        try:
            raw = get_config(_ZONE_CACHE_KEY)
            if raw:
                entry = json.loads(raw)
                age = (datetime.utcnow() - datetime.fromisoformat(entry["ts"])).total_seconds()
                if age < _ZONE_CACHE_TTL_SEC:
                    return {**entry["data"], "cached": True}
        except Exception as exc:
            _log_exc("hr_zones[cache_read]", exc)

    if not allow_fetch:
        return {
            "zones": None,
            "source": None,
            "lactate_threshold": None,
            "error": "no zone data cached yet (not fetched here to avoid blocking on Garmin)",
            "cached": False,
        }

    data = _fetch_hr_zones_live()
    try:
        set_config(_ZONE_CACHE_KEY, json.dumps({"ts": datetime.utcnow().isoformat(), "data": data}))
    except Exception as exc:
        _log_exc("hr_zones[cache_write]", exc)
    return {**data, "cached": False}


# --- Pace zones (plan targets) ----------------------------------------------


def pace_targets(paces: dict[str, Any], lt: dict[str, Any] | None = None) -> dict[str, Any]:
    """The plan's per-kind pace targets, said plainly as targets.

    `note` is part of the payload rather than prose the caller has to remember to
    add: these are single prescribed values, so there is no per-zone maximum to
    report, and a model asked for "max speed per zone" needs to be told that in
    the data rather than infer it.
    """
    out: dict[str, Any] = {
        "kind": "plan pace targets (single prescribed values, NOT zone ranges)",
        "targets": {
            kind: {"pace_sec_per_km": int(sec), "pace": _pace_text(sec), "speed_kmh": _speed_kmh(sec)}
            for kind, sec in (paces or {}).items()
            if isinstance(sec, (int, float)) and sec
        },
        "note": (
            "Each value is the single pace prescribed for that session kind, so it has no "
            "minimum or maximum. Report these as targets; for a measured range use "
            "observed_speed_by_hr_zone."
        ),
    }
    if lt:
        out["lactate_threshold"] = lt
    return out


# --- Observed speed per HR zone ---------------------------------------------


def _zone_of(hr: int, zones: list[dict[str, Any]]) -> int | None:
    for z in zones:
        low, high = z["low_bpm"], z["high_bpm"]
        if hr >= low and (high is None or hr <= high):
            return int(z["zone"])
    return None


def observed_by_hr_zone(
    zones: list[dict[str, Any]] | None,
    window_days: int = _OBSERVED_WINDOW_DAYS,
    today: date | None = None,
) -> dict[str, Any]:
    """Fastest and typical pace actually run in each HR zone, from stored laps.

    Returns a payload that always states its own basis — window, lap count and
    per-zone sample size — because a "max speed in Z4" drawn from two laps is a
    different claim from one drawn from forty, and the difference is invisible
    once the number is in prose.
    """
    coverage = lap_coverage()
    if not zones:
        return {
            "status": "unavailable",
            "reason": "no heart-rate zones known, so laps cannot be placed in a zone",
            "lap_coverage": coverage,
        }
    if not coverage["laps"]:
        return {
            "status": "unavailable",
            "reason": (
                "no lap splits stored yet — laps are saved from each new sync; "
                "POST /activities/laps/backfill imports them for past runs"
            ),
            "lap_coverage": coverage,
        }

    today = today or date.today()
    since = (today - timedelta(days=window_days)).isoformat()
    buckets: dict[int, list[dict[str, Any]]] = {}
    used = 0
    for lap in list_activity_laps(since=since):
        pace, hr = lap["pace_sec"], lap["avg_hr"]
        if not (_PACE_MIN_SEC <= pace <= _PACE_MAX_SEC):
            continue
        if float(lap["distance_km"] or 0) < _MIN_LAP_KM:
            continue
        z = _zone_of(int(hr), zones)
        if z is None:
            continue
        buckets.setdefault(z, []).append(
            {
                "pace_sec": int(pace),
                "hr": int(hr),
                "date": lap["activity_date"],
                "distance_km": round(float(lap["distance_km"] or 0), 2),
            }
        )
        used += 1

    per_zone = []
    for z in zones:
        num = int(z["zone"])
        laps = buckets.get(num, [])
        entry: dict[str, Any] = {
            "zone": num,
            "hr_range_bpm": f"{z['low_bpm']}-{z['high_bpm']}" if z["high_bpm"] else f"{z['low_bpm']}+",
            "laps": len(laps),
        }
        if laps:
            fastest = min(laps, key=lambda x: x["pace_sec"])
            paces = sorted(x["pace_sec"] for x in laps)
            median = paces[len(paces) // 2]
            entry.update(
                max_speed_kmh=_speed_kmh(fastest["pace_sec"]),
                fastest_pace=_pace_text(fastest["pace_sec"]),
                fastest_pace_sec_per_km=fastest["pace_sec"],
                fastest_on=fastest["date"],
                fastest_lap_km=fastest["distance_km"],
                typical_pace=_pace_text(median),
                typical_pace_sec_per_km=median,
                avg_hr_bpm=round(sum(x["hr"] for x in laps) / len(laps)),
            )
        else:
            entry["note"] = "no laps recorded in this zone in the window"
        per_zone.append(entry)

    return {
        "status": "ok",
        "basis": (
            f"per-lap averages from runs since {since} ({used} qualifying laps: "
            f">= {_MIN_LAP_KM} km, with both pace and HR). 'max_speed_kmh' is the fastest "
            "single lap averaged in that zone, not an instantaneous top speed."
        ),
        "window_days": window_days,
        "since": since,
        "qualifying_laps": used,
        "lap_coverage": coverage,
        "zones": per_zone,
    }


def zone_report(fresh: bool = False, today: date | None = None, allow_fetch: bool = True) -> dict[str, Any]:
    """Everything zone-related in one payload: HR bands, plan pace targets and
    observed per-zone speed. Used by the coach chat context and `GET /zones`.

    Pass `allow_fetch=False` from an interactive path — see `hr_zones`. The plan
    paces and the observed per-zone speeds are local reads either way, so they
    are still returned in full when the zone table is only cache-deep.
    """
    from .db import get_active_plan

    z = hr_zones(fresh=fresh, allow_fetch=allow_fetch)
    paces: dict[str, Any] = {}
    plan = get_active_plan()
    if plan is not None:
        try:
            paces = json.loads(plan["progression"]).get("paces", {}) or {}
        except Exception as exc:
            _log_exc("zone_report[plan_paces]", exc)
    return {
        "hr_zones": z["zones"],
        "hr_zones_source": z["source"],
        "hr_zones_error": z["error"],
        "pace_targets": pace_targets(paces, z.get("lactate_threshold")),
        "observed_speed_by_hr_zone": observed_by_hr_zone(z["zones"], today=today),
    }


# --- Run history ------------------------------------------------------------

# Distances a "personal best" is worth reporting over. A best is only claimed
# from a run that actually covered the distance — no extrapolating a 5K time from
# a 3 km run, which is the kind of invented number that makes an athlete plan
# around a pace they have never held.
_PB_DISTANCES = ((5.0, "5k"), (10.0, "10k"), (21.0975, "half_marathon"), (42.195, "marathon"))

# Tolerance on hitting a PB distance: a GPS 5K commonly records 4.98 km.
_PB_TOLERANCE_KM = 0.05


def run_history(runs: list[dict[str, Any]], today: date | None = None, recent: int = 12) -> dict[str, Any]:
    """What the athlete has actually run: recent sessions, volume, and bests.

    `runs` is `db.list_runs` output (newest first). This exists because the chat
    coach previously received none of it — the rows were loaded to score
    readiness and then dropped, so the coach had to ask the athlete for a recent
    5K time that was already on disk.
    """
    today = today or date.today()
    rows = []
    for r in runs:
        try:
            d = str(r["activity_date"])
            km = float(r["distance_km"] or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if km <= 0:
            continue
        rows.append(
            {
                "date": d,
                "distance_km": round(km, 2),
                "duration_sec": r.get("duration_seconds"),
                "pace_sec_per_km": r.get("avg_pace_sec_per_km"),
                "pace": _pace_text(r.get("avg_pace_sec_per_km")),
                "speed_kmh": _speed_kmh(r.get("avg_pace_sec_per_km")),
                "avg_hr": r.get("avg_hr"),
            }
        )

    def volume(days: int) -> float:
        cutoff = (today - timedelta(days=days)).isoformat()
        return round(sum(r["distance_km"] for r in rows if r["date"] >= cutoff), 1)

    # Best efforts, per distance, from runs that covered it. The pace is the
    # whole-run average, which for a run longer than the distance understates
    # what the athlete could do over the distance alone — said so in `note`.
    bests: dict[str, Any] = {}
    for dist_km, label in _PB_DISTANCES:
        eligible = [
            r for r in rows
            if r["distance_km"] >= dist_km - _PB_TOLERANCE_KM and r["pace_sec_per_km"]
            and _PACE_MIN_SEC <= r["pace_sec_per_km"] <= _PACE_MAX_SEC
        ]
        if not eligible:
            continue
        best = min(eligible, key=lambda r: r["pace_sec_per_km"])
        bests[label] = {
            "date": best["date"],
            "run_distance_km": best["distance_km"],
            "avg_pace": best["pace"],
            "avg_pace_sec_per_km": best["pace_sec_per_km"],
            "speed_kmh": best["speed_kmh"],
            "avg_hr": best["avg_hr"],
        }

    fastest = None
    paced = [r for r in rows if r["pace_sec_per_km"] and _PACE_MIN_SEC <= r["pace_sec_per_km"] <= _PACE_MAX_SEC]
    if paced:
        f = min(paced, key=lambda r: r["pace_sec_per_km"])
        fastest = {"date": f["date"], "distance_km": f["distance_km"], "pace": f["pace"], "speed_kmh": f["speed_kmh"], "avg_hr": f["avg_hr"]}

    return {
        "runs_stored": len(rows),
        "recent_runs": rows[:recent],
        "volume_km": {"last_7_days": volume(7), "last_28_days": volume(28)},
        "fastest_run_by_avg_pace": fastest,
        "best_efforts_by_distance": bests,
        "note": (
            "Every pace here is a WHOLE-RUN average, so it is not a time trial: a best "
            "effort over a distance the athlete ran as part of a longer session understates "
            "what they could do over that distance alone. For race-equivalent efforts use "
            "fitness.race_predictions; for speed at a given intensity use "
            "observed_speed_by_hr_zone."
        ),
    }
