.PHONY: help install run cycle premarket test lint fmt clean docker

help:
	@echo "panadhanam — Multi-Agent Trading Intelligence"
	@echo ""
	@echo "  make install    create .venv and install dependencies"
	@echo "  make run        start the server + dashboard (http://127.0.0.1:8000)"
	@echo "  make cycle      run ONE analysis cycle in the terminal and exit"
	@echo "  make premarket  run the pre-market fundamental + macro scan"
	@echo "  make test       run the test suite"
	@echo "  make lint       ruff check"
	@echo "  make fmt        ruff format"
	@echo "  make clean      remove caches and the runtime database"
	@echo "  make docker     build and run in Docker"

VENV ?= .venv
PY = $(VENV)/bin/python

install:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@test -f .env || cp .env.example .env
	@echo ""
	@echo "✅ Installed. Edit .env if you want live data, then: make run"

run:
	$(PY) run.py

cycle:
	$(PY) run.py --cycle

premarket:
	$(PY) run.py --premarket

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check app tests scripts run.py

fmt:
	$(PY) -m ruff format app tests scripts run.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
	rm -f data/runtime/*.db data/runtime/*.db-wal data/runtime/*.db-shm

docker:
	docker compose up --build
