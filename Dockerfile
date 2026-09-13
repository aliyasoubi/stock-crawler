# Crawler image: Python 3.12 + Microsoft ODBC Driver 18. One-shot CLI, not a service.
FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg2 ca-certificates apt-transport-https \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list \
      | sed 's|deb |deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] |' > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y curl gnupg2 && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY sql ./sql
RUN pip install .

RUN useradd --create-home --uid 10001 crawler \
 && mkdir -p /app/data /app/config /app/imports /app/tests/fixtures \
 && chown -R crawler:crawler /app
USER crawler

ENTRYPOINT ["stock-crawler"]
CMD ["--help"]
