# Предпродакшен-аудит: Barbershop «Острый» бот

**Дата:** 2026-08-22
**Статус ветки:** `main` @ `160c00b` (HIGH-1 закрыт: off-site S3-бэкап реализован; lint-clean, CI-green, push выполнен)
**Охват:** секреты/конфиг, безопасность, целостность БД, отказоустойчивость, деплой/CI, бэкапы, наблюдаемость, соответствие (PII/GDPR).

---

## TL;DR

Код в **хорошем состоянии** для запуска в продакшен малого масштаба: секреты не утекают, SQL-инъекций нет, админ-зона закрыта на уровне роутера (`AdminFilter`), CI зелёный, graceful shutdown и восстановление задач из БД реализованы.

**Единственный блокирующий риск для жёсткого продакшена — отсутствие off-site бэкапов (бэкапы лежали на том же Railway-волюме, что и БД) — ЗАКРЫТ.** Добавлен модуль `s3_backup.py`: ежедневный бэкап теперь заливается в S3-совместимое хранилище (AWS S3 / Cloudflare R2 / Backblaze B2) с timestamped-ключом, верификацией загрузки (`head_object`), retry/backoff и retention-очисткой. Локальный бэкап сохраняется как быстрая копия; временный сбой S3 не ломает job и не удаляет локальный файл.

Остальное — MEDIUM/LOW находки, которые желательно закрыть до/после деплоя, но они не являются hard-блокерами для бота барбершопа.

**Оценка готовности:** 🟢 Готов к запуску в продакшен. HIGH-1 закрыт. Рекомендуется использовать PostgreSQL в проде (MED-1) и добавить `/export_data` + `/delete_me` (MED-2) для PII-соответствия.

---

## Сводная таблица

| Раздел | Статус | Ключевые находки |
|---|---|---|
| Секреты и конфигурация | ✅ OK | BOT_TOKEN не логируется; прокси/Redis-URL маскируются; `.env` в .gitignore |
| Безопасность (auth) | ✅ OK | `AdminFilter` на уровне роутера; per-handler guards удалены как dead code |
| SQL-инъекции | ✅ OK | 100% параметризованные запросы; нет `eval/exec/subprocess/pickle` |
| XSS / HTML-инъекции | ✅ OK | `html.escape` на всех user-controlled полях в HTML-сообщениях |
| Целостность БД / транзакции | 🟡 Замечание | SQLite: новое соединение на каждый `acquire()` → риск «database is locked» |
| Отказоустойчивость | ✅ OK | retry PG-pool, graceful shutdown, TelegramConflictError retry ×24 |
| Деплой / Docker | 🟡 Замечание | контейнер под root; `POSTGRES_PASSWORD: changeme` в compose |
| CI/CD | ✅ OK | матрица 3.10–3.12, pytest+coverage, ruff, docker-smoke |
| Бэкапы | 🟢 Закрыто (HIGH-1) | off-site S3-бэкап реализован (`s3_backup.py`) |
| Наблюдаемость | 🟡 Замечание | нет HTTP `/healthz`; healthcheck только по процессу |
| PII / GDPR | 🟡 Замечание | нет `/export_data` и `/delete_me` |
| callback_data (Telegram ≤64B) | 🟡 Замечание | 3 инлайн-f-string в admin.py могут превысить лимит при длинных именах |

---

## Детальные находки

### 1. Секреты и конфигурация — ✅ OK

- `config.py`: `BOT_TOKEN`, `ADMIN_IDS`, `MASTER_IDS`, `DATABASE_URL`, `REDIS_URL`, `PROXY_URL` — все из env, **без хардкода**.
- **Нигде `BOT_TOKEN` не попадает в логи.** При отсутствии токена логируется только факт (`BOT_TOKEN is not set`).
- `bot.py:93-103` — proxy URL маскируется перед логированием (`user:****@host`).
- `bot.py:115-122` — Redis URL маскируется перед логированием.
- `.gitignore` корректно исключает `.env`, `data/*.db`, `backups/`, `logs/`, `*.log`.
- Проверено: в git **не закоммичено** ни `.env`, ни `.db`, ни `.gz`, ни `.bak`. Только `.env.example` (без секретов).
- `docker-compose.yml` и `railway.toml` не содержат секретов в тексте.

**Мелочь (LOW):** `.env.example` не документирует `LOG_LEVEL`, `LOG_JSON`, `GLOBAL_CONCURRENCY`, `DB_POOL_MIN/MAX/COMMAND_TIMEOUT`. Добавить для полноты.

---

### 2. Безопасность — ✅ OK

- **Авторизация админки:** `handlers/admin.py:45-46` — `router.message.filter(AdminFilter())` + `router.callback_query.filter(AdminFilter())`. `AdminFilter` проверяет `user.id in config.ADMIN_IDS`. Все админ-хендлеры зарегистрированы только на `admin_router` (bot.py подключает роутеры, нет `@dp.message(Command("admin"))` напрямую). Одиночная забытая проверка `_is_admin` больше не может дать доступ — это правильно закрывает MED-7.
- **SQL-инъекции:** все запросы в `storage.py`/`db.py` параметризованы (`?`/`$1`). Единственное место с динамическими идентификаторами — `db.upsert()` (имя таблицы/колонок через f-string), но **все вызовы передают константные имена** (`"settings"`, `["key"]`, `"masters"` и т.п.). User-controlled идентификаторов нет → инъекции нет.
- **Опасные паттерны:** grep по `eval/exec/os.system/subprocess/shell=True/pickle/yaml.load` — **только в test-файле** (`__import__` в `skipif`). В прод-коде отсутствуют.
- **HTML-inъекции (Telegram XSS):** все user-controlled поля (имя клиента, мастер, услуга, комментарии) обёрнуты в `html.escape(...)` перед `parse_mode="HTML"` (проверено в `scheduler.py`, `handlers/*`). В `admin.py` есть `is_safe_entity_name()` — отсекает `<>"\`` и переносы строк.
- **Дедуп-атаки / спам:** `RateLimitMiddleware` (20 req/60s на юзера, ×5 для админов) + `GlobalConcurrencyMiddleware` (семафор 64). Адекватно.

---

### 3. Целостность БД и транзакции — 🟡 SQLite-замечание

Позитив:
- Атомарные `UPDATE loyalty SET visits=visits+1` / `bonuses=bonuses-? WHERE bonuses>=?` — гонки исключены (HIGH-2, BUG-3 исправлены).
- `save_booking` + списание бонусов в **одной транзакции** (`conn.transaction()`), откат при конфликте слота.
- `cleanup_slot_locks_on_startup` удаляет только **просроченные** локи (исправлено HIGH-1).
- Пагинация на уровне SQL (HIGH-003/P1), нет полных сканов в Python.

**Риск (MEDIUM):** `db.py:185-197` — `acquire()` для SQLite открывает **новое соединение** (`aiosqlite.connect`) на каждый вызов. Под нагрузкой (`GLOBAL_CONCURRENCY=64` + rate-limit 20/юзер) одновременно могут висеть десятки соединений к одному файлу, и каждая транзакция — `BEGIN EXCLUSIVE` (`db.py:176`). На контейнерных/сетевых волюмах это даёт **«database is locked»** и упавшие записи.

**Рекомендация:** для продакшена **использовать PostgreSQL** через `DATABASE_URL` (пул уже корректно настроен: retry 5×, `DB_POOL_MIN/MAX`). SQLite оставить для dev/малого трафика, но в `init_sqlite` добавить `PRAGMA busy_timeout=5000` и рассмотреть единое shared-соединение вместо per-request.

---

### 4. Отказоустойчивость — ✅ OK

- `db.init_pool()`: 5 попыток реконнекта к PG с экспоненциальной задержкой (1→16s) — переживает zero-downtime деплой Railway.
- `bot.py` graceful shutdown: `SIGTERM/SIGINT` → drain polling → close scheduler/FSM/DB pool/bot session.
- `TelegramConflictError` retry до 24×5s (Railway поднимает 2 контейнера при деплое) — конфликт поллинга переживается.
- `check_webhook_conflict()` — уведомляет админов, если висит внешний webhook.
- Scheduler: восстановление задач (reminders/auto-complete/review) из `scheduler_jobs` при старте — не теряются при рестарте.

---

### 5. Деплой / Docker — 🟡 Замечания

- `Dockerfile`: многоступенчатая сборка, slim-образ, UTF-8 env, `HEALTHCHECK` (процесс + наличие БД). Хорошо.
- **(LOW) Root-пользователь:** контейнер работает под `root` (осознанно — Railway монтирует волюм как root). Для k8s/Fly.io рекомендуется не-root пользователь. Документировано в Dockerfile.
- **(LOW) `docker-compose.yml:11`:** `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme}` — слабый пароль по умолчанию. Только для локального dev (в Railway используется `DATABASE_URL`). Документировать, что в проде пароль обязателен.
- `railway.toml`: `numReplicas=1` (верно для поллинга), `restartPolicyType=ON_FAILURE`, 5 ретраев — ок.

---

### 6. Бэкапы — 🔴 HIGH (главный риск)

- `backup.py`: ежедневный бэкап в 3:30 (cron в scheduler) + ротация (30 копий). Для PG — SQL-dump через asyncpg; для SQLite — `gzip`-копия файла.
- **(HIGH) Бэкапы пишутся в `backups/` — тот же Railway-волюм, что и `/app/data` с БД.** Потеря волюма (редко, но бывает) = потеря БД **и** всех бэкапов одновременно.
- **(LOW) SQLite-бэкап копирует raw-файл БД** через `shutil.copyfileobj`. При активной записи снапшот может быть неконсистентным (WAL-файл не включается). Лучше `VACUUM INTO` или `sqlite3 .backup`.
- **(LOW) `pg_dump` в `backup.py`** собирает INSERT-ы ручным экранированием (`str(v).replace("'","''")`) — хрупко для спецсимволов. Для продакшена надёжнее нативный `pg_dump` CLI или `COPY`. Риск низкий (свои данные, дамп не исполняется от user-input).

**Рекомендация (блокер жёсткого продакшена) — ЗАКРЫТО (2026-08-22):** реализован off-site бэкап через `s3_backup.py`: ежедневный дамп заливается в S3/B2/R2 с timestamped-ключом, верификацией `head_object`, retry/backoff и retention-очисткой (см. `DEPLOYMENT.md` § Backup & Recovery и `OPERATIONS.md` § Disaster Recovery). Единственный критический риск закрыт.

---

### 7. Наблюдаемость — 🟡 Замечание

- `logging_config.py`: структурированные логи (human + JSON через `LOG_JSON=1`), `request_id` через ContextVar для корреляции. Хорошо.
- `monitoring.py`: `get_health_status()` проверяет DB / storage / scheduler. Но **HTTP `/healthz` endpoint отсутствует** — оркестратор полагается только на `pgrep -f "python bot.py"`. Для polling-бота этого достаточно, но при миграции на webhook/HTTP стоит добавить порт здоровья.
- **(LOW) `global_error_handler` шлёт полные traceback в Telegram админам** (обрезано до 3000 символов). Может содержать PII из аргументов исключения. Admin-only, приемлемо, но стоит иметь в виду.

---

### 8. PII / GDPR — 🟡 Замечание (MEDIUM)

Хранятся: `users.phone`, `users.first_name`, `users.username`, `bookings.name`, `loyalty`, `referrals`.
- **Нет эндпоинтов `/export_data` и `/delete_me`** (отмечено в TODO проекта). Для продакшена с реальными клиентами это пробел соответствия (право на забвение / экспорт данных).
- Рекомендация: добавить admin-команды выгрузки/удаления профиля по Telegram ID (с подтверждением).

---

### 9. callback_data (лимит Telegram ≤ 64 байт) — 🟡 Замечание

`keyboards.py:_safe_cb()` корректно обрезает до 62 байт. **Но в `handlers/admin.py` несколько инлайн `callback_data` собираются через f-string БЕЗ `_safe_cb`:**

| Callback | Префикс (байт) | Max value | Итог при 40-байт имени | Риск |
|---|---|---|---|---|
| `admin_confirm_remove_service:{service_name}` | 29 | 40 | **69** | 🔴 переполнение |
| `edit_mprice:{master}:{svc}` | 11 + `:` | 40+40 | **~92** | 🔴 переполнение |
| `del_mprice:{master}:{svc}` | 11 + `:` | 40+40 | **~92** | 🔴 переполнение |
| `admin_service_detail:{service_name}` | 22 | 40 | 62 | 🟢 вписывается |
| `admin_master_detail:{master}` | 20 | 40 | 60 | 🟢 |
| `toggle_service:{svc}` | 15 | 40 | 55 | 🟢 |

`is_safe_entity_name()` допускает имена до **40 байт**. С текущими данными (имена ≤~34 байт, мастера ≤~14) переполнения **не происходит на практике**, но оно возможно при добавлении длинного имени услуги (≥36 байт) или длинной пары мастер+услуга. Тогда Telegram отвергнет кнопку → она «не нажимается».

**Рекомендация:** либо понизить `max_bytes` в `is_safe_entity_name` до значения, гарантирующего `prefix+value ≤ 64` для самого длинного префикса (для двухимённых колбэков это ~24 байта на имя — довольно коротко), либо (лучше) привязывать эти колбэки к короткому стабильному ID/слагу вместо человекочитаемого имени. Обёртка в `_safe_cb` **сама по себе недостаточна** — обрезанное имя не совпадёт при разборе колбэка.

---

## Приоритетный список (что закрыть до продакшена)

| # | Приоритет | Находка | Действие |
|---|---|---|---|
| HIGH-1 | 🟢 Закрыто (2026-08-22) | Бэкапы на том же волюме, нет off-site → реализован off-site S3-бэкап (`s3_backup.py`) | Заливать бэкапы в S3/B2 или включить Volume Snapshots — СДЕЛАНО |
| MED-1 | 🟡 Medium | SQLite per-request соединения → «database is locked» | Использовать PostgreSQL в проде (`DATABASE_URL`); для SQLite добавить `busy_timeout` |
| MED-2 | 🟡 Medium | Нет `/export_data` / `/delete_me` (PII) | Добавить admin-команды выгрузки/удаления профиля |
| LOW-1 | 🟡 Low/Med | callback_data переполнение для длинных имён | Короткие ID/слаги в колбэках или понижение `max_bytes` |
| LOW-2 | 🟡 Low | `docker-compose` `POSTGRES_PASSWORD: changeme` | Документировать обязательность пароля в проде |
| LOW-3 | 🟡 Low | Контейнер под root | Не-root при миграции на k8s/Fly |
| LOW-4 | 🟡 Low | ruff не зафиксирован в CI | `pip install ruff==0.16.4` в CI lint-шаге |
| LOW-5 | 🟡 Low | `.env.example` неполный | Добавить `LOG_LEVEL/LOG_JSON/GLOBAL_CONCURRENCY/DB_POOL_*` |
| LOW-6 | 🟡 Low | SQLite-бэкап копирует raw-файл | Использовать `VACUUM INTO` / `sqlite3 .backup` |
| LOW-7 | 🟡 Low | Traceback в Telegram админам (возможна PII) | Санитайзить аргументы исключений перед отправкой |

---

## Чек-лист перед деплоем (дополнение к DEPLOYMENT.md)

- [x] **HIGH-1 (ЗАКРЫТО 2026-08-22):** реализован off-site S3-бэкап (`s3_backup.py`); настройте `S3_BUCKET`/`S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` в Railway variables для активации
- [ ] `BOT_TOKEN` — production-токен (не dev), ротирован
- [ ] `DATABASE_URL` задан (PostgreSQL) — рекомендуется для продакшена
- [ ] `ADMIN_IDS` заполнен (иначе админка недоступна — есть warning в логах)
- [ ] `RAILWAY` variables содержат все секреты (не в git)
- [ ] `LOG_JSON=1` + forwarding в Loki/CloudWatch настроен (опц.)
- [ ] Закрыт MED-2 (GDPR) — если применимо к юрисдикции
- [ ] Прогнан `ruff check . && ruff format --check .` локально (зелёный)
- [ ] Прогнан `pytest` (baseline: 1018 passed, 3 skipped)
- [ ] docker-smoke build проходит в CI

---

## Итог

Ядро приложения **безопасно и стабильно**: нет утечек секретов, SQLi, XSS; админ-доступ корректно изолирован; CI зелёный; обработка сбоев и восстановление задач реализованы. Для запуска в продакшен малого барбершопа бот **готов** после закрытия **HIGH-1 (off-site бэкап)**. Остальные пункты — улучшения надёжности и комплаенса, которые можно закрывать итеративно после старта.
