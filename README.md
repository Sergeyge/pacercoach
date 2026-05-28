# Running Personal Assistant — Automated Garmin Sync MVP

This version adds automatic Garmin sync on top of the previous CSV importer, plus
live Garmin health/fitness metrics and API-key authentication.

## Important Garmin note

There are two ways to automate Garmin data ingestion:

1. **Production / correct path:** Garmin Connect Developer Program using OAuth 2.0. This requires Garmin approval and is business-focused.
2. **Personal prototype path:** `python-garminconnect`, a community library that logs into Garmin Connect. This may break if Garmin changes login flows and should not be used as a commercial/production integration.

This MVP implements option 2 so you can run it now. The Garmin session is created in `app/garmin_client.py` and used by `app/garmin_sync.py`, `app/garmin_metrics.py`, and `app/workout_publisher.py` so it can later be replaced by the official OAuth integration.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Garmin credentials AND a strong API_KEY (see Authentication)
uvicorn app.main:app --reload
```

Open the **dashboard** (enter your `API_KEY` when prompted) or the API docs:

```text
http://127.0.0.1:8000/          # PACER dashboard (UI)
http://127.0.0.1:8000/docs      # Swagger API docs
```

## Run with Docker

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

The bundled `docker-compose.yml` also defines a `cloudflare-tunnel` service for
reaching the app beyond localhost — see **Exposing the app publicly** below.
Whenever the app is reachable beyond localhost, **`API_KEY` must be set** (see
Authentication).

> Note: code lives in the image (`COPY app ./app`), so after editing `app/` you
> must rebuild **and** recreate the container:
> `docker compose up -d --build --force-recreate running-assistant`

## Exposing the app publicly (Cloudflare Tunnel)

There is **no inbound port open to the internet** — the published `8000:8000`
mapping is reachable only on the host and its LAN. Public access goes through a
Cloudflare Tunnel, and **every request still needs the `X-API-Key` header** (see
Authentication). Never expose the app without `API_KEY` set.

### Free / instant — TryCloudflare quick tunnel

Gives a random, temporary `*.trycloudflare.com` URL with no domain and no account.
Run it on the same Docker network as the app:

```bash
docker run -d --name running-assistant-quicktunnel \
  --network "$(docker network ls --format '{{.Name}}' | grep tunnel_net | head -1)" \
  cloudflare/cloudflared:latest \
  tunnel --no-autoupdate --url http://running-assistant:8000

# print the public URL
docker logs running-assistant-quicktunnel 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
```

- The URL is **ephemeral** — it changes every time the container restarts.
- Stop / remove it: `docker rm -f running-assistant-quicktunnel`.

### Stable — named tunnel + your own domain

The `cloudflare-tunnel` service in `docker-compose.yml` runs a *named* tunnel via
a tunnel token. For a permanent hostname you need a **domain you own** (domain
names may not contain underscores) added to Cloudflare, then a public-hostname
route mapping e.g. `app.yourdomain.com → http://running-assistant:8000`.

## Authentication

Every endpoint requires an API key sent in the `X-API-Key` header. The only
unauthenticated paths are `/health`, `/docs`, `/openapi.json`, and `/redoc`.

Set a strong key in `.env`:

```text
API_KEY=your-strong-random-key
```

Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Behaviour:

- Missing/incorrect key on a protected endpoint → `401`.
- Server started without `API_KEY` set → protected endpoints return `503` (fails closed, never wide open).

In the examples below, export your key once and reuse it:

```bash
export API_KEY=your-strong-random-key
```

In Swagger UI (`/docs`), click **Authorize** and paste the key.

## API reference

All endpoints require the `X-API-Key` header **except** `/health`, `/docs`,
`/openapi.json`, and `/redoc`. Query parameters are listed with their defaults;
`*` marks a required parameter.

### System

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/health` | — | Liveness check (no auth). |

### Data & sync

| Method | Path | Parameters | Description |
|---|---|---|---|
| POST | `/sync/garmin` | `days=30` (1–365) | Sync recent runs from Garmin Connect into SQLite. |
| GET | `/sync/status` | — | Auto-sync config + the most recent sync-log entries. |
| POST | `/import/garmin-csv` | `distance_unit=km` (`km`\|`miles`), `file`* (multipart) | Import historical runs from a Garmin CSV export. |
| GET | `/activities/runs` | `limit=50` | List stored running activities (most recent first). |

### Planning (local computation)

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/readiness` | — | Readiness score/status from stored load (acute vs. 4-week). |
| GET | `/plan/today` | `goal=general_fitness`, `today` | Today's recommended session. |
| GET | `/plan/week` | `target_distance_km` (5–120), `goal=general_fitness`, `start_date` | 7-day plan; target auto-derived if omitted. |
| GET | `/plan/weekly` | *(alias of `/plan/week`)* | Same as `/plan/week`. |
| GET | `/assistant/context` | — | Aggregate: readiness + recent runs + today & week plans. |

### Goal-driven adaptive plan

| Method | Path | Parameters | Description |
|---|---|---|---|
| POST | `/goal` | `{distance_km, target_time}` (JSON body) | Set the goal; builds + stores the plan from current fitness. |
| GET | `/goal` | `progress=false` | Active goal; `?progress=true` adds Garmin race-prediction vs target (live, slower). |
| DELETE | `/goal` | — | Deactivate the active goal. |
| GET | `/goal/week` | — | 7-day picture: today firm (re-adapted each morning), days 2–7 projected. |
| GET | `/goal/progress` | `weeks=12` (2–52) | Garmin race-prediction trend for the goal distance vs target (on-pace verdict + ETA). |
| GET | `/goal/stats` | `days=30` (7–120) | Consistency: % of planned run days completed + current streak. |
| POST | `/goal/coach/ask` | `{question}` (JSON) | Ask the OpenAI coach a free-form question, grounded in your goal/plan/readiness. |
| GET | `/goal/plan` | `days=21` (1–120) | Upcoming planned workouts. |
| GET | `/goal/today` | — | Today's workout (adapted if available, else rule-based; cheap read). |
| POST | `/goal/today/refresh` | `live=true` | Force the adaptive recompute now (rules + OpenAI + morning metrics). |
| POST | `/goal/today/push` | — | Push today's workout to Garmin (skips rest days). |
| POST | `/goal/today/notify` | — | Send today's workout + coaching note as a morning summary (per `NOTIFY_CHANNEL`). |

### Live Garmin metrics

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/garmin/recovery` | `date` (default today) | Recovery: `training_readiness`, `hrv`, `sleep`, `stress`, `body_battery`, `resting_heart_rate`, `respiration`, `spo2`. |
| GET | `/garmin/fitness` | `date` (default today) | Performance: `training_status`, `race_predictions`, `vo2max`, `endurance_score`, `hill_score`, `fitness_age`. |
| GET | `/garmin/snapshot` | `date` (default today) | One login → compact recovery + fitness summary (used by the dashboard). |
| GET | `/activities/last/splits` | — | Per-lap/km splits of your most recent run. |
| GET | `/gear` | — | Shoe/gear list with total km + replace-soon flag (~600 km). |

### Workouts

| Method | Path | Parameters | Description |
|---|---|---|---|
| GET | `/workouts/today/json` | `goal`, `today` | Build today's structured workout and save it as JSON. |
| GET | `/workouts/today/garmin-payload` | `goal`, `today` | Today's workout as a Garmin workout-service payload. |
| GET | `/workouts/today/download` | `goal`, `today` | Download today's workout JSON file. |
| POST | `/workouts/today/push-to-garmin` | `goal`, `today`, `schedule=true` | Create (and optionally schedule) today's workout on Garmin. |
| POST | `/workouts/week/export-json` | `goal`, `start_date` | Export the week's workouts as JSON files. |
| POST | `/workouts/week/push-to-garmin` | `goal`, `start_date`, `schedule=true` | Create/schedule each run day's workout on Garmin. |

> Workout `goal` defaults to the `TRAINING_GOAL` env value; `today` / `start_date`
> default to the current date.

### Underlying Garmin data available (not yet exposed)

The `garminconnect` session can read much more than the endpoints above expose.
These are available to wire into new endpoints:

- **Recovery / readiness:** `get_training_readiness`, `get_hrv_data`, `get_sleep_data`, `get_stress_data`, `get_all_day_stress`, `get_body_battery`, `get_body_battery_events`, `get_rhr_day`, `get_respiration_data`, `get_spo2_data`
- **Fitness / performance:** `get_training_status`, `get_race_predictions`, `get_max_metrics`, `get_endurance_score`, `get_hill_score`, `get_fitnessage_data`
- **Activities & records:** `get_activities_by_date`, `get_activities_fordate`, `get_last_activity`, `get_activity`, `get_activity_details`, `get_activity_splits`, `get_activity_weather`, `get_activity_gear`, `get_personal_record`, `upload_activity`, `download_activity`, `create_manual_activity`, `delete_activity`, `set_activity_name`
- **Workouts:** `get_workouts`, `get_workout_by_id`, `download_workout`
- **Gear (shoes):** `get_gear`, `get_gear_stats`, `get_gear_ativities`, `get_gear_defaults`, `set_gear_default`
- **Daily load / stats:** `get_stats`, `get_stats_and_body`, `get_user_summary`, `get_daily_steps`, `get_steps_data`, `get_heart_rates`, `get_intensity_minutes_data`, `get_floors`, `get_progress_summary_between_dates`
- **Profile / device:** `get_full_name`, `get_user_profile`, `get_userprofile_settings`, `get_unit_system`, `get_goals`, `get_devices`, `get_device_settings`, `get_primary_training_device`
- **Off-domain (weight/BP/hydration/badges/menstrual, etc.):** `get_weigh_ins`, `add_weigh_in`, `get_body_composition`, `get_blood_pressure`, `get_hydration_data`, `get_earned_badges`, `get_badge_challenges`, `get_menstrual_calendar_data`, `get_pregnancy_summary`

## API flow

Manual sync now:

```bash
curl -X POST -H "X-API-Key: $API_KEY" "http://127.0.0.1:8000/sync/garmin?days=30"
```

Check sync status:

```bash
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/sync/status
```

Get readiness:

```bash
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/readiness
```

Get weekly plan:

```bash
curl -H "X-API-Key: $API_KEY" "http://127.0.0.1:8000/plan/weekly?target_distance_km=35&goal=half_marathon_sub_2h"
```

## Automatic sync

Set this in `.env`:

```text
AUTO_SYNC_ENABLED=true
SYNC_INTERVAL_MINUTES=60
```

The app will sync Garmin every 60 minutes and store new running activities in SQLite.
(The background scheduler runs in-process and is not affected by API-key auth.)

## Existing CSV fallback

The previous `/import/garmin-csv` endpoint still exists, so you can bootstrap the DB with historical data if needed.

```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  -F "file=@activities.csv" \
  "http://127.0.0.1:8000/import/garmin-csv?distance_unit=km"
```

## Automated planning endpoints

After Garmin sync has imported runs, the planner reads the stored activities and creates recommendations from the latest load/readiness:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/readiness
curl -H "X-API-Key: $API_KEY" http://localhost:8000/plan/today
curl -H "X-API-Key: $API_KEY" http://localhost:8000/plan/week
```

Optional parameters:

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/plan/today?goal=base"
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/plan/week?goal=general_fitness&target_distance_km=30"
```

If `target_distance_km` is omitted, the app calculates it from your recent 4-week average and readiness status.

## Live Garmin metrics

Two endpoints fetch your current Garmin metrics live (each logs in once and reads
every metric defensively, so a metric your device does not report comes back as
`null` instead of failing the whole response). Both accept an optional
`?date=YYYY-MM-DD` (defaults to today) and return `{ "date", "metrics", "errors" }`.

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/garmin/recovery" | jq
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/garmin/fitness?date=2026-05-27" | jq
```

- `GET /garmin/recovery` → `training_readiness`, `hrv`, `sleep`, `stress`, `body_battery`, `resting_heart_rate`, `respiration`, `spo2`
- `GET /garmin/fitness` → `training_status`, `race_predictions`, `vo2max`, `endurance_score`, `hill_score`, `fitness_age`

Note: standalone `vo2max` (`get_max_metrics`) can be empty on days without a
daily sample; the current value is also available under
`training_status.mostRecentVO2Max`.

## Goal-driven adaptive plan

Define a running goal once; the app builds a periodized plan from your current
Garmin fitness and, **each morning, adapts that day's workout** to your recent
results and recovery — a rule-based engine with an **OpenAI** coaching layer on
top — then (optionally) pushes it to your watch.

Configure in `.env`:

```text
OPENAI_API_KEY=sk-...          # enables the AI coaching layer (falls back to rules if unset)
OPENAI_MODEL=gpt-4o-mini       # any OpenAI chat model
MORNING_UPDATE_TIME=06:00      # local time of the daily auto-update
TIMEZONE=Asia/Jerusalem
GOAL_AUTO_PUSH=true            # push the adapted workout to Garmin each morning
NOTIFY_CHANNEL=callmebot       # morning summary channel: none | callmebot
CALLMEBOT_PHONE=9725XXXXXXXX   # your WhatsApp number (international, no '+')
CALLMEBOT_APIKEY=123456        # from CallMeBot activation
```

**Morning summary delivery (`NOTIFY_CHANNEL`):** each morning the day's workout +
coaching note can be sent to you. Choose one of:

- **`email` (Gmail SMTP)** — recommended. Enable 2-Step Verification on your Google
  account, then create a 16-char **App Password** at
  <https://myaccount.google.com/apppasswords>. Set in `.env`:
  ```text
  NOTIFY_CHANNEL=email
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=you@gmail.com
  SMTP_PASS=xxxxxxxxxxxxxxxx     # the App Password, NOT your Google login
  EMAIL_FROM=you@gmail.com
  EMAIL_TO=you@gmail.com
  ```
- **`callmebot` (WhatsApp)** — on your phone, WhatsApp `+34 644 51 95 23`
  *"I allow callmebot to send me messages"* to get an API key, then set
  `CALLMEBOT_PHONE` / `CALLMEBOT_APIKEY`.
- **`none`** — disabled.

Test the configured channel with `POST /goal/today/notify`.

Set a goal (open-ended — train until your Garmin race-prediction meets the target):

```bash
curl -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"distance_km": 21.1, "target_time": "2:00:00"}' \
  http://localhost:8000/goal
```

Then:

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/goal?progress=true"        # goal + Garmin prediction vs target
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/goal/progress?weeks=12"    # weekly progress report (are you on pace?)
curl -H "X-API-Key: $API_KEY" http://localhost:8000/goal/week                   # 7-day picture (today firm, rest projected)
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/goal/plan?days=21"         # upcoming planned workouts
curl -H "X-API-Key: $API_KEY" http://localhost:8000/goal/today                  # today's workout (cheap read)
curl -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/goal/today/refresh  # force adaptive recompute now
curl -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/goal/today/push     # push today's workout to Garmin
curl -X DELETE -H "X-API-Key: $API_KEY" http://localhost:8000/goal              # clear the active goal
```

How it works:
- **Weekly skeleton** (rule-based): Mon easy · Tue strength/rest · Wed quality · Thu rest · Fri long · Sat recovery · Sun rest, with progressive overload and a down week every 4th.
- **Daily adaptation** at `MORNING_UPDATE_TIME`: today's session is adjusted from readiness/HRV/sleep + recent planned-vs-actual, re-balancing the current week. The OpenAI layer refines it **within safe bounds**; if `OPENAI_API_KEY` is unset or the call fails, it falls back to the deterministic rules (so the morning update never fails).
- **Storage:** SQLite tables `goal`, `training_plan`, `planned_workout` in `data/running_assistant.db`.

## Garmin workout export / push to watch

The Docker version now includes endpoints that convert the planned run into a structured Garmin workout model and can try to push it to Garmin Connect using the same Garmin credentials from `.env`.

Useful endpoints:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/workouts/today/json | jq
curl -H "X-API-Key: $API_KEY" http://localhost:8000/workouts/today/garmin-payload | jq
curl -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/workouts/today/push-to-garmin | jq
curl -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/workouts/week/export-json | jq
curl -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/workouts/week/push-to-garmin | jq
```

Exported workout JSON files are stored in `./workouts` on the host.

Notes:
- The official Garmin Training API is the reliable production-grade way to publish workouts and training plans to Garmin Connect calendars and sync them to compatible devices.
- This local project uses Garmin Connect's private web API via the community `garminconnect` session (`/workout-service/workout` to create, `/workout-service/schedule/{id}` to schedule). It is useful for personal automation, but Garmin can change the private API and break this flow.
- If workout creation succeeds but scheduling fails, open Garmin Connect, find the created workout, add it to your calendar, and sync your watch.
