.PHONY: test up-db init sync grafana probe backup logs down

test:            ## offline unit tests
	.venv/bin/python -m pytest -q

up-db:           ## start SQL Server and wait for healthy
	docker compose up -d --wait mssql

init:            ## create database objects and accounts
	docker compose run --rm crawler init-db

sync:            ## run the crawler for the active company list
	docker compose run --rm crawler sync

grafana:         ## start Grafana on http://127.0.0.1:3000
	docker compose up -d grafana

probe:           ## two paced GETs against the source host (robots.txt, root)
	docker compose run --rm crawler probe-source

backup:
	scripts/backup.sh

logs:
	docker compose logs --tail=100 mssql grafana

down:            ## stop services (volumes are kept)
	docker compose down
