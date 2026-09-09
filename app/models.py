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
    # The two contributions to `score`, kept apart so a consumer can tell a
    # volume spike from a body that needs a break. Both are signed deltas off
    # the base of 80: `load_delta` from the acute:chronic ratio, and
    # `physiological_delta` from this morning's recovery metrics (0 when none
    # were scored — check `physiological` before reading it as "recovery is
    # fine", since "no deduction" and "no data" both come through as 0).
    #
    # `daily_coach._rule_adjust` needs this because the two warrant different
    # responses: too much volume this week means run less, while poor recovery
    # means run easier. Without the split, a yellow caused purely by load also
    # stripped the session's intensity.
    load_delta: int = 0
    physiological_delta: int = 0
