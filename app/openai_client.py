from __future__ import annotations

import json
import sys
from typing import Any

from .settings import settings


def _log_exc(where: str, exc: BaseException) -> None:
    print(f"[openai_client.{where}] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

_SYSTEM_PROMPT = (
    "You are an expert running coach adjusting a single day's workout for an athlete "
    "training toward a goal. You are given the rule-based plan for today, the athlete's "
    "recent completed-vs-planned results, and this morning's recovery metrics. "
    "Produce the final workout for today.\n"
    "Rules you MUST follow:\n"
    "- Respect the provided safety bounds: 'distance_km' must be between "
    "'min_distance_km' and 'max_distance_km', and 'kind' must be one of "
    "'allowed_kinds'.\n"
    "- When 'allowed_kinds' holds a single kind, today's session type is fixed: "
    "keep it, and note that 'min_distance_km' is above zero, so you cannot cancel "
    "the session by setting the distance to 0. If the athlete genuinely should not "
    "train today, say so in 'coach_note' rather than zeroing the distance.\n"
    "- For 'target_pace_sec', echo 'rule_suggestion.target_pace_sec' — NOT the value "
    "in 'plan_paces_sec_per_km' for the kind, which is wrong for a base-phase "
    "quality day (those run at easy pace). Whatever you send is replaced by the "
    "plan's pace for the kind you chose, and sending a FASTER pace is treated as an "
    "attempt to harden the session and discards your wording.\n"
    "- Never prescribe a harder session than the rules did: when readiness has "
    "already eased today, you may ease it further (down to rest) but not restore "
    "what was removed. 'allowed_kinds' is the authority — any kind listed there is "
    "permitted, including one that looks harder (a missed long run may be offered "
    "on an easy day).\n"
    "- Judge today's session by 'today_planned.details' (the actual prescription) "
    "and 'today_planned.is_hard_session', NOT by the 'kind' label. A base-phase "
    "quality day is an easy-pace run finished with short relaxed strides: it is "
    "not a hard session, so never demote it to a plain easy run just for being "
    "'quality'. If you keep the session, keep its prescription in 'details' — "
    "do not drop prescribed strides or intervals from the description.\n"
    "- 'recent_results[].actual_pace_sec' is the WHOLE activity's average pace. On a day "
    "carrying a 'structure' (tempo/intervals/sharpener) it blends the work reps with the "
    "warm-up, jog recoveries and cool-down, so it is far slower than that day's rep "
    "target even when every rep was nailed. Never read it as a missed pace, and never "
    "ease today on that basis.\n"
    "- If recovery is poor, reduce volume/intensity or prescribe rest. If the athlete is "
    "fresh and on track, keep the planned work.\n"
    "- 'readiness' splits the score into 'load_delta' (from the acute:chronic volume ratio) "
    "and 'physiological_delta' (from this morning's recovery metrics). They mean different "
    "things. A negative load_delta with a non-negative physiological_delta is a VOLUME spike "
    "on a well-recovered body: run less, not easier, and never describe it as poor recovery. "
    "A negative physiological_delta is the body itself — that is when intensity comes down.\n"
    "- Keep changes conservative; never increase load sharply.\n"
    "- Respect 'long_term_plan' (the periodized roadmap position: phase, week, weeks to "
    "race). Base phase = mostly easy aerobic work, no hard sessions. Build/peak = protect "
    "the key quality and long sessions when readiness allows. Taper = NEVER add volume or "
    "intensity — freshness beats fitness this close to the race.\n"
    "- You may ONLY change today's workout, but use 'week_ahead' (the upcoming planned "
    "days) and 'this_week' (weekly target vs planned vs completed km) to keep the week "
    "balanced: stay light the day before the long run, and never cram missed volume into "
    "a single day.\n"
    "- If you change 'kind', take the matching pace from 'plan_paces_sec_per_km'.\n"
    "Respond with ONLY a JSON object: {\"kind\": str, \"distance_km\": number, "
    "\"target_pace_sec\": int|null, \"details\": str, \"coach_note\": str}. "
    "'coach_note' is one short sentence explaining the adjustment."
)

_COACH_CHAT_SYSTEM = (
    "You are the athlete's personal running coach. Answer their question briefly and "
    "practically (2-4 sentences) using the provided context: their goal, the long-term "
    "phased roadmap position ('long_term_plan': phase base/build/peak/taper, week, weeks "
    "to race), today's planned workout, the upcoming week, recent results, and readiness. "
    "Keep advice and proposed changes consistent with the current phase (e.g. never add "
    "volume during taper). Be encouraging but honest. "
    "Do not invent data you weren't given; if you can't answer from the context, say so.\n\n"
    "ZONES, PACES AND SPEED — you are given all of this, so answer from it rather than "
    "asking the athlete for numbers:\n"
    "- 'hr_zones': the athlete's own Garmin heart-rate zones as {zone, low_bpm, high_bpm} "
    "(high_bpm null on the top zone = open-ended). If it is null, 'hr_zones_error' says "
    "why — say that plainly and never substitute a percentage-of-max estimate.\n"
    "- 'pace_targets': the plan's prescribed pace PER SESSION KIND. These are single "
    "target values, NOT ranges, so they have no minimum or maximum — never present them "
    "as zone bands or invent boundaries around them. 'lactate_threshold' is the one "
    "measured pace/HR anchor when Garmin has established it.\n"
    "- 'observed_speed_by_hr_zone': the speed the athlete has ACTUALLY run in each HR "
    "zone, from per-lap data. This is the only field that answers a 'max speed per zone' "
    "question. 'max_speed_kmh'/'fastest_pace' is the fastest single lap AVERAGED in that "
    "zone — not an instantaneous top speed — and 'laps' is the sample size: with 0 laps "
    "say the zone has no data, and with only 1-2 be explicit that it is thin evidence. "
    "Respect 'basis' and report the window. If 'status' is 'unavailable', give the "
    "'reason' rather than falling back to whole-run averages.\n"
    "- 'run_history': recent runs with pace and HR, 7/28-day volume, and best efforts by "
    "distance. Every pace there is a WHOLE-RUN average, so it understates what the "
    "athlete could do over a shorter distance — never call one a time trial or a PB "
    "unless the run's distance matches.\n"
    "- 'fitness': VO2max, training status and 'race_predictions' (Garmin's predicted 5K/"
    "10K/half/marathon times with paces). These are the race-equivalent efforts — use "
    "them when asked how fast the athlete could race, and label them as predictions.\n"
    "- 'morning_metrics' / 'morning_metrics_freshness': this morning's recovery data. "
    "When 'readiness.from_recovery_metrics' is false, the readiness verdict reflects "
    "training load ONLY — say so instead of implying you know how they slept.\n"
    "- Asked WHY a session was changed, answer from 'readiness.load_delta' vs "
    "'readiness.physiological_delta' instead of guessing. A negative load_delta with a "
    "non-negative physiological_delta means the volume ratio ('weekly_km' against "
    "'chronic_baseline_km', giving 'acute_chronic_ratio') caused it while recovery was fine — "
    "say that plainly and quote the two volumes. Never tell the athlete they were "
    "under-recovered when their metrics were good.\n\n"
    "You can ALSO change the training plan when the athlete asks you to (e.g. 'make today "
    "easier', 'move my long run to Sunday', 'I need a rest day today', 'shorten Friday', "
    "'make today 12 km', 'run tomorrow at 5:10/km'). When — and only when — the athlete is "
    "clearly asking to change a specific day, include a `proposed_change` describing it. "
    "Otherwise set proposed_change to null.\n\n"
    "Allowed actions:\n"
    "- adjust_day: change one day's kind, distance and/or pace. Fields: date (YYYY-MM-DD), "
    "kind (one of rest|recovery|easy|long|quality), distance_km (number; 0 for rest), "
    "target_pace_sec (integer seconds per km). Send ONLY the fields the athlete asked to "
    "change and omit the rest (null) — an omitted field keeps that day's current value, so "
    "a pace-only request carries target_pace_sec alone and leaves the distance where it is.\n"
    "- rest_day: turn one day into rest. Fields: date.\n"
    "- swap_days: swap the workouts of two dates. Fields: date, date2.\n"
    "- follow_plan: hand a day back to the automatic plan, undoing an earlier chat change "
    "and letting the morning adaptation manage it again ('put Thursday back on plan', "
    "'stop keeping today fixed'). Fields: date.\n\n"
    "Pace is ALWAYS in seconds per kilometre: '5:10/km' is 310, '4:45' is 285. Convert "
    "before sending; never send a mm:ss string.\n\n"
    "THE ATHLETE HAS THE FINAL WORD. When they ask for a specific distance or pace, propose "
    "exactly the number they asked for. Do NOT substitute a more conservative one, do not "
    "round it toward the plan, and do not withhold the change because readiness, the phase "
    "or the weekly total argues against it. If you disagree, say so plainly in `answer` in "
    "one sentence AND still propose what they asked for — the decision is theirs to make "
    "with your opinion in hand, not yours to make for them. 'Keep changes conservative' "
    "governs changes YOU initiate, never an explicit instruction from the athlete.\n"
    "A day changed this way becomes athlete-set: the morning adaptation will then leave it "
    "exactly as asked instead of trimming it for readiness, and will only add a readiness "
    "note. Mention this when it matters (e.g. when readiness is poor), and use follow_plan "
    "when the athlete wants the day managed automatically again.\n"
    "Only propose changes to dates within the upcoming plan you were given. Always describe "
    "the change in `summary` in one short sentence — include the distance and pace when they "
    "are part of it — and explain it in `answer` too.\n\n"
    "Respond with ONLY a JSON object: {\"answer\": str, \"proposed_change\": null | "
    "{\"action\": str, \"date\": str, \"date2\": str|null, \"kind\": str|null, "
    "\"distance_km\": number|null, \"target_pace_sec\": int|null, \"summary\": str}}."
)


def current_model() -> str:
    """Active model — runtime override (DB) falling back to the env default."""
    from .db import get_config

    return get_config("openai_model", settings.openai_model) or settings.openai_model


def _client():
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    return OpenAI(api_key=settings.openai_api_key)


def _create(client, model: str, messages: list, json_mode: bool = False, max_out: int = 400, temperature: float = 0.3):
    """Call chat.completions, tolerating parameter differences across model families.

    GPT-5 / o-series models use 'max_completion_tokens' (not 'max_tokens') and may only
    accept the default temperature, so we retry with progressively simpler params on
    parameter-related errors (and re-raise anything else)."""
    base: dict[str, Any] = {"model": model, "messages": messages}
    if json_mode:
        base["response_format"] = {"type": "json_object"}
    variants = [
        {"max_tokens": max_out, "temperature": temperature},
        {"max_completion_tokens": max_out, "temperature": temperature},
        {"max_completion_tokens": max_out},
        {},
    ]
    err: Exception | None = None
    for extra in variants:
        try:
            return client.chat.completions.create(**base, **extra)
        except Exception as exc:  # noqa: BLE001
            err = exc
            msg = str(exc).lower()
            if not any(t in msg for t in ("max_tokens", "max_completion", "temperature", "unsupported", "not supported", "parameter", "invalid_request")):
                raise
    raise err  # type: ignore[misc]


def coach_adjust(context: dict[str, Any]) -> dict[str, Any] | None:
    """Today's adjusted workout as JSON. None on any failure (caller falls back to rules)."""
    client = _client()
    if client is None:
        return None
    try:
        resp = _create(
            client,
            current_model(),
            [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(context, default=str)}],
            json_mode=True,
            max_out=2000,  # reasoning models (GPT-5/o-series) spend tokens thinking before output
            temperature=0.3,
        )
        content = resp.choices[0].message.content
        if not content or not content.strip():
            return None
        return json.loads(content)
    except Exception as exc:
        # Caller falls back to rule-based adapter — log so a permanent OpenAI
        # breakage (revoked key, removed model, quota exhausted) doesn't look
        # like "engine: rules-only is just the normal mode".
        _log_exc("coach_adjust", exc)
        return None


def coach_answer(
    context: dict[str, Any],
    question: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Coaching answer, optionally with a proposed plan change.

    `history` is the prior chat thread as a list of `{role, content}` dicts in
    chronological order (oldest first). The system prompt and the freshly-built
    `context` are sent on every turn so the coach always sees current state;
    older turns carry plain text so it can refer back to "what we discussed".

    Returns `{"answer": str, "proposed_change": dict|None}` or None on failure.
    The proposed change is NOT applied here — the caller validates and applies it
    only after the athlete confirms.
    """
    client = _client()
    if client is None:
        return None
    try:
        system_with_context = (
            _COACH_CHAT_SYSTEM
            + "\n\nCurrent athlete context (refreshed every turn):\n"
            + json.dumps(context, default=str)
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system_with_context}]
        for m in history or []:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})
        resp = _create(
            client,
            current_model(),
            messages,
            json_mode=True,
            max_out=1500,  # leave room for reasoning models to think + answer
            temperature=0.4,
        )
        content = resp.choices[0].message.content
        if not content or not content.strip():
            return None
        data = json.loads(content)
        answer = data.get("answer")
        if not answer or not str(answer).strip():
            return None
        pc = data.get("proposed_change")
        return {"answer": str(answer).strip(), "proposed_change": pc if isinstance(pc, dict) else None}
    except Exception as exc:
        _log_exc("coach_answer", exc)
        return None


def ping(model: str) -> tuple[bool, str | None]:
    """Validate a model end-to-end (incl. JSON mode, which the daily coach uses)."""
    client = _client()
    if client is None:
        return False, "OPENAI_API_KEY not set or openai package missing"
    try:
        resp = _create(
            client,
            model,
            [{"role": "user", "content": 'Reply with JSON: {"ok": true}'}],
            json_mode=True,
            max_out=1500,  # realistic budget so reasoning models aren't starved during the test
            temperature=0,
        )
        content = resp.choices[0].message.content
        if not content or not content.strip():
            return False, "model returned empty output (likely a reasoning model needing a larger token budget)"
        return True, None
    except Exception as exc:
        return False, str(exc)[:200]


# Curated shortlist (cheap → strong); only those actually on the account are shown.
_COMMON_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "gpt-5-mini", "gpt-5", "gpt-5.5"]


def list_models() -> dict[str, Any]:
    """Curated common chat models available on the account, plus current + default."""
    base = {"current": current_model(), "default": settings.openai_model}
    client = _client()
    if client is None:
        return {**base, "models": [], "error": "OPENAI_API_KEY not set"}
    try:
        available = {m.id for m in client.models.list().data}
        models = [m for m in _COMMON_MODELS if m in available]
        if base["current"] not in models:
            models.append(base["current"])  # always show the active selection
        return {**base, "models": models}
    except Exception as exc:
        return {**base, "models": [], "error": str(exc)[:200]}
