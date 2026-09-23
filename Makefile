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

.PHONY: help doctor env up down logs psql wait-db clean install venv sim collector engine api ui test \
        token firmware itp intrusion-demo deadband-evidence \
        migrate migrate-status loadtest seed-tags seed-assets seed-quality quality-demo seed-kpis kpi-demo events-demo omf-receiver omf-demo rig-stub redundancy-demo outage-test compression-report \
        fat backup restore backup-install backup-uninstall capacity alerts alerts-watch seed-alerts export access \
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
	@echo "  engine      run KPIs, quality rules, event frames (Stages 6-8)"
	@echo "  api         run the FastAPI service             (Stage 12)"
	@echo "  token       create a read-only API token for the UI (§509)"
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
	@echo "  events-demo        Stage 8 acceptance test (two start-ups, compared)"
	@echo "  omf-receiver       run the OMF receiver                (Stage 9)"
	@echo "  omf-demo           Stage 9 acceptance test (OMF, switchable endpoint)"
	@echo "  rig-stub           bench rig stand-in, NOT the rig      (Stage 10)"
	@echo "  firmware           compile both ESP32 builds (TCP, RTU) (Stage 10)"
	@echo "  redundancy-demo    Stage 11 acceptance test (kill the primary)"
	@echo ""
	@echo "  fat                run the automated FAT, write a report (Stage 14)"
	@echo "  itp                regenerate fat/ITP.md from fat/plan.py"
	@echo "  intrusion-demo     show T-12 failing a polling client"
	@echo "  backup / restore   archive backup and clean-environment restore"
	@echo "  backup-install     hourly backup under launchd (RPO 1 hour)"
	@echo "  capacity           central capacity report          (§344)"
	@echo "  alerts             evaluate alert rules once        (§507)"
	@echo "  export             machine-readable export          (§503)"
	@echo "  access             list principals and roles        (§509)"
	@echo "  outage-test        Stage 3 acceptance test (stops the database)"
	@echo "  compression-report Stage 4: compression through the live pipeline"
	@echo "  deadband-evidence  source deadband on vs off, measured"
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
# Components
# ---------------------------------------------------------------------------

sim:
	$(VPY) -m sim $(SIM_ARGS)

collector:
	$(VPY) -m collector

# KPIs, quality rules and event frames, continuously (engine/service.py).
engine:
	$(VPY) -m engine

api:
	$(VENV)/bin/uvicorn api.main:app --host $${API_HOST:-127.0.0.1} --port $${API_PORT:-8000} --reload

# Every API call needs a token (§509). This makes one for the visualisation,
# read-only, and prints it once.
token:
	@$(VPY) -m ops.access create $${NAME:-viewer.$$USER} $${ROLE:-corporate}

ui:
	@test -d ui/node_modules || { echo "installing ui dependencies ..."; cd ui && npm install; }
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

redundancy-demo:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m collector.redundancy_demo

# Stand-in for the bench rig, for testing the bridge without the hardware.
rig-stub:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m firmware.rig_stub

# Compile both firmware builds (TCP and RTU). Needs PlatformIO: `pip install
# platformio` into any environment, or set PIO. Compiling is not flashing.
PIO ?= $(shell command -v pio 2>/dev/null || echo $$HOME/.pio-venv/bin/pio)
firmware:
	@test -f firmware/src/secrets.h || cp firmware/src/secrets.h.example firmware/src/secrets.h
	cd firmware && $(PIO) run -e tcp -e rtu

omf-receiver:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m archive.omf_receiver

# The Stage 9 acceptance test: OMF messages and a configurable destination.
omf-demo:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m collector.omf_demo

events-demo:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m engine.events_demo

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

# The measurement behind config/sources.json: source deadband on vs off.
deadband-evidence:
	@$(VPY) -m collector.deadband_evidence

# The Stage 3 acceptance test. STOPS THE DATABASE CONTAINER for three minutes,
# and must run with no other collector running.
outage-test:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m collector.outage_test

# ---------------------------------------------------------------------------
# Operations (Stage 13)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The FAT (Stage 14)
# ---------------------------------------------------------------------------

fat:
	@test -x $(VPY) || { echo "no virtualenv — run 'make install' first."; exit 1; }
	$(VPY) -m fat.runner

itp:
	@$(VPY) -m fat.generate_itp

# Shows T-12 can fail: the same measurement against a polling client.
intrusion-demo:
	@$(VPY) -m fat.intrusion_demo

backup:
	./ops/backup.sh

restore:
	@test -n "$(DUMP)" || { echo "usage: make restore DUMP=backups/crpms-....dump"; exit 1; }
	./ops/restore.sh "$(DUMP)"

backup-install:
	./deploy/install-backup.sh

backup-uninstall:
	@launchctl bootout "gui/$$UID/com.orianode.crpms.backup" 2>/dev/null || true
	@rm -f "$$HOME/Library/LaunchAgents/com.orianode.crpms.backup.plist"
	@echo "hourly backup removed"

capacity:
	@$(VPY) -m ops.capacity

alerts:
	@$(VPY) -m ops.alerts --once

alerts-watch:
	$(VPY) -m ops.alerts

seed-alerts:
	@$(VPY) -m ops.seed_alerts

export:
	@$(VPY) -m ops.export --hours $${HOURS:-24}

access:
	@$(VPY) -m ops.access list

# ---------------------------------------------------------------------------
# Karnataka SLDC generation recorder (scraper/). Not a build-plan stage.
# ---------------------------------------------------------------------------

# Each file is idempotent (IF NOT EXISTS / OR REPLACE / ON CONFLICT), applied in
# name order.
scraper-migrate:
	@test -n "$(DOCKER)" || { echo "docker is not installed — run 'make doctor'"; exit 1; }
	@for f in scraper/migrations/*.sql; do \
	  echo "applying $$f"; \
	  $(DOCKER) exec -i $(DB_CONTAINER) psql -q -U $(POSTGRES_USER) -d $(POSTGRES_DB) \
	    -v ON_ERROR_STOP=1 < $$f || exit 1; \
	done

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
