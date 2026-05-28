# PACER — Personal Adaptive Coach

Self-hosted FastAPI service that turns Garmin Connect data into an adaptive
running plan, scores readiness, and pushes today's workout to your watch each
morning. OpenAI does the daily coach-layer adaptation; a deterministic
rule-based engine is the fallback.

The dashboard (single-file HTML at `/`) shows today's plan, last run's AI
coach review, recovery + fitness, goal progress with an LLM-driven ETA, and
a chat with the coach.

Live instance: <https://app.fanrun.app> (single user, magic-link gated).

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in: GARMIN_EMAIL, GARMIN_PASSWORD, API_KEY (strong random), optionally OPENAI_API_KEY
uvicorn app.main:app --reload
```

Open the dashboard, paste your `API_KEY` in the gate:

```text
http://127.0.0.1:8000/          # PACER dashboard
http://127.0.0.1:8000/docs      # Swagger API docs
```

## Run with Docker (canonical)

```bash
cp .env.example .env
# fill in GARMIN_EMAIL/PASSWORD, API_KEY, OPENAI_API_KEY, NOTIFY_CHANNEL,
# and CLOUDFLARE_TUNNEL_TOKEN (see "Public exposure" below) — see .env.example
docker compose up -d --build
```

Code lives in the image (`COPY app ./app`); after editing `app/*` rebuild **and**
force-recreate so the running container picks up the new image:

```bash
docker compose up -d --build --force-recreate running-assistant
```

The bundled `docker-compose.yml` also defines a `cloudflare-tunnel` service for
public access — see **Public exposure** below.

## Production deployment

The live instance runs on an **Oracle Cloud Always Free Ampere A1** VM (ARM,
Tel Aviv region, 1 OCPU / 6 GB) behind a Cloudflare Tunnel pointed at the
domain `app.fanrun.app`. The architecture is fully outbound — no inbound
ports open to the public internet — so the same `docker compose up -d` works
on any Linux host that has Docker installed.

The image builds for both `linux/amd64` and `linux/arm64` (all dependencies in
`requirements.txt` have ARM wheels).

## Public exposure (Cloudflare Tunnel)

The compose file runs a `cloudflared` container alongside the app. Public
traffic enters Cloudflare's edge → Cloudflare delivers the request down an
outbound-initiated, persistent QUIC connection to the cloudflared container
→ cloudflared proxies to `running-assistant:8000` on the internal docker
network. Your host never accepts inbound HTTP — port 8000 is only reachable
from inside the docker network.

Set up:

1. In the Cloudflare dashboard create a **Named Tunnel** (Zero Trust → Networks
   → Tunnels). Copy the long base64-ish token.
2. Put it in `.env`:
   ```text
   CLOUDFLARE_TUNNEL_TOKEN=eyJh...
   ```
3. Add a **Public Hostname** route on the same tunnel:
   `app.yourdomain.com → http://running-assistant:8000`
4. `docker compose up -d`

The token is interpolated into the cloudflared `command:` line at compose-parse
time — it lives **only** in `.env` (which is gitignored), never in the
committed compose file.

Every request still requires the `X-API-Key` header (see Authentication).

## Authentication

Every endpoint requires an API key in the `X-API-Key` header. Open paths:
`/`, `/dashboard`, `/health`, `/docs`, `/openapi.json`, `/redoc`.

Set a strong key in `.env`:

```text
API_KEY=your-strong-random-key
```

Generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Behaviour:

- Missing/incorrect key on a protected endpoint → `401` (constant-time compare)
- Server started without `API_KEY` set → `503` (fails closed, never wide open)

### Magic-link login

Sharing the API key is awkward, so the dashboard supports a one-click flow:

```text
https://app.yourdomain.com/#key=<API_KEY>
```

When you open that link, a `<head>` IIFE runs before anything else:

1. Reads the `#key=…` from `location.hash`
2. Stores it to `localStorage["ra_api_key"]`
3. `history.replaceState` rewrites the address bar to clean `/` so the key never
   sits in browser history, screenshots, or referrer headers

Subsequent dashboard requests attach `X-API-Key` from localStorage. The "Copy
link" button in the header regenerates this URL for sharing to a new device.

## Configuration (`.env`)

| Var | Purpose |
|---|---|
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Garmin Connect login |
| `GARMIN_MFA_CODE` | Optional, if your account requires MFA at login |
| `API_KEY` | Required for any non-localhost exposure |
| `OPENAI_API_KEY` | Enables AI coach + ETA + run review (rule-based fallback if unset) |
| `OPENAI_MODEL` | Default model, runtime-overridable from the dashboard (`/config/model`) |
| `MORNING_UPDATE_TIME` | Local-time HH:MM of the daily auto-adapt (default `06:00`) |
| `TIMEZONE` | IANA TZ for the morning cron (default `Asia/Jerusalem`) |
| `GOAL_AUTO_PUSH` | `true` to also push the adapted workout to Garmin each morning |
| `NOTIFY_CHANNEL` | `none` / `email` / `callmebot` — runtime-overridable from the dashboard |
| `SMTP_*`, `EMAIL_*` | Gmail SMTP for the email channel |
| `CALLMEBOT_PHONE`, `CALLMEBOT_APIKEY` | WhatsApp via CallMeBot for that channel |
| `AUTO_SYNC_ENABLED`, `SYNC_INTERVAL_MINUTES` | Background Garmin sync loop |
| `CLOUDFLARE_TUNNEL_TOKEN` | Token from your Cloudflare Named Tunnel |

Persistent state (gitignored):
- `data/running_assistant.db` — SQLite, all stored runs + goal + plan + analyses
- `data/garmin_token/` — Cached Garmin OAuth tokens (avoid re-SSO every restart)

## Daily flow

At `MORNING_UPDATE_TIME` (06:00 by default, in your TZ) an in-process
APScheduler job runs:

1. `run_garmin_sync(days=45, notify_analysis=False)` — pulls latest activities;
   run-review analyses on new activities are saved to the DB but NOT emailed
   (to avoid double-emails seconds before the morning summary)
2. `record_snapshot(goal)` — captures today's Garmin race-prediction
3. `adapt_today(use_live_metrics=True)` — reads readiness, HRV, sleep, recent
   planned-vs-actual, calls the OpenAI coach with safe bounds, falls back to
   rules; writes the adapted workout to `planned_workout`
4. `send_morning_summary(workout)` — emails the day's workout + coach note
   (via Gmail SMTP, CallMeBot WhatsApp, or skipped per `NOTIFY_CHANNEL`)
5. Auto-pushes the adapted workout to Garmin (skipped on rest days). On
   schedule failure, writes a `sync_log("warn", …)` row and does NOT mark
   pushed — so next run retries

Any failure at step 3 writes `sync_log("error", …)`, surfacing it in
`/sync/status` and on the dashboard's Sync card.

## AI features

### Run review on sync

When `run_garmin_sync` ingests new running activities, `analyze_new_runs_and_notify`
queues them through the OpenAI coach for a per-activity professional summary
(pace vs target, what went well, concerns, one takeaway). The analysis row is
**saved BEFORE the email is sent** so the once-per-activity guarantee holds
even if SMTP retries. The dashboard's *Last Run · AI Coach Review* card shows
the most recent one.

### Goal ETA

`estimate_eta(goal)` calls the OpenAI coach with: goal, race-prediction
history, current training plan, last 10 runs, training consistency over 28
days, load-based readiness, and a live Garmin recovery+fitness snapshot. The
coach returns `{estimated_date, on_pace, weeks_remaining, explanation}` —
explicitly instructed to cross-check Garmin predictions against the other
signals (Garmin tends to be optimistic for longer distances). Cached 6h in
the `app_config` table; `POST /goal` bypasses the cache.

### Coach chat

`POST /goal/coach/ask` — free-form question, grounded in your goal + plan +
readiness context. Returns escaped text rendered into the dashboard's "Ask
the coach" panel.

### Runtime model + channel switching

The dashboard header has dropdowns for:
- **Model** (`/openai/models` lists curated common models + custom) — changes
  the active OpenAI model, persisted to the `app_config` table, no restart
- **Notify channel** (`/config/notify`) — same idea for the notification channel

## API reference

All endpoints require `X-API-Key` except `/`, `/dashboard`, `/health`,
`/docs`, `/openapi.json`, `/redoc`. `*` marks a required parameter.

### System

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check (no auth). |

### Data & sync

| Method | Path | Parameters | Description |
|---|---|---|---|
| POST | `/sync/garmin` | `days=30`, `notify_analysis=true` | Sync recent runs into SQLite; trigger run-review on new ones. |
| GET | `/sync/status` | — | Auto-sync config + recent sync-log entries. |
| POST | `/import/garmin-csv` | `distance_unit=km`, `file`* | Import historical runs from a Garmin CSV export. |
| GET | `/activities/runs` | `limit=50` | List stored running activities. |
| GET | `/activities/last/splits` | — | Per-lap splits of your most recent run. |
| GET | `/activities/last/analysis` | — | Most recent AI coach review. |
| POST | `/activities/analyze-new` | `limit=5` | Force the analyze hook (skips already-analyzed). |

### Planning (legacy rule-based)

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/readiness` | — | Readiness score/status from stored load (acute vs. 4-week). |
| GET | `/plan/today` | `goal=general_fitness`, `today` | Today's recommended session. |
| GET | `/plan/week` | `target_distance_km`, `goal`, `start_date` | 7-day plan. |
| GET | `/assistant/context` | — | Readiness + recent runs + today & week plans bundled. |

### Goal-driven adaptive plan

| Method | Path | Parameters | Description |
|---|---|---|---|
| POST | `/goal` | `{distance_km, target_time}` | Set goal; build + store plan from current fitness; return ETA. |
| GET | `/goal` | `progress=false` | Active goal; `?progress=true` adds Garmin race-prediction. |
| DELETE | `/goal` | — | Deactivate the active goal. |
| GET | `/goal/week` | — | 7-day picture: today firm, projected days 2–7. |
| GET | `/goal/plan` | `days=21` | Upcoming planned workouts. |
| GET | `/goal/progress` | `weeks=12` | Garmin race-prediction trend vs target. |
| GET | `/goal/stats` | `days=30` | Consistency (% of run days completed + streak). |
| GET | `/goal/eta` | `fresh=false` | LLM completion-date estimate + explanation (`?fresh=true` bypasses 6h cache). |
| POST | `/goal/coach/ask` | `{question}` | Ask the OpenAI coach a free-form question. |
| GET | `/goal/today` | — | Today's adapted workout (cheap DB read). |
| POST | `/goal/today/refresh` | `live=true` | Force the adaptive recompute now. |
| POST | `/goal/today/push` | — | Push today's workout to Garmin (skips rest days). |
| POST | `/goal/today/notify` | — | Send today's workout via the configured channel. |
| POST | `/goal/week/push` | `days=7`, `force=false` | Push next N days to Garmin; `force=true` deletes old then re-pushes. |

### Live Garmin metrics

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/garmin/recovery` | `date` (default today) | Training-readiness, HRV, sleep, stress, body-battery, RHR, respiration, SpO2. |
| GET | `/garmin/fitness` | `date` (default today) | Training-status, race-predictions, VO2max, endurance, hill, fitness-age. |
| GET | `/garmin/snapshot` | `date` (default today) | Compact recovery + fitness summary (one login, used by dashboard). |
| GET | `/garmin/calendar/month` | `year`*, `month0`* (0-11) | Garmin's calendar for a given month. |
| POST | `/garmin/workout/{workout_id}/schedule` | `date`* | Schedule an existing Garmin workout. |
| GET | `/gear` | — | Shoe/gear list with total km + replace-soon flag (~600 km). |

### Workouts (legacy)

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/workouts/today/json` | `goal`, `today` | Build today's structured workout, save JSON. |
| GET | `/workouts/today/garmin-payload` | `goal`, `today` | Today's workout as a Garmin workout-service payload. |
| GET | `/workouts/today/download` | `goal`, `today` | Download today's workout JSON file. |
| POST | `/workouts/today/push-to-garmin` | `goal`, `today`, `schedule=true` | Create (and optionally schedule) today's workout on Garmin. |
| POST | `/workouts/week/export-json` | `goal`, `start_date` | Export the week as JSON files. |
| POST | `/workouts/week/push-to-garmin` | `goal`, `start_date`, `schedule=true` | Create/schedule each day's workout. |

### Config (runtime overrides)

| Method | Path | Body / Params | Description |
|---|---|---|---|
| GET | `/openai/models` | — | Curated list of common chat models + current selection. |
| POST | `/config/model` | `{model}` | Switch the coach model at runtime (validated with a ping first). |
| GET | `/config/notify` | — | Current notify channel + options. |
| POST | `/config/notify` | `{channel}` | Switch the notify channel at runtime (`none`/`email`/`callmebot`). |

## Notify channels

| Channel | Setup |
|---|---|
| `email` *(recommended)* | Gmail SMTP. Enable 2-Step Verification on your Google account, create a 16-char **App Password** at <https://myaccount.google.com/apppasswords>, then set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASS` (the App Password), `EMAIL_FROM`, `EMAIL_TO`. |
| `callmebot` | WhatsApp via the CallMeBot bot. Message *"I allow callmebot to send me messages"* to `+34 644 51 95 23`; copy the returned API key into `CALLMEBOT_APIKEY`. Set `CALLMEBOT_PHONE` (international format, no `+`). |
| `none` | Disabled. |

Test the configured channel: `POST /goal/today/notify`.

Switch channels at runtime from the dashboard header dropdown (writes to
`app_config`, no restart needed) or via `POST /config/notify`.

## Garmin notes

This project uses the community `python-garminconnect` library, which logs
into Garmin Connect using your email/password and operates on Garmin's
private web API. It may break if Garmin changes login flows. The Garmin
session is created in `app/garmin_client.py` and reused by `app/garmin_sync.py`,
`app/garmin_metrics.py`, and `app/workout_publisher.py` so a future switch to
the official OAuth2 Developer Program is a localized change.

**Token caching** to avoid SSO rate-limits: after the first successful login,
OAuth tokens are persisted under `data/garmin_token/` (oauth1 + oauth2 JSON);
subsequent calls reuse them. Garmin's SSO endpoint will 429 if you log in too
often (especially after an IP change) — the cache prevents that.

### Workout push to watch

`/workouts/*/push-to-garmin` and `/goal/today/push` create a structured workout
via `POST /workout-service/workout` and schedule it via
`POST /workout-service/schedule/{id}`. The `/workout-service/workout/{id}`
DELETE is used by `daily_coach.adapt_today` (and `/goal/week/push?force=true`)
to remove a stale push before replacing it — preventing duplicate calendar
entries on your watch.

If workout creation succeeds but scheduling fails, the result includes
`schedule_error` — the morning job writes a `sync_log("warn", …)` and does
NOT call `mark_garmin_pushed`, so the next run will retry.

### Underlying Garmin data available (not yet exposed)

The `garminconnect` session can read much more than the endpoints above
expose. See [the project's `app/garmin_client.py`](app/garmin_client.py)
for the underlying methods.
