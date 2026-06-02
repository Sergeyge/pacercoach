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
    "- Respect the provided safety bounds: 'distance_km' must be between 0 and "
    "'max_distance_km', and 'kind' must be one of 'allowed_kinds'.\n"
    "- If recovery is poor, reduce volume/intensity or prescribe rest. If the athlete is "
    "fresh and on track, keep the planned work.\n"
    "- Keep changes conservative; never increase load sharply.\n"
    "Respond with ONLY a JSON object: {\"kind\": str, \"distance_km\": number, "
    "\"target_pace_sec\": int|null, \"details\": str, \"coach_note\": str}. "
    "'coach_note' is one short sentence explaining the adjustment."
)

_COACH_CHAT_SYSTEM = (
    "You are the athlete's personal running coach. Answer their question briefly and "
    "practically (2-4 sentences) using the provided context: their goal, today's planned "
    "workout, the upcoming week, recent results, and readiness. Be encouraging but honest. "
    "Do not invent data you weren't given; if you can't answer from the context, say so.\n\n"
    "You can ALSO change the training plan when the athlete asks you to (e.g. 'make today "
    "easier', 'move my long run to Sunday', 'I need a rest day today', 'shorten Friday'). "
    "When — and only when — the athlete is clearly asking to change a specific day, include "
    "a `proposed_change` describing it. Otherwise set proposed_change to null.\n\n"
    "Allowed actions:\n"
    "- adjust_day: change one day's kind and/or distance. Fields: date (YYYY-MM-DD), "
    "kind (one of rest|recovery|easy|long|quality), distance_km (number; 0 for rest).\n"
    "- rest_day: turn one day into rest. Fields: date.\n"
    "- swap_days: swap the workouts of two dates. Fields: date, date2.\n"
    "Only propose changes to dates within the upcoming plan you were given. Keep changes "
    "sensible and conservative (never spike volume). Always describe the change in `summary` "
    "in one short sentence, and explain it in `answer` too.\n\n"
    "Respond with ONLY a JSON object: {\"answer\": str, \"proposed_change\": null | "
    "{\"action\": str, \"date\": str, \"date2\": str|null, \"kind\": str|null, "
    "\"distance_km\": number|null, \"summary\": str}}."
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
