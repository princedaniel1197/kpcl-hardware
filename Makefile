# CRPMS Demonstrator — Orianode Technologies
# Stage 0 of CRPMS_Demonstrator_Build_Plan.md.
#
# Targets required by Stage 0: up, down, sim, collector, api, ui, test.
# The stage targets refuse to pretend: if the stage that builds a component has
# not been done, the target says so instead of failing obscurely.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON  ?= python3
VENV    := .venv
VPY     := $(VENV)/bin/python

# Docker Desktop only symlinks its CLI into /usr/local/bin once it has been
# launched and its licence accepted. The binary exists inside the bundle before
# that, so fall back to it rather than reporting Docker as absent when it is not.
DOCKER_APP_BIN_DIR := /Applications/Docker.app/Contents/Resources/bin
DOCKER_APP_BIN     := $(DOCKER_APP_BIN_DIR)/docker

# docker shells out to helpers that live beside it — docker-credential-desktop
# among them — so the bundle's bin directory must be on PATH, not just the one
# binary. Appended, so a real /usr/local/bin/docker still wins if it appears.
ifneq (,$(wildcard $(DOCKER_APP_BIN)))
export PATH := $(PATH):$(DOCKER_APP_BIN_DIR)
endif

DOCKER ?= $(shell command -v docker 2>/dev/null || (test -x $(DOCKER_APP_BIN) && echo $(DOCKER_APP_BIN)))
COMPOSE ?= $(DOCKER) compose

# Local settings. `make up` creates .env from .env.example on first run.
ifneq (,$(wildcard .env))
include .env
export
endif

POSTGRES_USER ?= crpms
POSTGRES_DB   ?= crpms
POSTGRES_PORT ?= 5432
DB_CONTAINER  := crpms-timescaledb

.PHONY: help doctor env up down logs psql wait-db clean install venv sim collector api ui test \
        migrate migrate-status loadtest seed-tags seed-assets seed-quality quality-demo seed-kpis kpi-demo outage-test compression-report \
        scraper scraper-once scraper-migrate scraper-install scraper-uninstall \
        scraper-logs scraper-status

help:
	@echo "CRPMS Demonstrator — make targets"
	@echo ""
	@echo "  doctor      check that this machine has Python 3.11+, Docker and Node 20+"
	@echo "  install     create $(VENV) and install the project with dev extras"
	@echo "  up          start TimescaleDB and wait until it accepts connections"
	@echo "  down        stop TimescaleDB (the named volume is kept)"
	@echo "  psql        open a psql shell inside the running container"
	@echo "  logs        follow TimescaleDB logs"
	@echo "  clean       stop and DESTROY the archive volume (needs CONFIRM=yes)"
	@echo ""
	@echo "  sim         run the OPC UA DCS simulator        (Stage 1)"
	@echo "  collector   run the acquisition collector       (Stage 3)"
	@echo "  api         run the FastAPI service             (Stage 12)"
	@echo "  ui          run the React visualisation         (Stage 12)"
	@echo "  test        run the test suite"
	@echo ""
	@echo "  migrate            apply archive SQL migrations    (Stage 2)"
	@echo "  migrate-status     list applied and pending migrations"
	@echo "  loadtest           Stage 2 acceptance test: 10M rows (~1 GB)"
	@echo ""
	@echo "  seed-tags          load tag configuration from config/  (Stage 3)"
	@echo "  seed-assets        load the asset hierarchy from config/ (Stage 5)"
	@echo "  seed-quality       load quality rule thresholds       (Stage 6)"
	@echo "  quality-demo       Stage 6 acceptance test (forces each condition)"
	@echo "  seed-kpis          load KPI definitions, regenerate the dictionary"
	@echo "  kpi-demo           Stage 7 acceptance test (Bad input -> Bad KPI)"
	@echo "  outage-test        Stage 3 acceptance test (stops the database)"
	@echo "  compression-report Stage 4 ratios and reconstruction error"
	@echo ""
	@echo "  scraper-migrate    create the SLDC recorder tables"
	@echo "  scraper-once       one poll, then exit"
	@echo "  scraper            run the SLDC recorder in the foreground"
	@echo "  scraper-install    run it under launchd, restarting on reboot"
	@echo "  scraper-status     daily row counts and recent poll attempts"
	@echo "  scraper-logs       follow the recorder log"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

doctor:
	@echo "--- prerequisites ---"
	@if command -v $(PYTHON) >/dev/null 2>&1 && $(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then \
	  echo "  OK    python 3.11+   $$($(PYTHON) --version) ($$(command -v $(PYTHON)))"; \
	else \
	  echo "  FAIL  python 3.11+   found $$(command -v $(PYTHON) >/dev/null 2>&1 && $(PYTHON) --version 2>&1 || echo none)"; \
	  echo "        set PYTHON=/path/to/python3.11 or install Python 3.11+"; \
	fi
	@if [ -n "$(DOCKER)" ]; then \
	  echo "  OK    docker         $$($(DOCKER) --version)"; \
	  test -e /usr/local/bin/docker -o -e $$HOME/.docker/bin/docker || \
	    echo "        note: no docker outside Docker.app — make uses the bundled CLI"; \
	  if $(DOCKER) ps >/dev/null 2>&1; then echo "  OK    docker daemon   running"; \
	  else echo "  FAIL  docker daemon   not running — open Docker Desktop and accept the licence"; fi; \
	else \
	  echo "  FAIL  docker         not installed"; \
	fi
	@if command -v node >/dev/null 2>&1; then echo "  OK    node           $$(node --version)"; \
	else echo "  FAIL  node           not installed (needed from Stage 12)"; fi

env:
	@if [ ! -f .env ]; then cp .env.example .env; echo "created .env from .env.example"; fi

up: env
	@test -n "$(DOCKER)" || { echo "docker is not installed — run 'make doctor'"; exit 1; }
	@$(DOCKER) ps >/dev/null 2>&1 || { \
	  echo "the Docker daemon is not reachable — open Docker Desktop, accept the licence,"; \
	  echo "wait for the whale icon to settle, then re-run 'make up'."; exit 1; }
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory wait-db
	@echo "TimescaleDB ready on port $(POSTGRES_PORT) — psql: make psql"

wait-db:
	@printf "waiting for TimescaleDB "
	@for i in $$(seq 1 60); do \
	  if $(DOCKER) exec $(DB_CONTAINER) pg_isready -U $(POSTGRES_USER) -d $(POSTGRES_DB) >/dev/null 2>&1; then \
	    echo " ready"; exit 0; \
	  fi; \
	  printf "."; sleep 1; \
	done; \
	echo " TIMED OUT"; $(DOCKER) logs --tail 40 $(DB_CONTAINER); exit 1

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f timescaledb

psql:
	$(DOCKER) exec -it $(DB_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

# Destructive: the archive volume holds every sample. Explicit opt-in only.
clean:
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  echo "This destroys the crpms-pgdata volume and every archived sample."; \
	  echo "Re-run with: make clean CONFIRM=yes"; exit 1; \
	fi
	$(COMPOSE) down -v

venv:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || { \
	  echo "Python 3.11+ required; $(PYTHON) is $$($(PYTHON) --version 2>&1)."; \
	  echo "Install Python 3.11+ and retry, or: make install PYTHON=/path/to/python3.11"; exit 1; }
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)

install: venv
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -e ".[dev]"

# ---------------------------------------------------------------------------
# Components. Each is built by the stage named in its guard.
# ---------------------------------------------------------------------------

sim:
	@test -f sim/server.py || { echo "sim/server.py does not exist yet — that is Stage 1."; exit 1; }
	$(VPY) -m sim

collector:
	@test -f collector/main.py || { echo "collector/main.py does not exist yet — that is Stage 3."; exit 1; }
	$(VPY) -m collector.main

api:
	@test -f api/main.py || { echo "api/main.py does not exist yet — that is Stage 12."; exit 1; }
	$(VENV)/bin/uvicorn api.main:app --host $${API_HOST:-127.0.0.1} --port $${API_PORT:-8000} --reload

ui:
	@test -f ui/package.json || { echo "ui/package.json does not exist yet — that is Stage 12."; exit 1; }
	cd ui && npm run dev

test:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m pytest

# ---------------------------------------------------------------------------
# Archive (Stage 2)
# ---------------------------------------------------------------------------

migrate:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m archive.migrate

migrate-status:
	@$(VPY) -m archive.migrate --status

# The Stage 2 acceptance test. Writes ~1 GB; run it deliberately.
loadtest:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m archive.loadtest

# ---------------------------------------------------------------------------
# Collector (Stage 3)
# ---------------------------------------------------------------------------

seed-kpis:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m engine.seed_kpis

# The Stage 7 acceptance test: forces coal flow Bad and watches heat rate.
kpi-demo:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m engine.kpi_demo

seed-quality:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m engine.seed_quality

# The Stage 6 acceptance test: forces each quality condition on the live system.
quality-demo:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m engine.quality_demo

seed-assets:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m engine.seed_assets

seed-tags:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m collector.seed

# The Stage 4 acceptance measurement: compression against live simulator data.
compression-report:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m collector.compression_report

# The Stage 3 acceptance test. STOPS THE DATABASE CONTAINER for three minutes.
outage-test:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m collector.outage_test

# ---------------------------------------------------------------------------
# Karnataka SLDC generation recorder (scraper/). Not a build-plan stage.
# ---------------------------------------------------------------------------

scraper-migrate:
	@test -n "$(DOCKER)" || { echo "docker is not installed — run 'make doctor'"; exit 1; }
	$(DOCKER) exec -i $(DB_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) \
	  -v ON_ERROR_STOP=1 < scraper/migrations/001_sldc_generation.sql

scraper-once:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m scraper --once

scraper:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m scraper

scraper-install:
	./deploy/install-recorder.sh

scraper-uninstall:
	./deploy/uninstall-recorder.sh

scraper-logs:
	@tail -f $$HOME/Library/Logs/orianode-crpms/sldc-recorder.log

# The liveness check: daily row counts and the last few poll attempts.
scraper-status:
	@$(DOCKER) exec -i $(DB_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c \
	  "SELECT * FROM sldc_daily_rows LIMIT 7;"
	@$(DOCKER) exec -i $(DB_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c \
	  "SELECT attempt_ts, outcome, http_status, rows_written, page_source_ts, duration_ms \
	   FROM sldc_poll_log ORDER BY attempt_ts DESC LIMIT 10;"
	@launchctl print gui/$$UID/com.orianode.crpms.sldc-recorder 2>/dev/null \
	  | grep -E "state = |pid = |last exit" || echo "launchd agent not installed"
