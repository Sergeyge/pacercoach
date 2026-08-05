from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .garmin_metrics import VERIFIED_SUMMARY_KEYS
from .models import Readiness


# Signals that may assert "this morning's recovery picture has landed". Presence
# of any one makes the verdict physiological, whether or not it produced a
# deduction — a normal night must not look like missing data, or the disclosure
# below tells the athlete their watch hasn't synced when it has.
#
# Derived from `garmin_metrics`, not hand-listed, so it cannot drift from the
# filter that produces the summary. `body_battery` is deliberately absent even
# though it is scored below: it trickles in all day, so a value carried over from
# before bed must not by itself assert that this morning landed. Since callers
# pre-filter (see `calculate_readiness`), it only ever appears alongside a
# verified signal.
_VERIFIABLE_SIGNALS = VERIFIED_SUMMARY_KEYS


def _physiological_adjust(m: dict[str, Any]) -> tuple[int, list[str], bool]:
    """Score delta, explanations, and whether any known signal was present.

    Garmin's own `training_readiness` already folds in sleep, HRV, stress and
    body battery, so when it is present it is the primary signal and only the
    HRV *status* trend is applied on top — that one compares last night against
    your multi-week baseline, which the daily score underweights. Without a
    training-readiness score we compose an equivalent verdict from the individual
    metrics instead. Applying every metric independently would stack four
    overlapping penalties for a single bad night and manufacture false reds.
    """
    delta = 0
    reasons: list[str] = []
    used = any(m.get(k) is not None for k in _VERIFIABLE_SIGNALS)
    tr = m.get("training_readiness")

    if isinstance(tr, (int, float)):
        score = int(tr)
        if score < 25:
            delta -= 30
            reasons.append(f"Garmin training readiness very low ({score})")
        elif score < 40:
            delta -= 20
            reasons.append(f"Garmin training readiness low ({score})")
        elif score < 55:
            delta -= 10
            reasons.append(f"Garmin training readiness below par ({score})")
        elif score < 80:
            reasons.append(f"Garmin training readiness moderate ({score})")
        else:
            delta += 5
            reasons.append(f"Garmin training readiness strong ({score})")
    else:
        sleep_score = m.get("sleep_score")
        hours = m.get("sleep_hours")
        battery = m.get("body_battery")
        if isinstance(sleep_score, (int, float)):
            if sleep_score < 50:
                delta -= 15
                reasons.append(f"poor sleep score ({int(sleep_score)})")
            elif sleep_score < 65:
                delta -= 8
                reasons.append(f"mediocre sleep score ({int(sleep_score)})")
        if isinstance(hours, (int, float)):
            if hours < 5:
                delta -= 10
                reasons.append(f"only {hours:g}h sleep")
            elif hours < 6:
                delta -= 5
                reasons.append(f"short sleep ({hours:g}h)")
        if isinstance(battery, (int, float)):
            if battery < 30:
                delta -= 10
                reasons.append(f"body battery low at wake ({int(battery)})")
            elif battery < 50:
                delta -= 5
                reasons.append(f"body battery moderate at wake ({int(battery)})")

    hrv = str(m.get("hrv_status") or "").upper()
    if hrv == "UNBALANCED":
        delta -= 10
        reasons.append("HRV unbalanced vs your baseline")
    elif hrv in ("LOW", "POOR"):
        delta -= 20
        reasons.append(f"HRV {hrv.lower()} vs your baseline")

    if used and not reasons:
        reasons.append("recovery metrics look normal")
    return delta, reasons, used


def calculate_readiness(
    runs: list[dict],
    today: date | None = None,
    metrics: dict[str, Any] | None = None,
) -> Readiness:
    """Today's readiness from training load and, when available, this morning's
    recovery metrics.

    `metrics` is the compact recovery summary produced by `garmin_metrics` (keys
    as in `_VERIFIABLE_SIGNALS` plus `body_battery`). Passing it is what makes the
    verdict physiological: without it the score reflects only how much you have
    been running, so a bad night cannot produce yellow or red.

    Callers MUST pass a summary from which stale and unverified metrics have
    already been dropped (`daily_coach.morning_metrics` does this). This function
    has no way to tell yesterday's sleep score from today's, so scoring an
    unfiltered summary would let a two-day-old bad night cancel today's session.
    """
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

    physiological = False
    if metrics:
        delta, metric_reasons, physiological = _physiological_adjust(metrics)
        score += delta
        reasons.extend(metric_reasons)

    status = "green"
    if score < 55:
        status = "red"
    elif score < 70:
        status = "yellow"

    if not reasons:
        reasons.append("load looks stable")
    if not physiological:
        reasons.append("training load only — no recovery metrics available")

    return Readiness(
        score=max(0, min(100, score)),
        status=status,
        reasons=reasons,
        weekly_distance_km=round(weekly, 1),
        four_week_avg_km=round(four_week_avg, 1),
        acute_chronic_ratio=round(ratio, 2) if ratio is not None else None,
        physiological=physiological,
    )
