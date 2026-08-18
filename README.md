# Барбершоп Бот

Telegram-бот барбершопа «Острый» (Алматы): онлайн-запись, напоминания,
отзывы, реферальная программа, лояльность, админ-панель, ежедневные бэкапы.

## Возможности

- 📅 Онлайн-запись с защитой от двойного бронирования (`UNIQUE` индекс)
- ⏰ Авто-напоминание за 24ч и завершение визитов
- 🎁 Реферальная система с начислением бонусов
- ⭐ Лояльность: каждая 5-я стрижка — скидка 10%
- 📊 Админ-панель с пагинацией (на SQL `LIMIT/OFFSET`, не в Python-памяти)
- 💾 Ежедневный бэкап БД с ротацией (30 копий)
- 🌐 Long-polling (Railway-ready) с устойчивостью к TelegramConflictError
- 🔐 Защита секретов в логах (прокси/Redis URL — маскируются)
- 🪵 Structured logging (через `LOG_JSON=1`) + request ID для корреляции
- 🪦 SIGTERM/SIGINT graceful shutdown: корректно гасит polling, БД-пул, FSM

## Быстрый старт

```bash
cp .env.example .env       # заполните BOT_TOKEN и ADMIN_IDS
docker compose up -d       # или: pip install -r requirements.txt && python bot.py
```

Для production-deploy см. **[DEPLOYMENT.md](DEPLOYMENT.md)**.
Для incidents — **[OPERATIONS.md](OPERATIONS.md)**.

## Тесты

```bash
make test                  # или:  pytest -q
make test-cov              # coverage report
make lint                  # ruff check
make format                # ruff format
make type-check            # mypy (game modules)
```

## Требования

- Python 3.10+
- SQLite (по умолчанию) или PostgreSQL 14+ (рекомендуется для production)
- Docker (опционально, но рекомендуется)

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/me` | Профиль, активные записи, бонусы, реферальная ссылка |
| `/master` | Список мастеров |
| `/waitlist` | Мой лист ожидания |
| `/cancel` | Отменить запись |
| `/help` | Справка |
| `/admin` | Панель администратора (только для `ADMIN_IDS`) |

## Структура

```
barbershop_deploy/
├── bot.py              # Точка входа + SIGTERM graceful shutdown
├── logging_config.py   # Structured JSON/human logging + request_id contextvar
├── config.py           # Настройки + env-driven DB pool
├── db.py               # DB-абстракция (SQLite/PG) + connection retry
├── storage.py          # Операции с БД (пагинация на SQL)
├── scheduler.py        # APScheduler: reminders, auto-complete, backup
├── keyboards.py        # Инлайн-клавиатуры
├── messages.py         # Текстовые шаблоны
├── middleware.py       # RequestID + RateLimit + AdminCheck
├── monitoring.py       # Health checks
├── backup.py           # Бэкап (SQLite gzip / PG SQL dump)
├── fsm_storage.py      # File-based FSM fallback
├── tz_utils.py         # TZ-aware datetime (zoneinfo, stdlib)
├── handlers/
│   ├── start.py        # /start, профиль, рефералы
│   ├── booking.py      # Полный флоу записи
│   ├── info.py         # Мастера, контакты, цены
│   └── admin.py        # Админ-панель
├── tests/              # 47 тестов, 93% покрытие
├── .github/workflows/ci.yml  # GitHub Actions (pytest × 3 Python + ruff + Docker build)
├── pyproject.toml      # Project metadata + ruff/mypy/pytest/coverage config
├── Dockerfile          # Multi-stage build для маленького итогового image
├── docker-compose.yml  # Postgres + bot стек
├── Makefile            # make test / lint / format / type-check / docker-up
├── CHANGELOG.md        # История версий
├── OPERATIONS.md       # Runbook для on-call
├── DEPLOYMENT.md       # Railway step-by-step
├── LICENSE             # MIT
└── requirements.txt
```

## Переменные окружения

Полный список — в [.env.example](.env.example) и [DEPLOYMENT.md](DEPLOYMENT.md#переменные-окружения).

## Лицензия

MIT — см. [LICENSE](LICENSE).
