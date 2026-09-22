#!/usr/bin/env bash
# Prepare a new local Docker installation. Existing credentials are never overwritten.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'Install Docker Engine and the compose plugin first.'; exit 1; }
docker compose version >/dev/null
command -v python3 >/dev/null || { echo 'Install Python 3 first.'; exit 1; }
if [ ! -f .env ]; then
  umask 077
  python3 - <<'PY'
from pathlib import Path
import secrets, re
source = Path('.env.example').read_text()
passwords = {}
for key in re.findall(r'^([A-Z_]*PASSWORD)=', source, re.M):
    passwords[key] = secrets.token_urlsafe(24) + 'aA7!'
passwords['MSSQL_BOOTSTRAP_PASSWORD'] = passwords['MSSQL_SA_PASSWORD']
for key, value in passwords.items():
    source = re.sub(r'^' + key + r'=.*$', key + '=' + value, source, flags=re.M)
Path('.env').write_text(source)
PY
  echo 'Created .env with unique passwords. Review it before live source access.'
fi
sed -i "s/^CRAWLER_UID=.*/CRAWLER_UID=$(id -u)/" .env
sed -i "s/^CRAWLER_GID=.*/CRAWLER_GID=$(id -g)/" .env
chmod 600 .env
mkdir -p data/raw data/state data/runs data/captures imports config
chmod 755 data config
if rg -q REPLACE_WITH .env 2>/dev/null || grep -q REPLACE_WITH .env; then
  echo 'Replace remaining password placeholders before starting services.'
fi
echo 'Next: make setup (or: docker compose build crawler; docker compose up -d --wait mssql; docker compose run --rm crawler init-db)'
