from __future__ import annotations

import smtplib
import ssl
import urllib.parse
import urllib.request
from datetime import date
from email.message import EmailMessage
from typing import Any

from .settings import settings


def format_summary(workout: dict[str, Any]) -> str:
    """One short message: today's session + coaching note + readiness."""
    d = workout.get("date") or workout.get("plan_date") or date.today().isoformat()
    try:
        dow = date.fromisoformat(str(d)).strftime("%a %d %b")
    except Exception:
        dow = str(d)

    kind = (workout.get("kind") or "run").title()
    dist = float(workout.get("distance_km") or 0)
    pace = workout.get("target_pace_sec")
    pace_txt = f" @ {int(pace) // 60}:{int(pace) % 60:02d}/km" if pace else ""
    line = "Rest day" if (workout.get("kind") == "rest" or dist <= 0) else f"{kind} — {dist:g} km{pace_txt}"

    parts = [f"\U0001F3C3 Today · {dow}", line]
    note = workout.get("coach_note")
    if note:
        parts.append(f"Coach: {note}")
    readiness = workout.get("readiness") or {}
    if readiness.get("status"):
        score = readiness.get("score")
        parts.append(f"Readiness: {readiness['status']}" + (f" ({score})" if score is not None else ""))
    return "\n".join(parts)


def send_whatsapp_callmebot(text: str) -> dict[str, Any]:
    if not settings.callmebot_phone or not settings.callmebot_apikey:
        return {"status": "skipped", "reason": "CALLMEBOT_PHONE / CALLMEBOT_APIKEY not configured"}
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": settings.callmebot_phone, "text": text, "apikey": settings.callmebot_apikey}
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"status": "sent", "http_status": resp.status, "response": body[:200]}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


_EMAIL_STYLE = """
body{margin:0;background:#f4f4f1;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d18;line-height:1.5}
.wrap{padding:24px 12px}
.card{max-width:580px;margin:0 auto;background:#fff;border-radius:14px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,0.06);border:1px solid rgba(0,0,0,0.04)}
.brand{font-weight:800;letter-spacing:2px;font-size:12px;text-transform:uppercase;color:#1a1d18}
.brand .lit{background:#c8fb50;padding:4px 9px;border-radius:5px;color:#10130b}
.eyebrow{text-transform:uppercase;letter-spacing:2px;color:#7a8170;font-size:11px;font-weight:600;margin-top:22px}
.title{font-size:24px;margin:6px 0 4px;font-weight:700;color:#1a1d18}
.subtitle{color:#5d6452;font-size:14px;margin-bottom:14px}
.stat{font-size:18px;line-height:1.5;color:#1a1d18;padding:14px 16px;background:#f7f8f4;border-radius:10px;margin:10px 0 4px}
.stat .accent{color:#2e7d2e;font-weight:700}
.pill{display:inline-block;padding:5px 12px;border-radius:999px;font-size:12px;font-weight:600;margin-top:10px}
.pill.green{background:#e3f7e6;color:#1f7a35}
.pill.yellow{background:#fef5d6;color:#8a6107}
.pill.red{background:#fde3e1;color:#8a1a14}
.note{background:#f7f8f4;border-left:3px solid #c8fb50;padding:14px 16px;border-radius:6px;margin:16px 0;font-size:14px;line-height:1.6;color:#3a4030}
.note p{margin:0 0 10px}
.note p:last-child{margin-bottom:0}
.footer{color:#7a8170;font-size:11px;margin-top:22px;text-align:center}
"""


def _email_shell(body_html: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{_EMAIL_STYLE}</style></head><body>"
        "<div class=\"wrap\"><div class=\"card\">"
        "<div class=\"brand\"><span class=\"lit\">PACER</span> &middot; running coach</div>"
        f"{body_html}"
        "<div class=\"footer\">PACER &middot; your AI-assisted running coach</div>"
        "</div></div></body></html>"
    )


def morning_email_html(workout: dict[str, Any]) -> str:
    d = workout.get("date") or workout.get("plan_date") or date.today().isoformat()
    try:
        dow = date.fromisoformat(str(d)).strftime("%A &middot; %B %-d")
    except Exception:
        dow = str(d)
    kind = (workout.get("kind") or "run").title()
    dist = float(workout.get("distance_km") or 0)
    pace = workout.get("target_pace_sec")
    pace_txt = f"{int(pace) // 60}:{int(pace) % 60:02d}/km" if pace else ""
    body = "<div class=\"eyebrow\">Today's session</div>"
    body += f"<div class=\"title\">{dow}</div>"
    if workout.get("kind") == "rest" or dist <= 0:
        body += "<div class=\"stat\"><span class=\"accent\">Rest day</span> &mdash; recover, hydrate, light mobility.</div>"
    else:
        stat = f"<span class=\"accent\">{kind}</span> &middot; {dist:g} km"
        if pace_txt:
            stat += f" @ {pace_txt}"
        body += f"<div class=\"stat\">{stat}</div>"
    note = workout.get("coach_note")
    if note:
        body += f"<div class=\"note\"><p>{_escape(note)}</p></div>"
    readiness = workout.get("readiness") or {}
    status = readiness.get("status")
    if status in ("green", "yellow", "red"):
        score = readiness.get("score")
        score_txt = f" &middot; {score}" if score is not None else ""
        body += f"<div><span class=\"pill {status}\">readiness &middot; {status}{score_txt}</span></div>"
    return _email_shell(body)


def analysis_email_html(activity: dict[str, Any], summary: str) -> str:
    raw = activity.get("activity_date") or ""
    try:
        dow = date.fromisoformat(str(raw)).strftime("%A &middot; %B %-d, %Y")
    except Exception:
        dow = str(raw) or "Latest run"
    body = "<div class=\"eyebrow\">Run review</div>"
    body += f"<div class=\"title\">{dow}</div>"
    parts: list[str] = []
    if activity.get("distance_km") is not None:
        parts.append(f"<b>{activity['distance_km']:g}</b> km")
    pace = activity.get("avg_pace_sec_per_km")
    if pace:
        p = int(pace)
        parts.append(f"<b>{p // 60}:{p % 60:02d}/km</b>")
    if activity.get("avg_hr"):
        parts.append(f"<b>{int(activity['avg_hr'])}</b> bpm")
    if parts:
        body += f"<div class=\"subtitle\">{' &middot; '.join(parts)}</div>"
    paragraphs = []
    for chunk in (summary or "").split("\n\n"):
        chunk = chunk.strip()
        if chunk:
            paragraphs.append(_escape(chunk).replace("\n", "<br>"))
    if not paragraphs:
        paragraphs = [_escape(summary or "").replace("\n", "<br>")]
    note_html = "".join(f"<p>{p}</p>" for p in paragraphs)
    body += f"<div class=\"note\">{note_html}</div>"
    return _email_shell(body)


def _escape(s: str) -> str:
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def send_email(text: str, subject: str = "PACER · today's workout", html: str | None = None) -> dict[str, Any]:
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_pass):
        return {"status": "skipped", "reason": "SMTP_HOST / SMTP_USER / SMTP_PASS not configured"}
    if not settings.email_to:
        return {"status": "skipped", "reason": "EMAIL_TO not configured"}
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = settings.email_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(settings.smtp_user, settings.smtp_pass)
            server.send_message(msg)
        return {"status": "sent", "to": settings.email_to}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


def _summary_subject(workout: dict[str, Any]) -> str:
    d = workout.get("date") or workout.get("plan_date") or date.today().isoformat()
    try:
        dow = date.fromisoformat(str(d)).strftime("%a %d %b")
    except Exception:
        dow = str(d)
    kind = (workout.get("kind") or "run").title()
    return f"PACER · {dow} · {kind}"


def send_message(subject: str, text: str, html: str | None = None) -> dict[str, Any]:
    """Channel-agnostic send for ad-hoc messages (e.g. run-analysis summaries)."""
    channel = current_channel()
    if channel in ("", "none"):
        return {"status": "disabled", "channel": channel}
    if channel == "callmebot":
        # CallMeBot has no subject — prepend the subject in the body.
        return send_whatsapp_callmebot(f"*{subject}*\n\n{text}")
    if channel == "email":
        return send_email(text, subject=subject, html=html)
    return {"status": "error", "reason": f"unknown channel '{channel}'"}


def current_channel() -> str:
    """Active channel — runtime override (DB) falling back to env default."""
    from .db import get_config

    return (get_config("notify_channel", settings.notify_channel) or "none").lower()


def send_morning_summary(workout: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the morning summary on the configured channel. Never raises."""
    text = format_summary(workout)
    channel = current_channel()
    if channel in ("", "none"):
        return {"status": "disabled", "channel": channel, "message": text}
    if channel == "callmebot":
        result = send_whatsapp_callmebot(text)
        result["message"] = text
        return result
    if channel == "email":
        result = send_email(text, subject=_summary_subject(workout), html=morning_email_html(workout))
        result["message"] = text
        return result
    return {"status": "error", "reason": f"unknown NOTIFY_CHANNEL '{channel}'", "message": text}
