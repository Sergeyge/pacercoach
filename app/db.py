from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .models import RunActivity

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "running_assistant.db"


def _utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                source_id TEXT PRIMARY KEY,
                activity_date TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                distance_km REAL NOT NULL,
                duration_seconds INTEGER NOT NULL,
                avg_hr INTEGER,
                avg_pace_sec_per_km INTEGER,
                calories INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                imported_or_updated INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                distance_km REAL NOT NULL,
                target_seconds INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS training_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                base_weekly_km REAL NOT NULL,
                weekly_template TEXT NOT NULL,
                progression TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS planned_workout (
                plan_date TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                distance_km REAL NOT NULL,
                target_pace_sec INTEGER,
                details TEXT,
                source TEXT NOT NULL DEFAULT 'rule',
                coach_note TEXT,
                status TEXT NOT NULL DEFAULT 'planned',
                actual_source_id TEXT,
                actual_distance_km REAL,
                actual_pace_sec INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress_snapshot (
                snap_date TEXT PRIMARY KEY,
                distance_km REAL NOT NULL,
                predicted_seconds INTEGER NOT NULL,
                target_seconds INTEGER NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_analysis (
                source_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                sent_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coach_message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Migrations for columns added after initial release. Each ALTER is
        # wrapped in its own try because SQLite has no "ADD COLUMN IF NOT EXISTS".
        for stmt in (
            "ALTER TABLE planned_workout ADD COLUMN garmin_workout_id TEXT",
            "ALTER TABLE goal ADD COLUMN paused_at TEXT",
            "ALTER TABLE goal ADD COLUMN pause_reason TEXT",
            "ALTER TABLE goal ADD COLUMN pause_until TEXT",
            "ALTER TABLE goal ADD COLUMN race_date TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()


def upsert_activities(activities: Iterable[RunActivity]) -> int:
    rows = list(activities)
    if not rows:
        return 0
    with get_conn() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO activities (
                source_id, activity_date, activity_type, distance_km,
                duration_seconds, avg_hr, avg_pace_sec_per_km, calories
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                activity_date=excluded.activity_date,
                activity_type=excluded.activity_type,
                distance_km=excluded.distance_km,
                duration_seconds=excluded.duration_seconds,
                avg_hr=excluded.avg_hr,
                avg_pace_sec_per_km=excluded.avg_pace_sec_per_km,
                calories=excluded.calories
            """,
            [
                (
                    r.source_id,
                    r.activity_date.isoformat(),
                    r.activity_type,
                    r.distance_km,
                    r.duration_seconds,
                    r.avg_hr,
                    r.avg_pace_sec_per_km,
                    r.calories,
                )
                for r in rows
            ],
        )
        conn.commit()
        return conn.total_changes - before


def list_runs(limit: int = 200) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM activities
            WHERE lower(activity_type) LIKE '%run%'
            ORDER BY activity_date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def add_sync_log(status: str, message: str, imported_or_updated: int = 0) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_log (synced_at, status, message, imported_or_updated) VALUES (?, ?, ?, ?)",
            (_utcnow(), status, message, imported_or_updated),
        )
        conn.commit()


def list_sync_log(limit: int = 20) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


# --- Goal-driven training plan ---------------------------------------------


def set_active_goal(distance_km: float, target_seconds: int, race_date: str | None = None) -> int:
    with get_conn() as conn:
        conn.execute("UPDATE goal SET active = 0 WHERE active = 1")
        cur = conn.execute(
            "INSERT INTO goal (distance_km, target_seconds, active, created_at, race_date) VALUES (?, ?, 1, ?, ?)",
            (distance_km, target_seconds, _utcnow(), race_date),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_active_goal() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM goal WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def deactivate_goals() -> None:
    with get_conn() as conn:
        conn.execute("UPDATE goal SET active = 0 WHERE active = 1")
        conn.commit()


def save_plan(goal_id: int, base_weekly_km: float, weekly_template: dict, progression: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO training_plan (goal_id, base_weekly_km, weekly_template, progression, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (goal_id, base_weekly_km, json.dumps(weekly_template), json.dumps(progression), _utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_active_plan() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT tp.* FROM training_plan tp
               JOIN goal g ON g.id = tp.goal_id
               WHERE g.active = 1
               ORDER BY tp.id DESC LIMIT 1"""
        ).fetchone()


def upsert_planned_workout(
    plan_date: str,
    kind: str,
    distance_km: float,
    target_pace_sec: int | None,
    details: str | None,
    source: str = "rule",
    coach_note: str | None = None,
    status: str = "planned",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO planned_workout
                 (plan_date, kind, distance_km, target_pace_sec, details, source, coach_note, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(plan_date) DO UPDATE SET
                 kind=excluded.kind, distance_km=excluded.distance_km,
                 target_pace_sec=excluded.target_pace_sec, details=excluded.details,
                 source=excluded.source, coach_note=excluded.coach_note, status=excluded.status""",
            (plan_date, kind, distance_km, target_pace_sec, details, source, coach_note, status),
        )
        conn.commit()


def get_planned_workout(plan_date: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM planned_workout WHERE plan_date = ?", (plan_date,)
        ).fetchone()


def list_planned_workouts(start_date: str, end_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM planned_workout WHERE plan_date >= ? AND plan_date <= ? ORDER BY plan_date",
            (start_date, end_date),
        ).fetchall()


def delete_planned_from(start_date: str) -> list[str]:
    """Drop planned rows from `start_date` forward so a plan can be (re)materialized.

    Only rows whose status is still `'planned'` are dropped — `'completed'` and
    `'missed'` rows survive so history isn't lost. Source is not inspected, so
    an unfinished adapted day for the old goal is dropped alongside rule days.

    Returns the list of `garmin_workout_id` values that were on the dropped
    rows. Callers should use these to also delete the matching workouts from
    Garmin's calendar — otherwise a goal change leaves orphan workouts on the
    user's watch with no DB pointer back to them, and the next push for those
    dates creates duplicates.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT garmin_workout_id FROM planned_workout "
            "WHERE plan_date >= ? AND status = 'planned' AND garmin_workout_id IS NOT NULL",
            (start_date,),
        ).fetchall()
        ids = [r["garmin_workout_id"] for r in rows if r["garmin_workout_id"]]
        conn.execute(
            "DELETE FROM planned_workout WHERE plan_date >= ? AND status = 'planned'",
            (start_date,),
        )
        conn.commit()
        return ids


def link_actuals(today: date | None = None) -> None:
    """Match planned running days to stored Garmin activities by date.

    Completed runs are linked with their actual distance/pace; past non-rest
    days with no matching activity are flagged 'missed'.
    """
    today = today or date.today()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM planned_workout").fetchall()
        for r in rows:
            if r["kind"] == "rest":
                continue
            act = conn.execute(
                """SELECT * FROM activities
                   WHERE activity_date = ? AND lower(activity_type) LIKE '%run%'
                   ORDER BY distance_km DESC LIMIT 1""",
                (r["plan_date"],),
            ).fetchone()
            if act:
                conn.execute(
                    """UPDATE planned_workout
                       SET status='completed', actual_source_id=?, actual_distance_km=?, actual_pace_sec=?
                       WHERE plan_date=?""",
                    (act["source_id"], act["distance_km"], act["avg_pace_sec_per_km"], r["plan_date"]),
                )
            elif r["plan_date"] < today.isoformat() and r["status"] == "planned":
                conn.execute(
                    "UPDATE planned_workout SET status='missed' WHERE plan_date=?",
                    (r["plan_date"],),
                )
        conn.commit()


def upsert_progress_snapshot(snap_date: str, distance_km: float, predicted_seconds: int, target_seconds: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO progress_snapshot (snap_date, distance_km, predicted_seconds, target_seconds, recorded_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(snap_date) DO UPDATE SET
                 distance_km=excluded.distance_km, predicted_seconds=excluded.predicted_seconds,
                 target_seconds=excluded.target_seconds, recorded_at=excluded.recorded_at""",
            (snap_date, distance_km, int(predicted_seconds), int(target_seconds), _utcnow()),
        )
        conn.commit()


def list_progress_snapshots(start_date: str, end_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM progress_snapshot WHERE snap_date >= ? AND snap_date <= ? ORDER BY snap_date",
            (start_date, end_date),
        ).fetchall()


def get_config(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def mark_garmin_pushed(plan_date: str, workout_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE planned_workout SET garmin_workout_id=? WHERE plan_date=?",
            (str(workout_id), plan_date),
        )
        conn.commit()


def clear_garmin_pushed(plan_date: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE planned_workout SET garmin_workout_id=NULL WHERE plan_date=?",
            (plan_date,),
        )
        conn.commit()


def save_activity_analysis(source_id: str, summary: str, sent_at: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO activity_analysis (source_id, summary, sent_at, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 summary=excluded.summary, sent_at=excluded.sent_at""",
            (source_id, summary, sent_at, _utcnow()),
        )
        conn.commit()


def list_unanalyzed_running_activities(limit: int = 5) -> list[sqlite3.Row]:
    # Oldest first so a backlog (e.g. on first install of a 45-day sync) fills
    # forward rather than starving older runs that never get reached.
    with get_conn() as conn:
        return conn.execute(
            """SELECT a.* FROM activities a
               LEFT JOIN activity_analysis x ON x.source_id = a.source_id
               WHERE x.source_id IS NULL AND lower(a.activity_type) LIKE '%run%'
               ORDER BY a.activity_date ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def get_activity_analysis(source_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM activity_analysis WHERE source_id=?", (source_id,)
        ).fetchone()


def update_activity_sent_at(source_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE activity_analysis SET sent_at=? WHERE source_id=?",
            (_utcnow(), source_id),
        )
        conn.commit()


def get_latest_analysis() -> sqlite3.Row | None:
    # Order by the run's date, not the analysis timestamp — otherwise a backlog
    # fill that processes older unanalyzed runs after newer ones would surface
    # a stale activity as the "latest" review on the dashboard.
    with get_conn() as conn:
        return conn.execute(
            """SELECT a.activity_date, a.distance_km, a.avg_pace_sec_per_km, a.avg_hr,
                      x.summary, x.sent_at, x.created_at, x.source_id
               FROM activity_analysis x JOIN activities a ON a.source_id = x.source_id
               ORDER BY a.activity_date DESC, x.created_at DESC LIMIT 1"""
        ).fetchone()


# --- Coach chat history (persistent across days, trimmed to last N) ---


def add_coach_message(role: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO coach_message (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, _utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_recent_coach_messages(limit: int = 50) -> list[sqlite3.Row]:
    """Return the most recent N coach messages in chronological order (oldest
    first) — that's the order an OpenAI chat completion expects."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM coach_message ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed(rows))


def trim_coach_messages(keep: int = 50) -> int:
    """Delete all but the most recent `keep` messages. Returns rows removed."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM coach_message WHERE id NOT IN "
            "(SELECT id FROM coach_message ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        conn.commit()
        return cur.rowcount or 0


def clear_coach_messages() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM coach_message")
        conn.commit()


# --- Plan pause / resume ---


def pause_active_goal(reason: str | None, until: str | None) -> bool:
    """Mark the active goal as paused. `until` is an optional ISO date — the
    morning job auto-resumes once that date is past."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE goal SET paused_at=?, pause_reason=?, pause_until=? WHERE active=1",
            (_utcnow(), reason, until),
        )
        conn.commit()
        return cur.rowcount > 0


def resume_active_goal() -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE goal SET paused_at=NULL, pause_reason=NULL, pause_until=NULL WHERE active=1",
        )
        conn.commit()
        return cur.rowcount > 0


def mark_planned_paused_from(start_date: str) -> int:
    """Flag all status='planned' workouts from start_date forward as
    status='paused'. Returns the number of rows marked. Used by /goal/pause
    so the dashboard renders the rest of the week as paused, and the morning
    job doesn't accidentally pick a paused day to adapt/push."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE planned_workout SET status='paused' "
            "WHERE plan_date >= ? AND status='planned'",
            (start_date,),
        )
        conn.commit()
        return cur.rowcount or 0


def clear_paused_planned() -> int:
    """Delete all status='paused' workouts (used on resume before
    re-materializing the plan with a shifted start_date)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM planned_workout WHERE status='paused'")
        conn.commit()
        return cur.rowcount or 0


def shift_active_plan_start(days: int) -> str | None:
    """Push the active training plan's progression.start_date forward by
    `days` days so the weekly periodization continues from where it paused
    rather than where the calendar marched on. Returns the new start_date
    string or None if there's no active plan."""
    from datetime import date as _date, timedelta
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tp.id, tp.progression FROM training_plan tp "
            "JOIN goal g ON g.id = tp.goal_id "
            "WHERE g.active = 1 ORDER BY tp.id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        prog = json.loads(row["progression"])
        old_start_str = prog.get("start_date")
        if not old_start_str:
            return None
        try:
            old_start = _date.fromisoformat(old_start_str)
        except ValueError:
            return None
        new_start = (old_start + timedelta(days=int(days))).isoformat()
        prog["start_date"] = new_start
        conn.execute(
            "UPDATE training_plan SET progression=? WHERE id=?",
            (json.dumps(prog), row["id"]),
        )
        conn.commit()
        return new_start
