"""Command-line interface: init-db, sync, import-kap-export, reprocess, compare, probe-source, ..."""

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
from .storage import RawStore, RunLocked, RunLock, StateStore, StorageError, dump_json
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

import os


def _resolve_schema_path() -> Path:
    """sql/schema.sql from SCHEMA_PATH, the source checkout, or the working directory (/app in Docker)."""
    candidates = [
        Path(os.environ["SCHEMA_PATH"]) if os.environ.get("SCHEMA_PATH") else None,
        Path(__file__).resolve().parent / "sql" / "schema.sql",
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
    imp = sub.add_parser("import-kap-export", help="import legacy/v6 manifests or English KAP XLSX files (zero HTTP)")
    imp.add_argument("--input", type=Path, required=True, nargs="+", help="one or more manifest .json / export .xlsx files")
    imp.add_argument("--tickers", help="for a standalone XLSX: exact requested ticker list")
    imp.add_argument("--dry-run", action="store_true", help="archive and validate; do not open SQL Server")
    imp.add_argument("--output", type=Path, help="write validation/values as JSON")
    generate = sub.add_parser("build-kap-script", help="generate a resumable browser exporter from the company registry")
    generate.add_argument("--tickers")
    generate.add_argument("--company-file", type=Path)
    generate.add_argument("--years", required=True, help="comma-separated, maximum five years")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--max-requests", type=int, default=5)
    generate.add_argument("--batch-size", type=int, default=25)
    client = sub.add_parser("export-company-fundamentals", help="review saved export workbooks against the client CompanyFundamental contract (no HTTP/SQL)")
    client.add_argument("--input", type=Path, nargs="+", help="XLSX files; default: saved data/raw/_exports workbooks")
    client.add_argument("--output", type=Path, required=True, help="review JSON output; contains missing-field diagnostics")
    client.add_argument("--company-map", type=Path, help="JSON object mapping tickers to the client's dbo.Company IDs")
    client.add_argument("--currency", required=True, help="expected target currency, e.g. TRY; values are not converted")
    client.add_argument("--scope", choices=["consolidated", "unconsolidated"], default="consolidated")
    client.add_argument("--net-income-basis", choices=["total-profit", "owners-of-parent"], default="total-profit")
    client.add_argument("--tickers", help="optional comma-separated selection")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    if args.env_file:
        return Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    return Settings()


def _tickers(args: argparse.Namespace, settings: Settings) -> list[str]:
    if getattr(args, "tickers", None):
        return parse_ticker_argument(args.tickers)
    return load_company_file(getattr(args, "company_file", None) or settings.company_file)


def _pipeline(settings: Settings, repo: Repository, *, offline: bool = False) -> tuple[Pipeline, PacedClient | None]:
    state = StateStore(settings.data_dir)
    holder: dict[str, PacedClient] = {}

    def fetcher_factory() -> PacedClient:
        client = httpx.Client(headers={"User-Agent": settings.http_user_agent})
        holder["fetcher"] = PacedClient(client, settings, state, allowed_hosts=KAP_HOSTS)
        return holder["fetcher"]

    if offline:
        from types import SimpleNamespace
        source = SimpleNamespace(market_source="kap_compare" if settings.source_mode == "kap-export" else "kap")
    else:
        source = build_source_client(settings, fetcher_factory)
    pipeline = Pipeline(
        settings, repo, RawStore(settings.data_dir), state, source,
        request_attempts=lambda: holder["fetcher"].attempts if "fetcher" in holder else 0,
    )
    return pipeline, holder.get("fetcher")


def _print_summary(summary) -> None:
    rows = summary.data["companies"]
    company_count = len({r['ticker'] for r in rows})
    print(f"run {summary.run_id}: {company_count} companies, {len(rows)} result entries, {summary.data['request_attempts']} HTTP attempts")
    if "effective_company_limit" in summary.data:
        print(f"company cap: {summary.data['effective_company_limit']}; selected: {summary.data['selected_company_count']}")
    for row in rows:
        extras = []
        for key in ("fiscal_year", "notification_id", "report_id", "persist", "snapshot", "error"):
            if row.get(key) not in (None, ""):
                extras.append(f"{key}={row[key]}")
        if row.get("changed_fields"):
            extras.append(f"changed={','.join(row['changed_fields'])}")
        print(f"  {row['ticker']:<8} {row.get('status', '?'):<16} {'  '.join(extras)}")
    if summary.data.get("stopped_reason"):
        print(f"stopped: {summary.data['stopped_reason']}")
        print(f"pending: {', '.join(summary.data.get('pending', [])) or '-'}")
    print(f"rejected rows: {summary.data.get('rejected_row_count', 0)}; excluded tickers: {summary.data.get('excluded_ticker_count', 0)}")
    for row in summary.data.get("rejected_rows", [])[:10]:
        print(f"  rejected {row['company']}: {row['reason']}")
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
        pipeline, fetcher = _pipeline(settings, repo)
        try:
            summary = pipeline.sync(SyncOptions(tickers=tickers, refresh=args.refresh, limit=args.limit))
        finally:
            if fetcher is not None:
                fetcher.close()
            engine.dispose()
    _print_summary(summary)
    return summary_exit_code(summary)


def cmd_reprocess(args: argparse.Namespace) -> int:
    settings = _settings(args)
    tickers = _tickers(args, settings)
    engine = make_engine(settings)
    wait_for_database(engine)
    repo = Repository(engine)
    with RunLock(settings.data_dir):
        pipeline, _ = _pipeline(settings, repo, offline=True)
        try:
            summary = pipeline.reprocess(tickers)
        finally:
            engine.dispose()
    _print_summary(summary)
    return summary_exit_code(summary)


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
    try:
        with RunLock(settings.data_dir):
            results = probe_source(fetcher, args.base_url)
    finally:
        fetcher.close()
    for item in results:
        print(f"{item.outcome:<10} {item.status if item.status is not None else '-':>4}  {item.url}  {item.detail}")
        if item.robots_excerpt:
            print("---- robots.txt (first 4000 bytes) ----")
            print(item.robots_excerpt)
            print("---------------------------------------")
    print(f"HTTP attempts used: {fetcher.attempts}")
    # robots.txt is informational (KAP answers it with a non-standard status); reachability is judged on the site root.
    reachable = any(r.outcome == "ok" and not r.url.endswith("/robots.txt") for r in results)
    return 0 if reachable else 2


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
    try:
        with RunLock(settings.data_dir):
            path = capture_fixture(fetcher, url=args.url, output_dir=output_dir, identity=identity, candidate=candidate)
    finally:
        fetcher.close()
    print(f"captured {path} ({path.stat().st_size} bytes); HTTP attempts used: {fetcher.attempts}")
    print(f"next: set SOURCE_MODE=fixture and FIXTURE_SOURCE_DIR={output_dir} then run `sync --tickers {identity.ticker}` to parse it offline")
    return 0


def cmd_companies_from_csv(args: argparse.Namespace) -> int:
    stats = extract_candidate_tickers(args.input, args.ticker_column, args.output)
    print(json.dumps(stats.__dict__, indent=2))
    print(f"wrote {stats.candidates} candidate codes to {args.output} (zero KAP requests)")
    return 0


def summary_exit_code(summary) -> int:
    if summary.data.get("stopped_reason"):
        return 2
    errors = {"error", "unresolved", "failed", "unsupported", "cache_miss", "no_filing"}
    return int(bool(summary.data.get("rejected_rows") or summary.data.get("rejected_row_count")
                    or any(c.get("status") in errors for c in summary.data["companies"])))


def cmd_import_kap_export(args: argparse.Namespace) -> int:
    import base64
    import hashlib
    from .kap_export import CompanyRegistry, load_manifest, parse_export_row
    from .storage import write_atomic
    from .parser import PARSER_VERSION

    settings = _settings(args)
    registry = CompanyRegistry(settings.kap_company_registry)
    raw = RawStore(settings.data_dir)
    exit_code = 0
    with RunLock(settings.data_dir):
        entries, errors = [], []
        for path in args.input:
            if path.suffix.lower() == ".xlsx":
                if not args.tickers:
                    raise SettingsError("a standalone XLSX requires --tickers so company identities can be verified")
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise SettingsError("XLSX exceeds 20 MiB")
                data = path.read_bytes()
                wrapped = [{"tickers": parse_ticker_argument(args.tickers), "base64": base64.b64encode(data).decode()}]
                path = settings.data_dir / "imports" / (hashlib.sha256(data).hexdigest() + ".json")
                write_atomic(path, dump_json(wrapped).encode())
            file_entries, file_errors = load_manifest(path, registry, raw)
            print(f"{path}: {len(file_entries)} rows, {len(file_errors)} import errors")
            entries.extend(file_entries)
            errors.extend({"file": str(path), **e} for e in file_errors)
        validation = [{"ticker": e.identity.ticker, "notification_id": e.row["Notification ID"],
            "retrieved_at_basis": "source_capture" if e.retrieved_at_known else "import_time_original_capture_unknown",
            **parse_export_row(e.row, calendar_year_confirmed=settings.calendar_year_confirmed(e.identity.ticker),
                parser_version=PARSER_VERSION).model_dump(mode="json")} for e in entries]
        payload = {"rows": len(entries), "errors": errors, "records": validation}
        if args.output:
            write_atomic(args.output, dump_json(payload).encode())
        print(f"total: {len(entries)} rows; {len(errors)} import errors; {sum(v['parse_status'] == 'valid' for v in validation)} valid annual records")
        for error in errors:
            print(dump_json(error), file=sys.stderr)
        if args.dry_run:
            for row in validation:
                print(f"{row['ticker']} {row['notification_id']}: {row['parse_status']} {'; '.join(row['errors'])}")
            return 0 if entries and not errors and all(v["parse_status"] == "valid" for v in validation) else 1
        engine = make_engine(settings)
        try:
            wait_for_database(engine)
            pipeline, _ = _pipeline(settings, Repository(engine), offline=True)
            summary = pipeline.ingest_export_entries(entries)
            summary.data["import_errors"] = errors
            summary.finish()
            _print_summary(summary)
            exit_code = max(summary_exit_code(summary), int(bool(errors) or not entries))
        finally:
            engine.dispose()
    return exit_code


def cmd_build_kap_script(args: argparse.Namespace) -> int:
    from .kap_export import CompanyRegistry
    from .browser_export import build_browser_script
    from .storage import write_atomic
    settings = _settings(args)
    years = [int(y.strip()) for y in args.years.split(",")]
    registry = CompanyRegistry(settings.kap_company_registry)
    identities = [registry.resolve(t) for t in _tickers(args, settings)]
    script = build_browser_script(identities, years, max_requests=args.max_requests, batch_size=args.batch_size)
    write_atomic(args.output, script.encode())
    print(f"wrote {args.output}: {len(identities)} companies, {len(years)} years; no HTTP requests")
    return 0


from .client_export import cmd_export_company_fundamentals

COMMANDS = {
    "export-company-fundamentals": cmd_export_company_fundamentals,
    "init-db": cmd_init_db,
    "import-kap-export": cmd_import_kap_export,
    "build-kap-script": cmd_build_kap_script,
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
    except SQLAlchemyError as exc:
        print(f"database operation failed ({type(exc).__name__}); check connectivity, credentials, permissions and schema", file=sys.stderr)
        return 1
    except (SettingsError, CsvError, DatabaseError, SourceError, FetchError, StorageError, ValidationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
