# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — High-production readiness (2026-08-18)
- GitHub Actions CI pipeline (`.github/workflows/ci.yml`) running pytest on Python 3.10–3.12 + ruff lint + format check + Docker build smoke test.
- Dependabot weekly updates for pip and GitHub Actions.
- `.editorconfig`, `.pre-commit-config.yaml`, `pyproject.toml` (project metadata + ruff/mypy/pytest/coverage config).
- `LICENSE` (MIT) and `Makefile` (common dev commands).
- `OPERATIONS.md` incident runbook and `DEPLOYMENT.md` Railway deploy guide.
- `logging_config.py` with structured (JSON) formatter and per-request correlation ID via `contextvars`.
- SIGTERM/SIGINT graceful shutdown handler in `bot.py`: drains polling loop, closes asyncpg pool, flushes FSM state.
- `RequestIDMiddleware` — assigns a UUID to every incoming update and logs/scopes it across the handler chain.
- PostgreSQL connection retry with exponential backoff in `db.init_pool()` (5 attempts, 1s → 16s).

### Changed
- Replaced plain `logging.basicConfig` with structured logging in `bot.py`. Default format stays human-readable; set `LOG_JSON=1` for JSON output suited to Datadog/Loki/CloudWatch.
- `tz_utils.py` now uses `zoneinfo` (stdlib) instead of `pytz` to match `scheduler.py`.

### Removed
- `storage_sqlite_backup.py` (45 KB) — superseded by `backup.py`. Was already excluded from coverage, `.gitignore`, and `.dockerignore`.
- `migrate_db.py` — superseded by `storage.init_db()` which runs on startup.
- `pytz==2024.1` from `requirements.txt` (replaced by stdlib `zoneinfo` + `tzdata`).

## [1.0.0] — 2026-08-18 — Production Readiness Pass 1

### Added — P0/P1 fixes
- `bot.py` — mask proxy URL credentials before logging (was `PROXY_URL` plain-text leak).
- `bot.py` — typos "Попрбуйте/поже" → "Попробуйте позже".
- `Dockerfile` + `docker-compose.yml` — robust healthcheck (`pgrep` + DB exists).
- `storage.py` — five new `_paged` functions (`get_all_bookings_paged`, `export_bookings_csv_paged`, `get_reviews_paged`, `get_loyalty_list_paged`, `get_referrals_paged`). Pagination moved from Python-slice to SQL `LIMIT/OFFSET` to avoid OOM on large tables.
- `handlers/admin.py` — loyalty/referrals/admin-reviews now show "X из Y всего" + pagination buttons.
- `config.py` — env-configurable DB pool (`DB_POOL_MIN/MAX/COMMAND_TIMEOUT`).
- `db.py` — asyncpg pool reads these settings from config, logs them.
- `monitoring.py` — replaced deprecated `aioredis` with `redis.asyncio` (aiogram 3.7 already depends on redis>=4.6).
- `requirements.txt` — removed deprecated `aioredis==2.0.1`.
- `handlers/start.py` — removed duplicate `callback.answer()` in `cb_invite_friend`.

### Security
- 93% test coverage maintained (47 test files, ~10 k LOC).
- HTML escape on all user-controlled fields in scheduler/admin messages.

[Unreleased]: https://github.com/ignatvanuskin-sys/barbershop-ostry/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ignatvanuskin-sys/barbershop-ostry/releases/tag/v1.0.0
