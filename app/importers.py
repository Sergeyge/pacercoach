from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime
from typing import Optional

import pandas as pd

from .models import RunActivity


def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    normalized = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in df.columns}
    for candidate in candidates:
        key = re.sub(r"[^a-z0-9]", "", candidate.lower())
        if key in normalized:
            return normalized[key]
    return None


def _parse_duration(value) -> int:
    if pd.isna(value):
        return 0
    s = str(value).strip()
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(float(h)) * 3600 + int(float(m)) * 60 + int(float(sec))
        if len(parts) == 2:
            m, sec = parts
            return int(float(m)) * 60 + int(float(sec))
        return int(float(s))
    except ValueError:
        return 0


def _parse_pace(value) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    seconds = _parse_duration(value)
    return seconds or None


def _parse_date(value):
    if pd.isna(value):
        raise ValueError("missing date")
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid date: {value}")
    return parsed.date()


def _num(value) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in {"", ".", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_garmin_csv(content: bytes, distance_unit: str = "km") -> list[RunActivity]:
    df = pd.read_csv(io.BytesIO(content))

    date_col = _find_col(df, ["Date", "Start Time", "Begin Timestamp", "Activity Date"])
    type_col = _find_col(df, ["Activity Type", "Type", "Sport"])
    distance_col = _find_col(df, ["Distance", "Distance (km)", "Distance(km)"])
    time_col = _find_col(df, ["Time", "Elapsed Time", "Duration"])
    avg_hr_col = _find_col(df, ["Avg HR", "Average HR", "Avg Heart Rate", "Average Heart Rate"])
    pace_col = _find_col(df, ["Avg Pace", "Average Pace", "Pace"])
    calories_col = _find_col(df, ["Calories"])
    id_col = _find_col(df, ["Activity ID", "ActivityId", "ID"])

    required = [date_col, type_col, distance_col, time_col]
    if any(c is None for c in required):
        raise ValueError(
            f"Missing required columns. Found: {list(df.columns)}. Required: Date, Activity Type, Distance, Time."
        )

    activities: list[RunActivity] = []
    for _, row in df.iterrows():
        activity_type = str(row[type_col]).strip()
        if "run" not in activity_type.lower():
            continue

        distance = _num(row[distance_col]) or 0.0
        if distance_unit.lower() in {"mile", "miles", "mi"}:
            distance *= 1.609344

        duration = _parse_duration(row[time_col])
        if distance <= 0 or duration <= 0:
            continue

        activity_date = _parse_date(row[date_col])
        raw_id = str(row[id_col]).strip() if id_col else ""
        source_id = raw_id or hashlib.sha1(
            f"{activity_date}-{activity_type}-{distance}-{duration}".encode()
        ).hexdigest()

        activities.append(
            RunActivity(
                source_id=source_id,
                activity_date=activity_date,
                activity_type=activity_type,
                distance_km=round(distance, 3),
                duration_seconds=duration,
                avg_hr=int(_num(row[avg_hr_col])) if avg_hr_col and _num(row[avg_hr_col]) else None,
                avg_pace_sec_per_km=_parse_pace(row[pace_col]) if pace_col else None,
                calories=int(_num(row[calories_col])) if calories_col and _num(row[calories_col]) else None,
            )
        )
    return activities
