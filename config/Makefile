.PHONY: setup test up-db init sync import grafana probe backup logs down

setup:           ## one-time: write .env, build the image, start SQL Server, create schema + logins
	scripts/setup-linux.sh
	docker compose build crawler
	docker compose up -d --wait mssql
	docker compose run --rm crawler init-db

test:            ## offline unit tests (needs: python -m venv .venv && .venv/bin/pip install -e ".[dev]")
	.venv/bin/python -m pytest -q

up-db:           ## start SQL Server and wait for healthy
	docker compose up -d --wait mssql

init:            ## create database objects and accounts (safe to rerun)
	docker compose run --rm crawler init-db

sync:            ## fetch KAP_YEARS for the active company list (config/companies.txt)
	docker compose run --rm crawler sync

import:          ## import every downloaded manifest in imports/ (no HTTP)
	docker compose run --rm crawler import-kap-export --input $(wildcard imports/*.json)

grafana:         ## start Grafana on http://127.0.0.1:3000
	docker compose up -d grafana

probe:           ## reachability check: two paced GETs against kap.org.tr
	docker compose run --rm crawler probe-source

backup:
	scripts/backup.sh

logs:
	docker compose logs --tail=100 mssql grafana

down:            ## stop services (volumes are kept)
	docker compose down
