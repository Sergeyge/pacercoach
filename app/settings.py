from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    garmin_email: str | None = os.getenv("GARMIN_EMAIL")
    garmin_password: str | None = os.getenv("GARMIN_PASSWORD")
    garmin_mfa_code: str | None = os.getenv("GARMIN_MFA_CODE")
    auto_sync_enabled: bool = os.getenv("AUTO_SYNC_ENABLED", "false").lower() == "true"
    sync_interval_minutes: int = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))
    target_distance_km: float = float(os.getenv("TARGET_DISTANCE_KM", "35"))
    training_goal: str = os.getenv("TRAINING_GOAL", "general_fitness")
    api_key: str | None = os.getenv("API_KEY")

    # OpenAI (ChatGPT) — daily adaptive coaching layer
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Goal-driven plan: morning auto-update
    morning_update_time: str = os.getenv("MORNING_UPDATE_TIME", "06:00")  # HH:MM, local to TIMEZONE
    timezone: str = os.getenv("TIMEZONE", "Asia/Jerusalem")
    goal_auto_push: bool = os.getenv("GOAL_AUTO_PUSH", "true").lower() == "true"
    goal_push_horizon_days: int = int(os.getenv("GOAL_PUSH_HORIZON_DAYS", "7"))

    # Morning summary notification
    notify_channel: str = os.getenv("NOTIFY_CHANNEL", "none")  # none | callmebot | email
    callmebot_phone: str | None = os.getenv("CALLMEBOT_PHONE")
    callmebot_apikey: str | None = os.getenv("CALLMEBOT_APIKEY")
    # Email (SMTP) — for Gmail use an App Password as SMTP_PASS
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str | None = os.getenv("SMTP_USER")
    smtp_pass: str | None = os.getenv("SMTP_PASS")
    email_from: str | None = os.getenv("EMAIL_FROM")
    email_to: str | None = os.getenv("EMAIL_TO")


settings = Settings()
