from __future__ import annotations

from datetime import date, timedelta

from .models import Readiness


def calculate_readiness(runs: list[dict], today: date | None = None) -> Readiness:
    today = today or date.today()
    last_7 = today - timedelta(days=7)
    last_28 = today - timedelta(days=28)

    weekly = sum(r["distance_km"] for r in runs if date.fromisoformat(r["activity_date"]) >= last_7)
    four_week_total = sum(r["distance_km"] for r in runs if date.fromisoformat(r["activity_date"]) >= last_28)
    four_week_avg = four_week_total / 4 if four_week_total else 0

    ratio = weekly / four_week_avg if four_week_avg > 0 else None
    score = 80
    reasons: list[str] = []

    if ratio is not None:
        if ratio > 1.35:
            score -= 25
            reasons.append("weekly load is much higher than recent baseline")
        elif ratio > 1.15:
            score -= 10
            reasons.append("weekly load is moderately higher than recent baseline")
        elif ratio < 0.6 and four_week_avg > 10:
            score -= 5
            reasons.append("recent load is low; rebuild gradually")

    if weekly == 0:
        score -= 10
        reasons.append("no running activity in the last 7 days")

    status = "green"
    if score < 55:
        status = "red"
    elif score < 70:
        status = "yellow"

    if not reasons:
        reasons.append("load looks stable")

    return Readiness(
        score=max(0, min(100, score)),
        status=status,
        reasons=reasons,
        weekly_distance_km=round(weekly, 1),
        four_week_avg_km=round(four_week_avg, 1),
        acute_chronic_ratio=round(ratio, 2) if ratio is not None else None,
    )
