"""Validated settings and company-list loading."""

from __future__ import annotations

import re
from pathlib import Path
from datetime import date
from typing import Iterable, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

TICKER_PATTERN = re.compile(r"^[A-Z0-9]{2,10}$")


class SettingsError(ValueError):
    """Raised for invalid configuration or company lists."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    company_file: Path = Path("config/companies.txt")
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    http_user_agent: str = "StockFundamentalsMVP/0.1"

    # kap-export: live KAP comparison XLSX (the real data product). fixture: replay local HTML
    # fixtures with zero HTTP, used by the test-suite and for exercising SQL/Grafana offline.
    source_mode: Literal["fixture", "kap-export"] = "kap-export"
    kap_company_registry: Path = Path("config/kap_companies.json")
    # Historical titles proven by `verify-aliases` (single-company exports). Optional.
    kap_alias_file: Path = Path("config/kap_aliases.json")
    kap_years: list[int] = Field(default_factory=lambda: [date.today().year - 1, date.today().year])
    # Issuers whose financial year is NOT the calendar year. Everyone else gets Jan 1-Dec 31 inferred
    # from the export's Year column (BIST issuers overwhelmingly report on calendar years).
    kap_non_calendar_year_tickers: list[str] = Field(default_factory=list)
    fixture_source_dir: Path | None = Path("tests/fixtures/kap/source")

    mssql_host: str = "mssql"
    mssql_port: int = 1433
    mssql_database: str = "StockFundamentals"
    mssql_user: str = "crawler_writer"
    mssql_password: SecretStr = SecretStr("")
    mssql_encrypt: bool = True
    mssql_trust_server_certificate: bool = False
    mssql_bootstrap_user: str = "sa"
    mssql_bootstrap_password: SecretStr = SecretStr("")
    mssql_reader_password: SecretStr = SecretStr("")
    mssql_grafana_password: SecretStr = SecretStr("")
    mssql_driver: str = "ODBC Driver 18 for SQL Server"

    request_delay_min_seconds: float = Field(default=5.0, ge=0)
    request_delay_max_seconds: float = Field(default=10.0, ge=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: tuple[float, ...] = (10.0, 30.0)
    discovery_interval_hours: float = Field(default=24.0, ge=0)
    report_revalidate_hours: float = Field(default=168.0, ge=0)
    # kap-export requests 25 companies per POST, so 250 companies is about ten requests.
    max_companies_per_run: int = Field(default=250, gt=0)
    max_requests_per_run: int = Field(default=50, gt=0)
    throttle_cooldown_seconds: float = Field(default=3600.0, gt=0)
    retry_after_fallback_seconds: float = Field(default=60.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported LOG_LEVEL {value!r}")
        return level

    @model_validator(mode="after")
    def _check_delays(self) -> "Settings":
        if not self.kap_years or len(self.kap_years) > 5 or len(set(self.kap_years)) != len(self.kap_years) or any(y < 2000 or y > 2100 for y in self.kap_years):
            raise ValueError("KAP_YEARS must contain 1-5 unique years between 2000 and 2100")
        if self.request_delay_max_seconds < self.request_delay_min_seconds:
            raise ValueError("REQUEST_DELAY_MAX_SECONDS must be >= REQUEST_DELAY_MIN_SECONDS")
        if self.source_mode == "fixture" and self.fixture_source_dir is None:
            raise ValueError("FIXTURE_SOURCE_DIR is required when SOURCE_MODE=fixture")
        return self

    def calendar_year_confirmed(self, ticker: str) -> bool:
        return ticker.upper() not in {t.upper() for t in self.kap_non_calendar_year_tickers}

    def sqlalchemy_url(self, *, user: str | None = None, password: str | None = None, database: str | None = None) -> URL:
        """Build a pyodbc URL. Credentials default to the runtime crawler_writer account."""
        def quoted(value: str) -> str:
            return "{" + value.replace("}", "}}") + "}"

        server = f"{self.mssql_host},{self.mssql_port}"
        odbc = ";".join(
            [
                f"DRIVER={{{self.mssql_driver}}}",
                f"SERVER={quoted(server)}",
                f"DATABASE={quoted(database or self.mssql_database)}",
                f"UID={quoted(user or self.mssql_user)}",
                f"PWD={quoted(password if password is not None else self.mssql_password.get_secret_value())}",
                f"Encrypt={'yes' if self.mssql_encrypt else 'no'}",
                f"TrustServerCertificate={'yes' if self.mssql_trust_server_certificate else 'no'}",
            ]
        )
        return URL.create("mssql+pyodbc", query={"odbc_connect": odbc})


def normalize_tickers(raw: Iterable[str]) -> list[str]:
    """Trim, drop blanks/comments, uppercase, validate, de-duplicate, sort deterministically."""
    seen: set[str] = set()
    for line in raw:
        item = line.split("#", 1)[0].strip().upper()
        if not item:
            continue
        if not TICKER_PATTERN.match(item):
            raise SettingsError(f"invalid ticker {item!r}: expected 2-10 letters/digits")
        seen.add(item)
    return sorted(seen)


def load_company_file(path: Path) -> list[str]:
    if not path.is_file():
        raise SettingsError(f"company file not found: {path}")
    with path.open("r", encoding="utf-8-sig") as handle:
        return normalize_tickers(handle)


def parse_ticker_argument(value: str) -> list[str]:
    tickers = normalize_tickers(value.split(","))
    if not tickers:
        raise SettingsError("--tickers must contain at least one ticker")
    return tickers
