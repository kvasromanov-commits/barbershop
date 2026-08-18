.PHONY: help install install-dev test test-cov lint format type-check clean run docker-build docker-up docker-down docker-logs backup backup-restore pre-commit-check

PYTHON ?= python3
PIP    ?= pip3

help:
	@echo "Targets:"
	@echo "  install       - install runtime deps"
	@echo "  install-dev   - install dev/test deps"
	@echo "  test          - run pytest"
	@echo "  test-cov      - run pytest with coverage report"
	@echo "  lint          - run ruff check"
	@echo "  format        - apply ruff formatting"
	@echo "  type-check    - run mypy on key modules"
	@echo "  clean         - remove build artifacts"
	@echo "  run           - start the bot locally (loads .env)"
	@echo "  docker-build  - build the production image"
	@echo "  docker-up     - start via docker compose"
	@echo "  docker-down   - stop via docker compose"
	@echo "  docker-logs   - tail bot logs from compose"
	@echo "  backup        - run backup_database() once"
	@echo "  pre-commit-check - run all pre-commit hooks"

install:
	$(PIP) install -r requirements.txt

install-dev: install
	$(PIP) install -r requirements-test.txt
	$(PIP) install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

test-cov:
	$(PYTHON) -m pytest --cov=. --cov-report=term-missing

lint:
	ruff check .

format:
	ruff format .

type-check:
	mypy bot.py config.py db.py storage.py middleware.py utils.py scheduler.py backup.py monitoring.py logging_config.py

clean:
	rm -rf .pytest_cache .coverage htmlcov coverage.xml __pycache__ */__pycache__ */*/__pycache__
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

run:
	$(PYTHON) bot.py

docker-build:
	docker build -t barbershop-bot:latest .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=200 bot

backup:
	$(PYTHON) -c "import backup; print(backup.backup_database() or 'failed')"

pre-commit-check:
	pre-commit run --all-files
