#!/usr/bin/env bash
# One-time host preparation on Ubuntu (or any Linux with Docker Engine + compose plugin).
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "docker not found: install Docker Engine + compose plugin first (https://docs.docker.com/engine/install/ubuntu/)"; exit 1; }
docker compose version >/dev/null || { echo "docker compose plugin missing"; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example - edit every REPLACE_WITH_* value before continuing"
fi

# Run the crawler container as the invoking user so bind mounts are writable.
sed -i "s/^CRAWLER_UID=.*/CRAWLER_UID=$(id -u)/" .env
sed -i "s/^CRAWLER_GID=.*/CRAWLER_GID=$(id -g)/" .env

mkdir -p data/raw data/state data/runs data/captures imports config
chmod 755 data config

if grep -q REPLACE_WITH .env; then
  echo "WARNING: .env still contains REPLACE_WITH_* placeholders; SQL Server will refuse to start with a weak sa password."
fi
echo "host ready. next: docker compose up -d mssql && docker compose run --rm crawler init-db"
