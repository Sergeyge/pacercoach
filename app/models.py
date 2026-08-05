from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class RunActivity:
    source_id: str
    activity_date: date
    activity_type: str
    distance_km: float
    duration_seconds: int
    avg_hr: Optional[int] = None
    avg_pace_sec_per_km: Optional[int] = None
    calories: Optional[int] = None


@dataclass
class Readiness:
    score: int
    status: str
    reasons: list[str]
    weekly_distance_km: float
    four_week_avg_km: float
    acute_chronic_ratio: Optional[float]
    # True when verified recovery metrics for today fed the score. False means the
    # verdict reflects training load alone — either nothing had synced, or the
    # caller did not ask for metrics (e.g. GET /readiness without live=true).
    physiological: bool = False
