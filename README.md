# PACER — Personal Adaptive Coach

Self-hosted FastAPI service that turns Garmin Connect data into an adaptive
running plan, scores readiness, and pushes today's workout to your watch each
morning. OpenAI does the daily coach-layer adaptation; a deterministic
rule-based engine is the fallback.

The dashboard (single-file HTML at `/`) shows today's plan, the phased
Training Roadmap (base → build → peak → taper), last run's AI coach review,
recovery + fitness, goal progress with an LLM-driven ETA, and a chat with the
coach that can also propose plan changes (applied only after you confirm).
The header settings panel (gear icon on mobile) holds the coach model,
notification channel, API key / magic link, and the Garmin sync status +
manual "Sync now".

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
| `MORNING_RETRY_MINUTES` | Gap between retries while waiting for the watch to sync recovery data (default `20`) |
| `MORNING_RETRY_UNTIL` | Local HH:MM to stop waiting and adapt on training load alone (default `09:00`) |
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
APScheduler job runs (while the goal is paused, the adapt/notify/push steps
are skipped — with auto-resume on the day after `pause_until`):

1. **Freshness gate first.** `morning_metrics()` reads this morning's Garmin
   recovery metrics and checks the calendar stamp on each post-sleep signal
   (training readiness, sleep, HRV). Unless at least one is confirmed to be
   *today's*, the job stops here and reschedules itself every
   `MORNING_RETRY_MINUTES` until `MORNING_RETRY_UNTIL` — nothing below runs, so
   no email is sent, nothing is written, and a workout already on your watch is
   left alone. Stale, undatable and missing metrics are dropped rather than
   scored: readiness cannot tell yesterday's sleep score from today's, so a
   two-day-old bad night must not cancel today's session.
2. `run_garmin_sync(days=45, notify_analysis=False)` — pulls latest activities;
   run-review analyses on new activities are saved to the DB but NOT emailed
   (to avoid double-emails seconds before the morning summary)
3. `record_snapshot(goal)` — captures today's Garmin race-prediction
4. `adapt_today(recovery=…)` — scores readiness from the verified metrics plus
   training load, calls the OpenAI coach with safe bounds, falls back to rules;
   writes the adapted workout to `planned_workout`. A day already marked
   completed is returned untouched.
5. `send_morning_summary(workout)` — emails the day's session name, prescription
   and coach note (via Gmail SMTP, CallMeBot WhatsApp, or skipped per
   `NOTIFY_CHANNEL`)
6. Auto-pushes the adapted workout to Garmin (skipped on rest days). On
   schedule failure, writes a `sync_log("warn", …)` row and does NOT mark
   pushed — so next run retries

At the cutoff the job proceeds regardless and the coach note names the actual
cause — watch not synced, data from an earlier day, unconfirmable, or Garmin
refusing the requests (which needs re-authorising, not a re-sync). The retry
chain is also capped at whatever fits the window, with an absolute ceiling of 48
attempts so a misconfigured interval can't loop.

Retries live only in memory, so once the routine completes it records the date in
`app_config`; a restart inside the retry window with that marker unset schedules a
one-off catch-up. The catch-up is bounded by the same cutoff and skips completed
days, so an evening deploy can't send a "this morning" email or schedule a
workout for a day you've already run.

Readiness is only physiological when verified metrics are present; otherwise the
score reflects training load alone and both the dashboard and the coach note say
so. The dashboard's **Check my condition** button (`POST /goal/today/recheck`)
re-reads the metrics on demand, re-adapts and re-pushes — use it when your watch
syncs late or you run in the evening.

**Only today is ever pushed to the watch.** The 7-day panel is a projection you
look at; each day is finalized and pushed on its own morning. (`POST
/goal/week/push` can push a range on demand, but nothing calls it — there is no
button and the scheduler never uses it.)

### What the coach may change

The OpenAI layer is bounded by the rules, not trusted over them. It can never
prescribe a **harder** session than the rules did, and on a morning readiness
didn't flag it cannot change the session type at all — only tune volume within a
band.

| Bound | Readiness green (nothing eased) | Volume eased (yellow, type kept) | Type eased (yellow demotion / red) |
|---|---|---|---|
| `allowed_kinds` | the planned kind only, plus `long` if a long run was missed | `rest` or the planned kind — shorten it or call the day off, but don't swap it | only kinds at or below the eased one; `['rest']` alone on red |
| `min_distance_km` | 70% of planned — shortened, not cancelled | `0` | `0` |
| `max_distance_km` | 110% of planned, raised to this week's long-run distance when a missed long run is offered; the plan's own distance during taper | the eased distance | the eased distance |
| pace | always the plan's pace for the chosen kind | same | same |

Kinds are ranked `rest < recovery < easy < long < quality`, and when readiness
changes the type that ranking becomes the ceiling. So a peak-phase interval
session becomes an easy run on a yellow morning and full rest on a red one, and
the coach can go further down but not back up. A base-phase strides day is
different: it already runs at easy pace, so readiness trims its volume without
changing its type, and the type stays locked.

The pace is never taken from the coach — it is the prescription for a kind, and
accepting it separately let an "easy" day be run at threshold. A zero distance on
a run kind is normalised to `rest` rather than stored as an incoherent
"quality, 0 km", which every push path would have treated as a rest day anyway.

Anything the bounds reject is replaced by the plan's value, and the coach's prose
and note are replaced along with it, so the stored day never describes a change
that didn't happen. Every rejection is logged. A coach that thinks you shouldn't
train says so in the note; cancelling a session requires readiness to justify it.

A failure reading the metrics (step 1) or adapting (step 4) writes
`sync_log("error", …)`, surfacing it in `/sync/status` and in the dashboard's
settings-panel sync status.

## AI features

### Run review on sync

When `run_garmin_sync` ingests new running activities, `analyze_new_runs_and_notify`
queues them through the OpenAI coach for a per-activity professional summary.
The review is judged against the **training plan**, not just the race goal:
the coach gets the day's planned session (kind/distance/target pace/details),
the plan's pace map, the current phase position, and the next 7 planned days
(with natural labels — "tomorrow", "Wednesday"). So an easy run is compared to
its easy target instead of being called slow against race pace, and the
takeaway names the athlete's actual next session. Runs on days with no planned
workout are flagged as unplanned and assessed on their own merits. The
analysis row is **saved BEFORE the email is sent** so the once-per-activity
guarantee holds even if SMTP retries. The dashboard's *Last Run · AI Coach
Review* card shows the most recent one.

### Goal ETA

`estimate_eta(goal)` calls the OpenAI coach with: goal, race-prediction
history, current training plan, last 10 runs, training consistency over 28
days, load-based readiness, and a live Garmin recovery+fitness snapshot. The
coach returns `{estimated_date, on_pace, weeks_remaining, explanation}` —
explicitly instructed to cross-check Garmin predictions against the other
signals (Garmin tends to be optimistic for longer distances). Cached 6h in
the `app_config` table; `POST /goal` bypasses the cache.

### Coach chat (with plan edits)

`POST /goal/coach/ask` — free-form question, grounded in your goal, phase
roadmap position, today's workout, the upcoming 14 days, recent results and
readiness, plus the persisted chat history (last ~25 exchanges, stored in
`coach_message`). When you clearly ask for a plan change ("make today easier",
"move my long run to Sunday"), the coach returns a `proposed_change`
(adjust_day / rest_day / swap_days) which the dashboard renders as a
confirm card; `POST /goal/coach/apply` validates it (future dates only,
bounded kinds/distances) and re-pushes affected days to Garmin.

### Pause / resume

`POST /goal/pause` (optional reason + auto-resume date) skips the morning
adapt + auto-push while keeping snapshots for a continuous trend;
`POST /goal/resume` shifts the plan's start date by the paused days and
re-materializes from today.

### Settings panel (runtime switching)

The dashboard's settings panel (header; gear toggle on mobile) has:
- **Model** (`/openai/models` lists curated common models + custom) — changes
  the active OpenAI model, persisted to the `app_config` table, no restart
- **Notify channel** (`/config/notify`) — same idea for the notification channel
- **Garmin sync** — compact status (auto-sync on/off · interval · last result)
  plus a manual "Sync now" button

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
| GET | `/readiness` | `live=false` | Readiness score/status from stored load (acute vs. 4-week); `live=true` folds in this morning's Garmin recovery metrics. |
| GET | `/plan/today` | `goal=general_fitness`, `today` | Today's recommended session. |
| GET | `/plan/week` | `target_distance_km`, `goal`, `start_date` | 7-day plan. |
| GET | `/assistant/context` | — | Readiness + recent runs + today & week plans bundled. |

### Goal-driven adaptive plan

| Method | Path | Parameters | Description |
|---|---|---|---|
| POST | `/goal` | `{distance_km, target_time, race_date?}` | Set goal; build + store plan from current fitness; return ETA. Optional `race_date` (ISO, ≥4 weeks out) anchors the phased roadmap. |
| GET | `/goal` | `progress=false` | Active goal incl. race date, phase position and pause state; `?progress=true` adds Garmin race-prediction. |
| DELETE | `/goal` | — | Deactivate the active goal. |
| GET | `/goal/phases` | — | Long-term roadmap: each phase with dates, volume range, status + current position. |
| GET | `/goal/week` | — | 7-day picture: today firm, projected days 2–7. |
| GET | `/goal/plan` | `days=21` | Upcoming planned workouts. |
| GET | `/goal/progress` | `weeks=12` | Garmin race-prediction trend vs target. |
| GET | `/goal/stats` | `days=30` | Consistency (% of run days completed + streak). |
| GET | `/goal/eta` | `fresh=false` | LLM completion-date estimate + explanation (`?fresh=true` bypasses 6h cache). |
| POST | `/goal/coach/ask` | `{question}` | Ask the coach; may return a `proposed_change` for confirmation. |
| POST | `/goal/coach/apply` | `{change}` | Apply a confirmed coach-proposed change (bounded; re-pushes Garmin days). |
| GET | `/goal/coach/history` | `limit=50` | Persisted coach chat, oldest first. |
| DELETE | `/goal/coach/history` | — | Clear the persisted coach chat. |
| POST | `/goal/pause` | `{reason?, until?}` | Pause morning adapt + auto-push; optional auto-resume date. |
| POST | `/goal/resume` | — | Resume: shift plan start by paused days, re-materialize from today. |
| GET | `/goal/today` | — | Today's adapted workout (cheap DB read). |
| POST | `/goal/today/refresh` | `live=true` | Force the adaptive recompute now. |
| POST | `/goal/today/recheck` | — | Re-read this morning's recovery metrics, re-adapt today from them, and push the result to the watch. Reports `metrics_freshness`. |
| POST | `/goal/today/push` | — | Push today's workout to Garmin (skips rest days). |
| POST | `/goal/today/notify` | — | Send today's workout via the configured channel. |
| POST | `/goal/week/push` | `days=7`, `force=false` | Push next N days to Garmin; `force=true` deletes old then re-pushes. API-only — no UI, and the morning job never calls it. |

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

Quality days are defined once in `goal_planner.QUALITY_SHAPES`: the prose the
athlete reads, the day's target pace and the steps pushed to the watch are all
derived from the same entry, so they cannot describe different workouts. The
chosen shape is stored in `planned_workout.structure` when the day is written and
read by every push path via `structured_workout_for_planned_row`.

| structure | Phase | Steps pushed |
|---|---|---|
| `strides` | base | Planned distance **minus the timed tail** at easy pace → 6 × (20s stride + 60s jog, by feel) → 5 min cool-down |
| `tempo` | build | 10 min warm-up → 3 × (8 min near goal pace + 3 min jog) → 10 min cool-down |
| `intervals` | peak | 10 min warm-up → 4 × (6 min at goal pace + 3 min jog) → 10 min cool-down |
| `sharpener` | taper | 10 min warm-up → 2 × (5 min at goal pace + 3 min jog) → 10 min cool-down |
| `NULL` | easy / long / recovery / rest, and any quality day whose phase can't be resolved | One distance step at the day's target pace (no steps at all for rest) |

A base-phase quality day is not a hard session — the running is at easy pace and
only the strides are fast — so it takes the easy pace, and readiness-based easing
trims its volume rather than demoting it.

Storing the shape rather than re-deriving the phase at push time is deliberate: a
failed phase lookup would otherwise decide the session silently. Rows written
before the column existed fall back through `row_structure`, which returns `NULL`
(a plain steady run) and logs when the phase can't be resolved — degrading toward
the easier session rather than inventing intervals.

### Underlying Garmin data available (not yet exposed)

The `garminconnect` session can read much more than the endpoints above
expose. See [the project's `app/garmin_client.py`](app/garmin_client.py)
for the underlying methods.
