# Ubuntu SOCKS proxy: diagnosis and two ways to run the crawler

These add-on files target the attached stock-crawler project. Put them inside your
existing `~/projects/stock-crawler` checkout. Keep its existing `.env` and data.
Use the same directory and Compose project name you used to create SQL Server.

## What the reported error proves

`exec: "curl": executable file not found in $PATH` happens before networking.
The Dockerfile installs curl to download Microsoft's key and then explicitly purges
it. This error does not establish whether the proxy or KAP is reachable.

The project already includes `httpx[socks]==0.28.1` and the Linux
`host.docker.internal:host-gateway` mapping. However, that mapping cannot make an SSH
proxy listening only on host `127.0.0.1` reachable from a bridge-network container.
The host browser and the container have different loopback interfaces.

HTTPX reads `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY`; the project's
`SOCKS5_PROXY` variable is unused by its HTTP client. A scheme-specific or lowercase
proxy can supersede your intended setting. The supplied launcher sets both cases.
[HTTPX environment documentation](https://www.python-httpx.org/environment_variables/).

## 1. Diagnose before changing the deployment

Keep your existing SSH tunnel running. On Ubuntu:

```bash
cd ~/projects/stock-crawler
ss -ltnp 'sport = :10808'
curl --proxy socks5h://127.0.0.1:10808 --noproxy '' \
  --connect-timeout 10 --max-time 25 \
  -sS -o /dev/null -w 'Host HTTP %{http_code}\n' https://www.kap.org.tr/en
```

An address such as `127.0.0.1:10808` confirms a loopback listener. `0.0.0.0:10808`
listens on other interfaces too. Do not broaden the listener just to try this fix.

Test the already-built image with host networking. This needs no image download,
image rebuild, SQL connection, or curl installation in the image:

```bash
docker run --rm --pull=never --network host --entrypoint python \
  stock-crawler:local -c 'import httpx; c=httpx.Client(proxy="socks5h://127.0.0.1:10808", trust_env=False, timeout=20); r=c.get("https://www.kap.org.tr/en"); print("Container HTTP", r.status_code); c.close()'
```

For a more detailed bridge-network diagnostic after extracting this add-on:

```bash
docker compose run --rm --no-deps -T --entrypoint python crawler - \
  --proxy socks5h://host.docker.internal:10808 < scripts/probe-socks.py
```

The supplied probe sends one GET, does not follow redirects or retry, and does not
read or modify crawler state. Use it to diagnose transport only; use the normal
application probe before a sync.

| Result | Meaning / next action |
|---|---|
| Missing `curl` | Test never started; use Python as above. |
| Missing `socksio` | The local image predates the SOCKS dependency; rebuild the crawler image. |
| Proxy DNS failure | The host alias is missing or not resolving in that container. |
| Proxy TCP refusal | No listener reachable at that address/port; check `ss` output. |
| Proxy TCP timeout | Check listener address, routing and firewall; this does not identify one exact cause. |
| Proxy TCP works, HTTP request fails | Investigate SOCKS negotiation, SSH-server egress, destination DNS or TLS. |
| Host and host-network container both return HTTP | Host networking works for that URL; proceed with option A. |
| HTTP 403 / 429 | Transport worked, but access was denied / throttled. Stop; native Python will not inherently fix that. |
| HTTP 200 | That URL responded; the XLSX export POST still needs a small sync to verify. |

The browser working alone is not a terminal test: it may use different settings,
cookies, cached pages or a different tunnel. Do not infer that DNS succeeded solely
from HTTPX `ConnectTimeout`.

## 2. Option A — keep the crawler in Docker (recommended first)

Host networking lets the crawler reach your existing SSH listener at
`127.0.0.1:10808`. SQL Server stays on its Compose network, with port 1433 published
only to host loopback. The crawler therefore uses `127.0.0.1:1433` for SQL, not the
Compose-only hostname `mssql`. Grafana continues to use its existing SQL connection.
[Docker host networking](https://docs.docker.com/engine/network/drivers/host/).

This is for Docker Engine on Linux. Docker Desktop needs its host-network feature
enabled; rootless/remote engines need separate verification.

Extract the add-on archive into the existing project, then run:

```bash
bash scripts/crawler-ubuntu.sh db
bash scripts/crawler-ubuntu.sh docker probe-source
```

The first command may briefly recreate SQL Server to add its port publication; its
named data volume is reused. Do not run `docker compose down -v`.

If port 1433 is already occupied, choose one alternative for both commands:

```bash
export CRAWLER_DB_HOST_PORT=11433
bash scripts/crawler-ubuntu.sh db
bash scripts/crawler-ubuntu.sh docker probe-source
```

Keep that variable exported for subsequent crawler commands (or add the same export
to the shell used for this project). A different proxy can be selected with
`export CRAWLER_PROXY_URL=socks5h://127.0.0.1:YOUR_PORT`.

Review these settings in your existing `.env`, retaining your current passwords:

```dotenv
SOURCE_MODE=kap-export
KAP_YEARS=[2024,2025]
```

Those are example completed years; keep the years you actually want. Set a real
operator contact in `HTTP_USER_AGENT`. Remove the obsolete
`KAP_CALENDAR_YEAR_TICKERS`; the current setting is `KAP_NON_CALENDAR_YEAR_TICKERS`
for exceptional non-calendar-year issuers only.

Once `probe-source` reports the site root as reachable, test two companies:

```bash
bash scripts/crawler-ubuntu.sh docker sync --tickers THYAO,ASELS
```

Read the resulting summary. Fresh companies may be skipped; `--refresh` requests
another check if deliberately needed. A successful page probe does not validate the
export endpoint. Source access blocks/cooldowns recorded by the application remain
in effect. Do not clear all state to fix a connection error.

Use this launcher for crawler commands instead of `make sync` / `make probe`, which
use the original bridge-network configuration. Network overrides alone do not need a
rebuild, but every Python source update does: run `bash scripts/crawler-ubuntu.sh build`
before `init-db` or `sync`. The host-network crawler shares the
host's network namespace; filesystem/process isolation remains.

## 3. Option B — native Ubuntu crawler, SQL Server in Docker

This is supported by the application architecture, but requires Python packages
and Microsoft's ODBC driver on Ubuntu. Use Python 3.12 to match the existing image;
the supplied archive contains a Python 3.14 environment, which should not be reused
for the pinned dependency set without checking compatibility.

### Install the native prerequisites

Check the actual OS version:

```bash
cat /etc/os-release
```

Microsoft's current ODBC Driver 18 installation guide includes Ubuntu 26.04. The
following uses the repository for your actual version, not a substituted older OS.
[Microsoft installation guide](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server?view=sql-server-ver17).

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates unixodbc
ubuntu_release=$(python3 -c 'import platform; print(platform.freedesktop_os_release()["VERSION_ID"])')
ms_repo_package=$(mktemp --suffix=.deb)
curl --proxy socks5h://127.0.0.1:10808 --noproxy '' -fSL \
  "https://packages.microsoft.com/config/ubuntu/$ubuntu_release/packages-microsoft-prod.deb" \
  -o "$ms_repo_package"
sudo dpkg -i "$ms_repo_package"
rm "$ms_repo_package"
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18
odbcinst -q -d
```

The driver list must include `ODBC Driver 18 for SQL Server`. Package installation
also needs network access; a browser proxy does not automatically configure APT,
uv or pip. If a download fails, fix that package manager's proxy before continuing.

If Python 3.12 is already installed:

```bash
python3.12 -m venv .venv-native
.venv-native/bin/python -m pip install -e .
```

Otherwise install uv following its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/), then use:

```bash
uv python install 3.12
uv venv --python 3.12 .venv-native
uv pip install --python .venv-native/bin/python -e .
```

[uv's Python-version guide](https://docs.astral.sh/uv/guides/install-python/).

Validate the native interpreter and driver:

```bash
.venv-native/bin/python -c 'import sys, httpx, socksio, pyodbc; print(sys.version); print(pyodbc.drivers())'
```

### Run

```bash
bash scripts/crawler-ubuntu.sh db
bash scripts/crawler-ubuntu.sh native probe-source
```

After the source probe succeeds:

```bash
bash scripts/crawler-ubuntu.sh native sync --tickers THYAO,ASELS
```

This starts only SQL Server if no other services are already running. Grafana can
remain in Docker if wanted. The launcher converts `/app/...` settings to native
paths, supplies the host SQL address, and exports proxy settings for HTTPX while
retaining credentials from `.env`. Merely placing proxy keys in `local.env` would
not export them to HTTPX in this application's native path.

If the database was already initialized, do not replace credentials or recreate
accounts. For a genuinely new database only, run
`bash scripts/crawler-ubuntu.sh native init-db` before the first sync.

## Verification and scope

Prepared against the attached source, not an unseen later repository commit. The
add-on contains deployment overrides and launch/diagnostic helpers; it does not
change parsing, storage, or request pacing. YAML and script syntax and launcher
environment/argument handling were checked locally. This environment has no Docker
daemon or connection to your Ubuntu machine, so real Compose startup, the Ubuntu
ODBC installation and KAP connectivity must be confirmed with the commands above.

One separate archive issue: `scripts/setup-linux.sh` reads `.env.example`, while
the supplied ZIP has `env.example`. This only affects creating a fresh `.env` and
does not explain your running container's curl error. The add-on expects your
existing `.env` and does not rerun the bootstrap script.
