# Operations Runbook — Barbershop Bot

This document is for operators on call. Read once. Trust twice when the bot is
down at 22:00 on a Saturday.

The bot lives at: `https://github.com/ignatvanuskin-sys/barbershop-ostry`
Deploys happen on `git push origin main` → Railway auto-build → container roll.

---

## Health & Status

| What | Where |
|---|---|
| Live status | Railway dashboard → project `barbershop-ostry` |
| Logs | Railway → service → Logs (auto-rotated) |
| Container health | Railway → service → Deployments → Check event "Healthcheck passed" |
| DB health | `railway logs --filter "[db]"` — looking for `db.py: PostgreSQL pool initialised` (or `SQLite mode`) |
| Scheduler | `railway logs --filter "[scheduler]"` — APScheduler writes `Started/Added job …` on boot |

The internal `/health` JSON is exposed via `monitoring.get_health_status()`.
Call it locally: `python -c "import asyncio, monitoring; print(asyncio.run(monitoring.get_health_status()))"`.

---

## Common Incidents

### Bot not responding to messages
1. Railway dashboard → check container status. Restarted? Crashed? Memory exceeded?
2. `railway logs --tail 100` — look for `TelegramConflictError` (two polling instances) or `Traceback`.
3. If `OOMKilled`: raise RAM in service settings, or reduce schedule job frequency.
4. If `TelegramConflictError`: ensure only one container is running. Manually restart: `railway restart`.

### Database unreachable
1. If using Postgres plugin: open plugin tab, confirm green light.
2. `railway logs --filter "[db]"` — `pool initialised` should appear within 30s of start.
3. DB pool retry (added 2026-08-18) — 5 attempts, 1s→16s exponential backoff. Past that, the bot will not start; check DB provider status.
4. Manual test: `railway run psql $DATABASE_URL -c "SELECT 1"`.

### Bot restarts in a loop
1. Check exit code: `railway logs --tail 200` — last lines before restart.
2. Most common cause: unhandled exception in `main()` setup (env vars missing).
3. Verify env: `railway variables` — must include `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL` (if PG).

### Backup missing
1. `railway logs --filter "[backup]"` — daily backup at 03:30 Asia/Almaty.
2. Check `/app/backups/` on the volume: `railway run ls -la /app/backups`.
3. If zero files older than 24 h: scheduler may have crashed. Restart, then check `scheduler` job list.

### Lost FSM state (users stuck mid-flow)
1. Cause: FileStorage writes to `fsm_state.json` on `/app/data/`. Volume may be detached.
2. Inspect: `railway run cat /app/data/fsm_state.json | head`.
3. Atomic writes were added so partial loss is impossible — full file loss only.

---

## Disaster Recovery

### Restore from backup
```bash
# 1. List backups on volume
railway run ls /app/backups/

# 2. Download locally (assume DATABASE_URL is set locally too)
railway run cat /app/backups/barbershop_YYYYMMDD_HHMMSS.sql.gz > ./restore.sql.gz
gunzip restore.sql.gz

# 3. Restore into PostgreSQL (overwrites; do this only with the right backup!)
psql "$DATABASE_URL" < restore.sql
```

> ⚠️ PostgreSQL restore REPLACES the current DB. Take a fresh backup first.

### Restore from off-site backup (S3-compatible, HIGH-1 FIX)
If the Railway volume is lost or corrupted, recover from the off-site copy:
```bash
# 1. List off-site backups (AWS CLI / any S3 client). Example with aws:
aws s3 ls s3://$S3_BUCKET/$S3_PREFIX/

# 2. Download the latest backup
aws s3 cp "s3://$S3_BUCKET/$S3_PREFIX/barbershop_YYYYMMDD_HHMMSS.db.gz" ./restore.db.gz
gunzip restore.db.gz

# 3a. SQLite: point DB_PATH at the restored file (or replace /app/data/barbershop.db)
# 3b. PostgreSQL: gunzip the .sql.gz then: psql "$DATABASE_URL" < restore.sql
```
Off-site upload is best-effort and verifies each upload (`head_object`); if the
volume is gone but the bucket is intact you have a full daily history up to
`S3_BACKUP_RETENTION` copies. A failed off-site upload never deletes the local copy.

### Rotate BOT_TOKEN (suspected leak)
1. In Telegram: contact @BotFather → `/revoke` → get new token.
2. `railway variables` → set new `BOT_TOKEN` value → save. Service will redeploy.
3. Old token stops working in < 60 s.

### Rotate DATABASE_URL password
1. Rotate provider password.
2. Get new connection string → update `DATABASE_URL` in Railway.
3. Redeploy triggers reconnect.

---

## Observability Cheat Sheet

| Need | Where to look |
|---|---|
| Find one user journey | `grep <request_id> railway.log` (set `LOG_JSON=1` to get IDs) |
| Count bookings/day | `SELECT COUNT(*), date FROM bookings GROUP BY date;` against DB |
| Total bookings | `SELECT COUNT(*) FROM bookings;` |
| Active bots polling | Railway shows `numReplicas=1` in `railway.toml`; check that only one is "Running" |
| Last error | `grep -i "global error" railway.log \| tail` |

---

## On-Call Checklist (rotate weekly)

- [ ] `railway logs` — no errors in the last 7 days
- [ ] `/app/backups/` — at least 6 daily backups exist
- [ ] `GET /health` (via `python -c …` inside the container) returns `status: ok`
- [ ] No PR open in repo with failing CI
- [ ] Dependabot PRs checked — no critical-severity `requirements.txt` updates older than 14 days
- [ ] DB free disk space > 20% (Postgres plugin tab)
- [ ] ADMIN_IDS is not empty (bot warns at startup if it is)

---

## When All Else Fails

Roll back to last good commit:
```bash
git revert HEAD            # creates a new commit reverting the latest change
git push origin main       # Railway auto-deploys
```

If `revert` is not enough (DB schema drift):
```bash
# Mark migrations as done if you wrote them since.
# For this codebase: storage.init_db() is idempotent — restart is safe.
```

If restart fails repeatedly: SSH into the volume, dump DB, recreate service, restore.
