"""Parameterized SQL Server persistence, transactions, readiness, and bootstrap."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from .config import Settings
from .models import FINANCIAL_FIELDS, METHOD_FIELDS, CompanyIdentity, FundamentalRecord, ReportRecord

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabaseError(RuntimeError):
    pass


class IdentityConflict(DatabaseError):
    """A ticker or source id already maps to a different company."""


@dataclass(frozen=True)
class CompanyRow:
    company_id: int
    market_source: str
    source_company_id: str
    ticker: str
    company_name: str | None
    yahoo_ticker: str | None
    last_discovery_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    latest_discovered_notification_id: str | None


@dataclass(frozen=True)
class ReportVersionRow:
    report_id: int
    parse_status: str
    is_withdrawn: bool


class RepositoryLike(Protocol):
    """The subset of persistence the pipeline needs; tests use an in-memory implementation."""

    def get_company_by_ticker(self, market_source: str, ticker: str) -> CompanyRow | None: ...

    def upsert_company(self, identity: CompanyIdentity) -> CompanyRow: ...

    def record_discovery(self, company_id: int, *, at: datetime, notification_id: str | None, error: str | None) -> None: ...

    def record_success(self, company_id: int, *, at: datetime) -> None: ...

    def find_report_version(self, market_source: str, notification_id: str, content_hash: str, parser_version: str) -> ReportVersionRow | None: ...

    def save_report(self, report: ReportRecord, fundamentals: list[FundamentalRecord]) -> tuple[int, str]: ...

    def notification_is_withdrawn(self, market_source: str, notification_id: str) -> bool: ...

    def mark_withdrawn(self, market_source: str, notification_id: str) -> int: ...

    def get_report(self, report_id: int) -> dict[str, Any] | None: ...

    def get_fundamentals(self, report_id: int) -> list[dict[str, Any]]: ...

    def current_view_row(self, company_id: int, fiscal_year: int, consolidation_scope: str) -> dict[str, Any] | None: ...


_REPORT_COLUMNS = (
    "company_id", "market_source", "notification_id", "published_at", "filing_fiscal_year", "filing_fiscal_period",
    "filing_period_start_date", "filing_period_end_date", "consolidation_scope", "statement_type", "source_url",
    "document_url", "raw_path", "content_hash", "parser_version", "retrieved_at", "parsed_at", "parse_status",
    "is_withdrawn", "validation_summary",
)
_FUNDAMENTAL_COLUMNS = (
    "report_id", "fiscal_year", "fiscal_period", "period_start_date", "period_end_date", "is_comparative",
    "currency_code", "currency_scale", "presentation_currency_raw", "measuring_unit_date",
    *FINANCIAL_FIELDS, *METHOD_FIELDS,
)


def _insert_sql(table: str, columns: tuple[str, ...], output: str | None = None) -> str:
    cols = ", ".join(columns)
    params = ", ".join(f":{c}" for c in columns)
    out = f" OUTPUT INSERTED.{output}" if output else ""
    return f"INSERT INTO dbo.{table} ({cols}){out} VALUES ({params})"


class Repository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- companies ------------------------------------------------------------------------

    @staticmethod
    def _company(row: Any) -> CompanyRow:
        return CompanyRow(
            company_id=row.company_id,
            market_source=row.market_source,
            source_company_id=row.source_company_id,
            ticker=row.ticker,
            company_name=row.company_name,
            yahoo_ticker=row.yahoo_ticker,
            last_discovery_at=row.last_discovery_at,
            last_success_at=row.last_success_at,
            last_error=row.last_error,
            latest_discovered_notification_id=row.latest_discovered_notification_id,
        )

    def get_company_by_ticker(self, market_source: str, ticker: str) -> CompanyRow | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM dbo.companies WHERE market_source = :s AND ticker = :t"), {"s": market_source, "t": ticker}
            ).first()
        return self._company(row) if row else None

    def upsert_company(self, identity: CompanyIdentity) -> CompanyRow:
        with self.engine.begin() as conn:
            by_ticker = conn.execute(
                text("SELECT * FROM dbo.companies WHERE market_source = :s AND ticker = :t"),
                {"s": identity.market_source, "t": identity.ticker},
            ).first()
            by_source = conn.execute(
                text("SELECT * FROM dbo.companies WHERE market_source = :s AND source_company_id = :i"),
                {"s": identity.market_source, "i": identity.source_company_id},
            ).first()
            if by_ticker and by_ticker.source_company_id != identity.source_company_id:
                raise IdentityConflict(
                    f"ticker {identity.ticker} is stored with source id {by_ticker.source_company_id}, "
                    f"but the source now resolves it to {identity.source_company_id}"
                )
            if by_source and by_source.ticker != identity.ticker:
                raise IdentityConflict(
                    f"source id {identity.source_company_id} is stored as {by_source.ticker}, not {identity.ticker}"
                )
            if by_ticker:
                conn.execute(
                    text(
                        "UPDATE dbo.companies SET company_name = COALESCE(:n, company_name), "
                        "yahoo_ticker = COALESCE(:y, yahoo_ticker), updated_at = SYSDATETIMEOFFSET() WHERE company_id = :id"
                    ),
                    {"n": identity.company_name, "y": identity.yahoo_ticker, "id": by_ticker.company_id},
                )
                company_id = by_ticker.company_id
            else:
                company_id = conn.execute(
                    text(
                        "INSERT INTO dbo.companies (market_source, source_company_id, ticker, yahoo_ticker, company_name) "
                        "OUTPUT INSERTED.company_id VALUES (:s, :i, :t, :y, :n)"
                    ),
                    {
                        "s": identity.market_source,
                        "i": identity.source_company_id,
                        "t": identity.ticker,
                        "y": identity.yahoo_ticker,
                        "n": identity.company_name,
                    },
                ).scalar_one()
            row = conn.execute(text("SELECT * FROM dbo.companies WHERE company_id = :id"), {"id": company_id}).first()
        return self._company(row)

    def record_discovery(self, company_id: int, *, at: datetime, notification_id: str | None, error: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE dbo.companies SET last_discovery_at = :at, last_error = :err, "
                    "latest_discovered_notification_id = COALESCE(:nid, latest_discovered_notification_id), "
                    "updated_at = SYSDATETIMEOFFSET() WHERE company_id = :id"
                ),
                {"at": at, "err": error, "nid": notification_id, "id": company_id},
            )

    def record_success(self, company_id: int, *, at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE dbo.companies SET last_success_at = :at, last_error = NULL, updated_at = SYSDATETIMEOFFSET() WHERE company_id = :id"),
                {"at": at, "id": company_id},
            )

    # -- reports and fundamentals --------------------------------------------------------

    def find_report_version(self, market_source: str, notification_id: str, content_hash: str, parser_version: str) -> ReportVersionRow | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT report_id, parse_status, is_withdrawn FROM dbo.reports "
                    "WHERE market_source = :s AND notification_id = :n AND content_hash = :h AND parser_version = :p"
                ),
                {"s": market_source, "n": notification_id, "h": content_hash, "p": parser_version},
            ).first()
        return ReportVersionRow(row.report_id, row.parse_status, bool(row.is_withdrawn)) if row else None

    def save_report(self, report: ReportRecord, fundamentals: list[FundamentalRecord]) -> tuple[int, str]:
        """Insert a report version and its fundamentals in one transaction.

        Returns (report_id, outcome) where outcome is `inserted`, `exists` (a valid version was
        already stored; values untouched), or `upgraded` (a failed/unsupported version of the
        same snapshot+parser became valid).
        """
        if report.parse_status == "valid" and not fundamentals:
            raise DatabaseError("a valid report must carry at least one fundamentals row")
        if report.parse_status != "valid" and fundamentals:
            raise DatabaseError("non-valid reports must not carry fundamentals")
        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    "SELECT report_id, parse_status FROM dbo.reports WITH (UPDLOCK, HOLDLOCK) "
                    "WHERE market_source = :s AND notification_id = :n AND content_hash = :h AND parser_version = :p"
                ),
                {"s": report.market_source, "n": report.notification_id, "h": report.content_hash, "p": report.parser_version},
            ).first()
            if existing is not None:
                if existing.parse_status == "valid" or report.parse_status != "valid":
                    return int(existing.report_id), "exists"
                conn.execute(
                    text(
                        "UPDATE dbo.reports SET parse_status = :st, parsed_at = :pa, validation_summary = :vs, "
                        "filing_fiscal_year = :fy, filing_fiscal_period = :fp, filing_period_start_date = :ps, "
                        "filing_period_end_date = :pe, consolidation_scope = :cs, statement_type = :tt WHERE report_id = :id"
                    ),
                    {
                        "st": report.parse_status,
                        "pa": report.parsed_at,
                        "vs": report.validation_summary,
                        "fy": report.filing_fiscal_year,
                        "fp": report.filing_fiscal_period,
                        "ps": report.filing_period_start_date,
                        "pe": report.filing_period_end_date,
                        "cs": report.consolidation_scope,
                        "tt": report.statement_type,
                        "id": existing.report_id,
                    },
                )
                report_id, outcome = int(existing.report_id), "upgraded"
            else:
                params = report.model_dump()
                params["is_withdrawn"] = 1 if report.is_withdrawn else 0
                try:
                    report_id = int(conn.execute(text(_insert_sql("reports", _REPORT_COLUMNS, "report_id")), params).scalar_one())
                except IntegrityError as exc:
                    raise DatabaseError(f"concurrent insert of the same report version: {exc.orig}") from exc
                outcome = "inserted"
            self._insert_fundamentals(conn, report_id, fundamentals)
        return report_id, outcome

    @staticmethod
    def _insert_fundamentals(conn: Connection, report_id: int, fundamentals: list[FundamentalRecord]) -> None:
        for record in fundamentals:
            params = record.model_dump()
            params["report_id"] = report_id
            params["is_comparative"] = 1 if record.is_comparative else 0
            conn.execute(text(_insert_sql("fundamentals", _FUNDAMENTAL_COLUMNS)), params)

    def notification_is_withdrawn(self, market_source: str, notification_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(conn.execute(text("SELECT TOP 1 1 FROM dbo.reports WHERE market_source=:s AND notification_id=:n AND is_withdrawn=1"), {"s": market_source, "n": notification_id}).scalar())

    def mark_withdrawn(self, market_source: str, notification_id: str) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                text("UPDATE dbo.reports SET is_withdrawn = 1 WHERE market_source = :s AND notification_id = :n AND is_withdrawn = 0"),
                {"s": market_source, "n": notification_id},
            )
        return result.rowcount or 0

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM dbo.reports WHERE report_id = :id"), {"id": report_id}).mappings().first()
        return dict(row) if row else None

    def get_fundamentals(self, report_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM dbo.fundamentals WHERE report_id = :id ORDER BY is_comparative, fiscal_year DESC"), {"id": report_id}
            ).mappings()
            return [dict(row) for row in rows]

    def current_view_row(self, company_id: int, fiscal_year: int, consolidation_scope: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM dbo.vw_fundamentals WHERE company_id = :c AND fiscal_year = :y "
                    "AND fiscal_period = 4 AND consolidation_scope = :s"
                ),
                {"c": company_id, "y": fiscal_year, "s": consolidation_scope},
            ).mappings().first()
        return {**dict(row), "is_comparative": False} if row else None


# -- engine, readiness, bootstrap --------------------------------------------------------------


def _utc_bind_params(conn, cursor, statement, parameters, context, executemany):
    """pyodbc binds datetimes without their offset and SQL Server stores them as +00:00, so
    every aware datetime is converted to UTC wall-clock time first. Naive datetimes are a bug."""

    def convert(value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise DatabaseError("refusing to bind a naive datetime; use timezone-aware UTC values")
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    if isinstance(parameters, (list, tuple)) and executemany:
        parameters = [tuple(convert(v) for v in row) if isinstance(row, tuple) else row for row in parameters]
    elif isinstance(parameters, tuple):
        parameters = tuple(convert(v) for v in parameters)
    elif isinstance(parameters, dict):
        parameters = {k: convert(v) for k, v in parameters.items()}
    return statement, parameters


def make_engine(settings: Settings, **overrides: Any) -> Engine:
    engine = create_engine(settings.sqlalchemy_url(**overrides), pool_pre_ping=True, future=True, hide_parameters=True)
    event.listen(engine, "before_cursor_execute", _utc_bind_params, retval=True)
    return engine


def wait_for_database(engine: Engine, *, timeout_seconds: float = 120.0, interval_seconds: float = 3.0) -> None:
    """Readiness check: SQL Server accepts connections and answers SELECT 1."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except (OperationalError, DBAPIError) as exc:
            last_error = exc
            log.info("database not ready yet: %s", str(exc).splitlines()[0][:160])
            time.sleep(interval_seconds)
    raise DatabaseError(f"database not reachable after {timeout_seconds:.0f}s: {last_error}")


def split_batches(script: str) -> list[str]:
    batches, current = [], []
    for line in script.splitlines():
        if line.strip().upper() == "GO":
            if any(item.strip() for item in current):
                batches.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if any(item.strip() for item in current):
        batches.append("\n".join(current))
    return batches


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise DatabaseError(f"unsafe SQL identifier {name!r}")
    return f"[{name}]"


def init_db(settings: Settings, schema_path: Path) -> None:
    """Create the database, apply the idempotent schema, and create least-privilege accounts.

    Uses the bootstrap login only here. DDL cannot take bind parameters, so names are
    validated as identifiers and passwords are escaped as SQL string literals.
    """
    database = settings.mssql_database
    _identifier(database)
    bootstrap = dict(user=settings.mssql_bootstrap_user, password=settings.mssql_bootstrap_password.get_secret_value())
    logins = {
        settings.mssql_user: (settings.mssql_password.get_secret_value(), "crawler_writer_role"),
        "fundamentals_reader": (settings.mssql_reader_password.get_secret_value(), "fundamentals_reader_role"),
        "grafana_reader": (settings.mssql_grafana_password.get_secret_value(), "fundamentals_reader_role"),
    }
    for name, (password, _) in logins.items():
        _identifier(name)
        if not password or "REPLACE_WITH" in password:
            raise DatabaseError(f"password for login {name} is empty; set it in .env before init-db")

    master = make_engine(settings, database="master", **bootstrap).execution_options(isolation_level="AUTOCOMMIT")
    wait_for_database(master)
    with master.connect() as conn:
        conn.execute(text(f"IF DB_ID({_sql_string(database)}) IS NULL CREATE DATABASE {_identifier(database)}"))
        for name, (password, _) in logins.items():
            conn.execute(
                text(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_sql_string(name)}) "
                    f"CREATE LOGIN {_identifier(name)} WITH PASSWORD = {_sql_string(password)}, CHECK_POLICY = OFF"
                )
            )
    master.dispose()

    target = make_engine(settings, database=database, **bootstrap).execution_options(isolation_level="AUTOCOMMIT")
    with target.connect() as conn:
        existing = conn.execute(
            text("SELECT MAX(version) FROM dbo.schema_migrations") if _table_exists(conn, "schema_migrations") else text("SELECT NULL")
        ).scalar()
        if existing is not None and int(existing) > SCHEMA_VERSION:
            raise DatabaseError(f"database schema version {existing} is newer than this application supports ({SCHEMA_VERSION})")
        for batch in split_batches(schema_path.read_text("utf-8")):
            conn.execute(text(batch))
        for name, (_, role) in logins.items():
            conn.execute(
                text(
                    f"IF DATABASE_PRINCIPAL_ID({_sql_string(name)}) IS NULL CREATE USER {_identifier(name)} FOR LOGIN {_identifier(name)}; "
                    f"ALTER ROLE {_identifier(role)} ADD MEMBER {_identifier(name)};"
                )
            )
    target.dispose()


def _table_exists(conn: Connection, name: str) -> bool:
    return conn.execute(text("SELECT OBJECT_ID(:n, 'U')"), {"n": f"dbo.{name}"}).scalar() is not None
