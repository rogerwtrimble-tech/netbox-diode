# NetBox + Diode inventory reconciliation MVP
COMPOSE ?= docker compose
RUN_RECON = $(COMPOSE) --profile tools run --rm --user "$$(id -u):$$(id -g)" recon

.PHONY: help init up demo plan status logs down reset test

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

init:  ## generate .env and OAuth2 credentials (idempotent)
	python3 scripts/init_secrets.py

up: init  ## build and start the stack, wait until healthy
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) --profile tools build recon

demo:  ## run the 3-phase reconciliation scenario, write reports/
	mkdir -p reports && $(RUN_RECON) demo

plan:  ## dry run: SNAPSHOT=data/discovery_day1.json
	$(RUN_RECON) plan /app/$(SNAPSHOT)

status:  ## container health
	$(COMPOSE) ps --format "table {{.Service}}\t{{.Status}}"

logs:  ## follow Diode logs
	$(COMPOSE) logs -f diode-ingester diode-reconciler

down:  ## stop (keeps data)
	$(COMPOSE) down

reset:  ## stop and delete all data volumes (fresh NetBox)
	$(COMPOSE) down -v

test:  ## lint, type-check and unit tests (tox)
	tox
