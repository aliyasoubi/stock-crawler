"""Command-line interface: init-db, sync, reprocess, compare, companies-from-csv."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import httpx

from . import __version__
from .compare import compare_reports, render_table
from .config import Settings, SettingsError, load_company_file, parse_ticker_argument
from .csvtools import CsvError, extract_candidate_tickers
from .db import DatabaseError, Repository, init_db, make_engine, wait_for_database
from .fetch import FetchError, PacedClient
from .kap import KAP_HOSTS, SourceError, build_source_client, capture_fixture, probe_source
from .models import CompanyIdentity, ConsolidationScope, FilingCandidate
from .units import parse_source_timestamp
from .pipeline import Pipeline, SyncOptions
from .storage import RawStore, RunLocked, RunLock, StateStore, dump_json

import os


def _resolve_schema_path() -> Path:
    """sql/schema.sql from SCHEMA_PATH, the source checkout, or the working directory (/app in Docker)."""
    candidates = [
        Path(os.environ["SCHEMA_PATH"]) if os.environ.get("SCHEMA_PATH") else None,
        Path(__file__).resolve().parents[2] / "sql" / "schema.sql",
        Path.cwd() / "sql" / "schema.sql",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return candidates[1]


SCHEMA_PATH = _resolve_schema_path()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-crawler", description="KAP annual fundamentals to SQL Server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", type=Path, default=None, help="alternative .env file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create database objects and least-privilege accounts (bootstrap credentials)")

    sync = sub.add_parser("sync", help="discover due companies, capture filings, parse, publish")
    sync.add_argument("--tickers", type=str, help="comma-separated one-off selection replacing the company file")
    sync.add_argument("--company-file", type=Path, help="alternative company list")
    sync.add_argument("--limit", type=int, help="cap eligible companies for this run")
    sync.add_argument("--refresh", action="store_true", help="ignore the discovery freshness interval")

    reprocess = sub.add_parser("reprocess", help="re-parse stored snapshots with the current parser (no HTTP)")
    reprocess.add_argument("--tickers", type=str)
    reprocess.add_argument("--company-file", type=Path)

    compare = sub.add_parser("compare", help="show field differences between two stored report versions (no HTTP)")
    compare.add_argument("--before-report-id", type=int, required=True)
    compare.add_argument("--after-report-id", type=int, required=True)
    compare.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    csv_cmd = sub.add_parser("companies-from-csv", help="extract candidate tickers from a CSV column (no HTTP)")
    csv_cmd.add_argument("--input", type=Path, required=True)
    csv_cmd.add_argument("--ticker-column", type=str, default="stock_code")
    csv_cmd.add_argument("--output", type=Path, required=True)

    probe = sub.add_parser("probe-source", help="live smoke check: two paced GETs (robots.txt, site root); no crawling")
    probe.add_argument("--base-url", type=str, default="https://www.kap.org.tr")

    capture = sub.add_parser("capture-fixture", help="download ONE operator-supplied filing URL into the fixture layout for offline parsing")
    capture.add_argument("--url", type=str, required=True, help="exact notification URL taken from the browser")
    capture.add_argument("--ticker", type=str, required=True)
    capture.add_argument("--source-company-id", type=str, required=True, help="stable KAP company id shown for this issuer")
    capture.add_argument("--company-name", type=str, default=None)
    capture.add_argument("--notification-id", type=str, required=True)
    capture.add_argument("--published-at", type=str, required=True, help="e.g. '05.03.2025 18:45:00' (Istanbul) or ISO-8601")
    capture.add_argument("--fiscal-year", type=int, required=True)
    capture.add_argument("--period-end", type=str, required=True, help="YYYY-MM-DD")
    capture.add_argument("--scope", choices=["consolidated", "unconsolidated"], required=True)
    capture.add_argument("--annual", action="store_true", default=True)
    capture.add_argument("--statement-type", type=str, default="general")
    capture.add_argument("--output-dir", type=Path, default=None, help="default: DATA_DIR/captures")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    if args.env_file:
        return Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    return Settings()


def _tickers(args: argparse.Namespace, settings: Settings) -> list[str]:
    if getattr(args, "tickers", None):
        return parse_ticker_argument(args.tickers)
    return load_company_file(getattr(args, "company_file", None) or settings.company_file)


def _pipeline(settings: Settings, repo: Repository) -> tuple[Pipeline, PacedClient | None]:
    state = StateStore(settings.data_dir)
    holder: dict[str, PacedClient] = {}

    def fetcher_factory() -> PacedClient:
        client = httpx.Client(headers={"User-Agent": settings.http_user_agent})
        holder["fetcher"] = PacedClient(client, settings, state, allowed_hosts=KAP_HOSTS)
        return holder["fetcher"]

    source = build_source_client(settings, fetcher_factory)
    pipeline = Pipeline(
        settings, repo, RawStore(settings.data_dir), state, source,
        request_attempts=lambda: holder["fetcher"].attempts if "fetcher" in holder else 0,
    )
    return pipeline, holder.get("fetcher")


def _print_summary(summary) -> None:
    rows = summary.data["companies"]
    print(f"run {summary.run_id}: {len(rows)} companies, {summary.data['request_attempts']} HTTP attempts")
    for row in rows:
        extras = []
        for key in ("notification_id", "report_id", "persist", "snapshot", "error"):
            if row.get(key) not in (None, ""):
                extras.append(f"{key}={row[key]}")
        if row.get("changed_fields"):
            extras.append(f"changed={','.join(row['changed_fields'])}")
        print(f"  {row['ticker']:<8} {row.get('status', '?'):<16} {'  '.join(extras)}")
    if summary.data.get("stopped_reason"):
        print(f"stopped: {summary.data['stopped_reason']}")
        print(f"pending: {', '.join(summary.data.get('pending', [])) or '-'}")
    print(f"summary: {summary.path}")


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings(args)
    init_db(settings, SCHEMA_PATH)
    print(f"database {settings.mssql_database} initialised (schema {SCHEMA_PATH.name})")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    settings = _settings(args)
    tickers = _tickers(args, settings)
    engine = make_engine(settings)
    wait_for_database(engine)
    repo = Repository(engine)
    with RunLock(settings.data_dir):
        pipeline, _ = _pipeline(settings, repo)
        summary = pipeline.sync(SyncOptions(tickers=tickers, refresh=args.refresh, limit=args.limit))
    _print_summary(summary)
    return 2 if summary.data.get("stopped_reason") else 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    settings = _settings(args)
    tickers = _tickers(args, settings)
    engine = make_engine(settings)
    wait_for_database(engine)
    repo = Repository(engine)
    with RunLock(settings.data_dir):
        pipeline, _ = _pipeline(settings, repo)
        summary = pipeline.reprocess(tickers)
    _print_summary(summary)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    settings = _settings(args)
    engine = make_engine(settings)
    wait_for_database(engine)
    repo = Repository(engine)
    before, after = repo.get_report(args.before_report_id), repo.get_report(args.after_report_id)
    if before is None or after is None:
        print("report id not found", file=sys.stderr)
        return 1
    comparison = compare_reports(before, repo.get_fundamentals(before["report_id"]), after, repo.get_fundamentals(after["report_id"]))
    if args.json:
        payload = {
            "classification": comparison.classification,
            "notes": comparison.notes,
            "rows": [{"key": r.key, "status": r.status, "diffs": [d.__dict__ for d in r.diffs]} for r in comparison.rows],
        }
        print(dump_json(payload))
    else:
        print(render_table(comparison))
    return 0


def _fetcher(settings: Settings) -> PacedClient:
    client = httpx.Client(headers={"User-Agent": settings.http_user_agent})
    return PacedClient(client, settings, StateStore(settings.data_dir), allowed_hosts=KAP_HOSTS)


def cmd_probe_source(args: argparse.Namespace) -> int:
    settings = _settings(args)
    fetcher = _fetcher(settings)
    results = probe_source(fetcher, args.base_url)
    for item in results:
        print(f"{item.outcome:<10} {item.status if item.status is not None else '-':>4}  {item.url}  {item.detail}")
        if item.robots_excerpt:
            print("---- robots.txt (first 4000 bytes) ----")
            print(item.robots_excerpt)
            print("---------------------------------------")
    print(f"HTTP attempts used: {fetcher.attempts}")
    return 0 if results and all(r.outcome == "ok" for r in results) else 2


def cmd_capture_fixture(args: argparse.Namespace) -> int:
    from datetime import date

    settings = _settings(args)
    fetcher = _fetcher(settings)
    identity = CompanyIdentity(source_company_id=args.source_company_id, ticker=args.ticker.upper(), company_name=args.company_name)
    candidate = FilingCandidate(
        notification_id=args.notification_id,
        published_at=parse_source_timestamp(args.published_at),
        fiscal_year=args.fiscal_year,
        period_end_date=date.fromisoformat(args.period_end),
        period_label="Yıllık" if args.annual else None,
        is_annual=args.annual,
        consolidation_scope=ConsolidationScope(args.scope),
        statement_type=args.statement_type,
        source_url=args.url,
    )
    output_dir = args.output_dir or (settings.data_dir / "captures")
    with RunLock(settings.data_dir):
        path = capture_fixture(fetcher, url=args.url, output_dir=output_dir, identity=identity, candidate=candidate)
    print(f"captured {path} ({path.stat().st_size} bytes); HTTP attempts used: {fetcher.attempts}")
    print(f"next: set SOURCE_MODE=fixture and FIXTURE_SOURCE_DIR={output_dir} then run `sync --tickers {identity.ticker}` to parse it offline")
    return 0


def cmd_companies_from_csv(args: argparse.Namespace) -> int:
    stats = extract_candidate_tickers(args.input, args.ticker_column, args.output)
    print(json.dumps(stats.__dict__, indent=2))
    print(f"wrote {stats.candidates} candidate codes to {args.output} (zero KAP requests)")
    return 0


COMMANDS = {
    "init-db": cmd_init_db,
    "sync": cmd_sync,
    "reprocess": cmd_reprocess,
    "compare": cmd_compare,
    "companies-from-csv": cmd_companies_from_csv,
    "probe-source": cmd_probe_source,
    "capture-fixture": cmd_capture_fixture,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        level = _settings(args).log_level if args.command != "companies-from-csv" else "INFO"
    except Exception:  # settings errors are reported by the command itself
        level = "INFO"
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return COMMANDS[args.command](args)
    except RunLocked as exc:
        print(f"refusing to overlap another run: {exc}", file=sys.stderr)
        return 3
    except (SettingsError, CsvError, DatabaseError, SourceError, FetchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
