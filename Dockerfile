# ======================================================================
# Stage 1 — builder: install Python deps into a clean venv
# ----------------------------------------------------------------------
# Multi-stage keeps the runtime image lean (~150 MB vs ~900 MB for full
# pip + gcc + build tools). Wheels only — no source compilation needed.
# ======================================================================
FROM python:3.10-slim AS builder

WORKDIR /build

# System deps for any wheel that needs them (kept minimal: most pure-Python
# packages ship Windows-compatible wheels too). We add them only in builder
# so they don't bloat the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ======================================================================
# Stage 2 — runtime: copy the venv + project code, set UTF-8 + healthcheck
# ======================================================================
FROM python:3.10-slim

# UTF-8 everywhere (default in 3.10+ but explicit for safety)
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime-only system deps (none required for our pip wheels, but `tzdata`
# is bundled via pytz replacement tzdata==2024.2 — see requirements.txt).
# If your slim image dropped tz metadata, install tzdata package at runtime
# too: `apt-get install -y --no-install-recommends tzdata`
# — but we use the Python `tzdata` wheel so no OS package required.

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Railway volume paths — created at build time, entrypoint fixes perms at start
RUN mkdir -p /app/data /app/backups /app/logs

COPY . .
RUN chmod +x /app/entrypoint.sh

# Healthcheck: process must be alive AND the DB file (or PG env var) must exist.
# Real liveness + readiness probe — detects mid-init crashes that a simple
# exec-check would miss.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD pgrep -f "python bot.py" > /dev/null \
    && python -c "import os, sys; db=os.getenv('DB_PATH','/app/data/barbershop.db'); sys.exit(0 if os.path.exists(db) or os.getenv('DATABASE_URL') else 1)" \
    || exit 1

# NOTE: Running as root intentionally.
# Railway mounts volumes as root:root 755. A non-root user cannot write to them
# without explicit chown on every volume mount. If you migrate to a host that
# supports non-root (Docker Swarm, k8s, Fly.io with launch command), add:
#   RUN useradd -r -u 1000 bot
#   USER bot
# (and stop using SQLite in favour of PostgreSQL — SQLite under non-root with
# mounted volumes is fragile.)
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "bot.py"]
