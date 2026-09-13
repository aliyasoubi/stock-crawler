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
from .fetch import PacedClient
from .kap import KAP_HOSTS, build_source_client
from .pipeline import Pipeline, SyncOptions
from .storage import RawStore, RunLocked, RunLock, StateStore, dump_json

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


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
    except (SettingsError, CsvError, DatabaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
