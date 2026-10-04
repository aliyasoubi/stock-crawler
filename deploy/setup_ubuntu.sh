#!/usr/bin/env bash
# Sets up stock-crawler on Ubuntu: Python, Microsoft's ODBC driver for SQL Server (used by
# load_db), the project's .venv, then runs the offline tests. Safe to run again.
#
#   ACCEPT_EULA=Y bash deploy/setup_ubuntu.sh
#
# ACCEPT_EULA=Y means you accept the licence of Microsoft's ODBC driver (msodbcsql18),
# shown at https://aka.ms/odbc18eula
set -euo pipefail
cd "$(dirname "$0")/.."
SUDO=$([ "$(id -u)" -eq 0 ] || echo sudo)

if [ "${ACCEPT_EULA:-}" != "Y" ]; then
    echo "Microsoft's ODBC driver needs its licence accepted: https://aka.ms/odbc18eula"
    echo "If you accept it, run: ACCEPT_EULA=Y bash deploy/setup_ubuntu.sh"
    exit 2
fi

$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv curl ca-certificates
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is needed"; exit 1; }

if ! dpkg -s msodbcsql18 >/dev/null 2>&1; then
    version=$(. /etc/os-release && echo "$VERSION_ID")
    curl -fsSL -o /tmp/packages-microsoft-prod.deb \
        "https://packages.microsoft.com/config/ubuntu/$version/packages-microsoft-prod.deb"
    $SUDO dpkg -i /tmp/packages-microsoft-prod.deb
    rm /tmp/packages-microsoft-prod.deb
    $SUDO apt-get update
    $SUDO env ACCEPT_EULA=Y apt-get install -y msodbcsql18
fi

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -c "import pyodbc; print('ODBC drivers:', pyodbc.drivers())"
.venv/bin/python -m pytest -q
