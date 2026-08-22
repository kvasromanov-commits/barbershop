# Deployment Guide — Barbershop Bot

This guide covers production deployment. For architecture overview and bot user
docs, see `README.md`. For incident response, see `OPERATIONS.md`.

---

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [Environment Variables](#environment-variables)
3. [Railway Deployment](#railway-deployment)
4. [Self-hosted (Oracle Cloud / Fly.io / VPS)](#self-hosted)
5. [Database Migrations](#database-migrations)
6. [Backup & Recovery](#backup--recovery)
7. [Health & Monitoring](#health--monitoring)
8. [Production Checklist](#production-checklist)

---

## Prerequisites

- Python 3.10+ runtime (Dockerfile uses `python:3.10-slim`)
- PostgreSQL 14+ (if using Postgres backend; SQLite works for tiny installs)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Admin Telegram IDs from [@userinfobot](https://t.me/userinfobot)

---

## Environment Variables

All variables are loaded from process env / `.env` via `python-dotenv`.

### Required

| Variable | Example | Notes |
|---|---|---|
| `BOT_TOKEN` | `123:ABC-DEF…` | From @BotFather |
| `ADMIN_IDS` | `111111,222222` | Comma-separated Telegram IDs |

### Production database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | unset (→ SQLite) | `postgresql://user:pwd@host:5432/db` |
| `DB_PATH` | `/app/data/barbershop.db` | SQLite path, used only when DATABASE_URL is empty |
| `DB_POOL_MIN` | `2` | asyncpg pool min |
| `DB_POOL_MAX` | `10` | asyncpg pool max |
| `DB_COMMAND_TIMEOUT` | `30` | seconds per asyncpg statement |

### Optional

| Variable | Default | Notes |
|---|---|---|
| `TIMEZONE` | `Asia/Almaty` | IANA TZ; affects all date displays |
| `REDIS_URL` | unset | `redis://:pwd@host:6379/0` — enables Redis FSM storage (recommended for multi-replica) |
| `PROXY_URL` | unset | `http://user:pwd@host:8080` — for Telegram-blocked regions. Credentials masked in logs |
| `MIN_BOOKING_ADVANCE_MINUTES` | `60` | Earliest bookable slot |
| `MASTER_IDS` | unset | `Name=telegram_id,…` — sends booking notifications to masters |
| `LOG_JSON` | unset | `1` → structured JSON logs (for Loki/Datadog/CloudWatch); default is human-readable |

> ⚠️ Never commit a filled `.env` file. Use `.env.example` as a template and provide real secrets only through `railway variables` or your orchestrator's secrets manager.

---

## Railway Deployment

### One-time setup
1. Create a project at https://railway.app
2. **New → Service → PostgreSQL** — wait for provisioning, copy `DATABASE_URL`
3. **New → Service → GitHub Repo** — connect `ignatvanuskin-sys/barbershop-ostry`
4. Set env vars (see table above). `DATABASE_URL` is auto-injected by Railway if the bot service links to the Postgres plugin.
5. (Optional) **New → Service → Redis** — set `REDIS_URL` to its connection string.

### Deploy
```bash
git push origin main
```

Railway auto-builds the Dockerfile, runs healthcheck (`pgrep` + DB exists), starts the bot. Watch logs for:
```
db.py: PostgreSQL pool initialised
Bot started
Health: {'status': 'ok', …}
```

### Volumes that must persist
| Volume path | Purpose |
|---|---|
| `/app/data` | SQLite DB (only if no Postgres plugin) + FSM state file |
| `/app/backups` | Backup files (rotated, max 30 retained) |
| `/app/logs` | Optional log files if not streaming stdout |

Railway → service → Settings → Volumes — make sure these are mounted (if you opt to use SQLite for cost reasons). When using Postgres, only `/app/backups` needs persisting.

### Restart policy
Defined in `railway.toml`:
```toml
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 5
```
For zero-downtime deploys Railway starts new container, then stops the old. The bot itself retries `TelegramConflictError` for 120 s (in `bot.py`).

---

## Self-hosted

Works on any Linux with Docker. See `docker-compose.yml` for a full local stack
(Postgres + bot). For Oracle Cloud Free Tier / Fly.io / VPS:

1. Install Docker + docker compose plugin
2. `git clone https://github.com/ignatvanuskin-sys/barbershop-ostry.git`
3. Create `.env`, fill in real values
4. `docker compose up -d`
5. (Recommended) Place an nginx/caddy reverse proxy in front if exposing
   a webhook endpoint in the future. Polling mode currently needs no public port.

---

## Database Migrations

This codebase does **not** use Alembic. Schema is created on first start
via `storage.init_db()` and is idempotent (`CREATE TABLE IF NOT EXISTS` everywhere).
Backward-incompatible column changes use defensive `ALTER TABLE ... ADD COLUMN`
inside `_migrate_masters_columns` and friends, guarded by `information_schema` /
`PRAGMA` lookups.

**Implication for deploys**: redeploys are safe. The DB never gets dropped.
For destructive migrations, plan a maintenance window.

---

## Backup & Recovery

Automated daily backup at 03:30 Asia/Almaty (see `scheduler.py`).
Retains the latest 30 backups in `/app/backups/`.

| Backend | Format | Restore |
|---|---|---|
| SQLite | `barbershop_YYYYMMDD_HHMMSS.db.gz` | `gunzip *.db.gz && sqlite3 barbershop.db < barbershop.db` |
| PostgreSQL | `barbershop_YYYYMMDD_HHMMSS.sql.gz` | `gunzip *.sql.gz && psql "$DATABASE_URL" < barbershop_YYYY…sql` |

See `OPERATIONS.md § Disaster Recovery` for full procedure.

### Off-site backup (HIGH-1 FIX — recommended for production)

The local backup still lives on the Railway volume (kept as a fast local copy),
but a copy is also pushed to any **S3-compatible** store (AWS S3, Cloudflare R2,
Backblaze B2, MinIO). This protects against total volume loss — the previous
single point of failure.

The daily job (`scheduler.backup_database_job`) runs the local backup first, then
best-effort uploads the file with a **timestamped object key** (previous backups
are never overwritten), **verifies** the upload via `head_object`, retries with
exponential backoff on transient errors, and prunes old off-site copies beyond
`S3_BACKUP_RETENTION` (default 30). A failed or unconfigured off-site step never
deletes or breaks the local backup, and never crashes the scheduler.

Enable it by setting **all** of the following in your environment (see
`.env.example`):

| Variable | Purpose | Example |
|---|---|---|
| `S3_ENDPOINT_URL` | Custom endpoint for R2/B2/MinIO (leave empty for AWS S3) | `https://<id>.r2.cloudflarestorage.com` |
| `S3_REGION` | Bucket region (optional for R2/B2) | `auto` / `us-east-1` |
| `S3_BUCKET` | Target bucket (required to enable) | `barbershop-backups` |
| `S3_PREFIX` | Key prefix / "folder" (optional) | `prod` |
| `S3_ACCESS_KEY_ID` | Scoped, least-privilege key | — |
| `S3_SECRET_ACCESS_KEY` | Secret for the key above | — |
| `S3_BACKUP_RETENTION` | Off-site copies to keep (default 30) | `30` |

If `S3_BUCKET` / `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` are not all set, the
off-site step is **silently skipped** (dev mode). Credentials are read from the
environment only — never hardcoded, never logged.

---

## Health & Monitoring

The internal health check verifies:
- Database connectivity (`SELECT 1`)
- FSM storage (Redis ping or FileStorage JSON parse)
- APScheduler running state

Set `LOG_JSON=1` for structured logs:
```
LOG_JSON=1
```

This enables a JSON line per event ready for `fluentbit` / Loki / CloudWatch ingestion. Every record includes a `request_id` (UUIDv4 per update) so you can trace one user's journey through the handler chain.

---

## Production Checklist

Before announcing to users:

- [ ] `BOT_TOKEN` is rotated (not the development token)
- [ ] `ADMIN_IDS` includes at least one real admin
- [ ] PostgreSQL provisioned (or SQLite volume mounted)
- [ ] `REDIS_URL` set if planning horizontal scale
- [ ] `TZ` / `TIMEZONE` matches the barbershop's location
- [ ] `PROXY_URL` set if Telegram is unreachable from the host region
- [ ] `railway logs` shows clean startup, no errors in 15 min
- [ ] `/start` command works in a Telegram client
- [ ] At least one admin panel command (`/admin`) verified
- [ ] Backup file appears in `/app/backups/` after 04:00 next day
- [ ] Branch protection on `main` (PR review required; CI green)

---

## Rolling back

```bash
git revert HEAD             # safe non-fast-forward
git push origin main        # Railway auto-deploys previous-good state
```

For schema-level rollback: take a backup **before** each deploy, so you can re-import on a clean DB if needed.
