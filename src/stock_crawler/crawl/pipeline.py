"""Sync / reprocess orchestration: discover, snapshot, parse locally, persist, summarise."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .compare import compare_rows
from ..core.config import Settings
from ..core.db import IdentityConflict, RepositoryLike
from .fetch import AccessBlocked, BudgetExhausted, FetchError, HostCoolingDown, HostThrottled
from .kap import SourceClient, SourceError, select_latest_annual
from ..core.models import ANNUAL_PERIOD, CompanyIdentity, FilingCandidate, FundamentalRecord, ParsedReport, ParseStatus, ReportRecord
from .parser import PARSER_VERSION, parse_snapshot
from ..core.storage import Clock, RawStore, RunSummary, SnapshotMissing, SnapshotRef, StateStore, StorageError, dump_json, utcnow

log = logging.getLogger(__name__)

RUN_STOPPING_ERRORS = (BudgetExhausted, HostCoolingDown, AccessBlocked, HostThrottled)
# Per-year outcomes that settle a requested year until the discovery interval elapses.
# "error" is deliberately absent: a fetch/storage error leaves the year unchecked.
CHECKED_STATUSES = ("published", "already_parsed", "no_filing", "unsupported", "failed", "withdrawn")


@dataclass(frozen=True)
class SyncOptions:
    tickers: list[str]
    refresh: bool = False
    limit: int | None = None


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        repo: RepositoryLike,
        raw: RawStore,
        state: StateStore,
        source: SourceClient,
        *,
        clock: Clock = utcnow,
        parser_version: str = PARSER_VERSION,
        request_attempts: Callable[[], int] = lambda: 0,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.raw = raw
        self.state = state
        self.source = source
        self.clock = clock
        self.parser_version = parser_version
        self.request_attempts = request_attempts

    # -- sync ---------------------------------------------------------------------------------

    def sync(self, options: SyncOptions) -> RunSummary:
        summary = RunSummary(self.settings.data_dir, "sync", clock=self.clock)
        summary.data["requested_tickers"] = options.tickers
        summary.data["refresh"] = options.refresh
        summary.data["requested_years"] = list(self.settings.kap_years)
        summary.data["parser_version"] = self.parser_version
        try:
            return self._sync(options, summary)
        except Exception as exc:
            # Preserve diagnostics for unexpected failures without treating them as success
            # or writing exception text that could contain database credentials.
            summary.data["stopped_reason"] = f"unexpected {type(exc).__name__}; inspect the command error"
            summary.data["error_type"] = type(exc).__name__
            self._finish(summary)
            log.error("Run failed; diagnostic summary: %s", summary.path)
            raise

    def _sync(self, options: SyncOptions, summary: RunSummary) -> RunSummary:
        now = self.clock()
        if options.limit is not None and options.limit <= 0:
            raise ValueError("--limit must be positive")

        eligible: list[tuple[str, CompanyIdentity, Any]] = []
        for position, ticker in enumerate(options.tickers):
            try:
                identity, company = self._resolve(ticker)
            except RUN_STOPPING_ERRORS as exc:
                summary.data["stopped_reason"] = str(exc)
                summary.data["pending"] = options.tickers[position:]
                summary.add_company({"ticker": ticker, "status": "stopped", "error": str(exc)})
                return self._finish(summary)
            except (SourceError, IdentityConflict, FetchError) as exc:
                summary.add_company({"ticker": ticker, "status": "unresolved", "error": str(exc)})
                continue
            if not options.refresh and self._is_fresh(company, now):
                summary.add_company({"ticker": ticker, "status": "fresh", "last_discovery_at": company.last_discovery_at})
                continue
            eligible.append((ticker, identity, company))

        eligible.sort(key=lambda item: (item[2].last_discovery_at is not None, item[2].last_discovery_at or now, item[0]))
        cap = min(options.limit or self.settings.max_companies_per_run, self.settings.max_companies_per_run)
        summary.data["effective_company_limit"] = cap
        summary.data["eligible_company_count"] = len(eligible)
        summary.data["selected_company_count"] = min(len(eligible), cap)
        if options.limit and options.limit > cap:
            log.warning("--limit %s is capped at MAX_COMPANIES_PER_RUN=%s", options.limit, cap)
        deferred = eligible[cap:]
        for ticker, _, _ in deferred:
            summary.add_company({"ticker": ticker, "status": "deferred", "reason": f"company cap {cap} reached"})
        eligible = eligible[:cap]
        if hasattr(self.source, "plan"):
            self.source.plan([identity for _, identity, _ in eligible])

        for index, (ticker, identity, company) in enumerate(eligible):
            try:
                entries = self._sync_company(identity, company)
            except RUN_STOPPING_ERRORS as exc:
                summary.data["stopped_reason"] = str(exc)
                summary.data["pending"] = [t for t, _, _ in eligible[index:]] + [t for t, _, _ in deferred]
                summary.add_company({"ticker": ticker, "status": "stopped", "error": str(exc)})
                self.repo.record_discovery(company.company_id, at=self.clock(), notification_id=None, error=str(exc))
                break
            except (SourceError, FetchError, StorageError) as exc:
                log.warning("%s: %s", ticker, exc)
                self.repo.record_discovery(company.company_id, at=self.clock(), notification_id=None, error=str(exc))
                entries = [{"ticker": ticker, "status": "error", "error": str(exc)}]
            for entry in entries:
                summary.add_company(entry)
        return self._finish(summary)

    def _finish(self, summary: RunSummary) -> RunSummary:
        # Rows the source client could not match or decode are coverage loss, not noise.
        # Without this they only reached the log and the run still exited 0.
        rejected = list(getattr(self.source, "rejected_rows", []))
        excluded = list(getattr(self.source, "excluded_tickers", []))
        normalized = list(getattr(self.source, "normalized_matches", []))
        summary.data["rejected_rows"] = rejected
        summary.data["rejected_row_count"] = len(rejected)
        summary.data["excluded_tickers"] = excluded
        summary.data["excluded_ticker_count"] = len(excluded)
        summary.data["normalized_title_matches"] = normalized
        summary.data["normalized_title_match_count"] = len(normalized)
        summary.finish(request_attempts=self.request_attempts())
        return summary

    def _resolve(self, ticker: str) -> tuple[CompanyIdentity, Any]:
        company = self.repo.get_company_by_ticker(self.source.market_source, ticker)
        if company is None:
            identity = self.source.resolve_company(ticker)
            company = self.repo.upsert_company(identity)
        identity = CompanyIdentity(
            market_source=company.market_source,
            source_company_id=company.source_company_id,
            ticker=company.ticker,
            company_name=company.company_name,
            yahoo_ticker=company.yahoo_ticker,
        )
        return identity, company

    def _is_fresh(self, company: Any, now: datetime) -> bool:
        """Recent AND every requested fiscal year checked within the discovery interval.

        `last_discovery_at` alone is not enough: it says nothing about which years were asked
        for. Without the coverage check, changing KAP_YEARS for a historical backfill would
        skip every company as fresh and collect nothing.

        For all-years sources a year counts as checked whatever the outcome (published,
        already parsed, no filing, unsupported format, failed parse): the source was asked and
        answered. Re-asking every run changed nothing in the September 19 runs except burning
        ~40% of the request budget on the same empty answers; the interval re-checks them.
        A company-level `last_error` is not consulted for those sources because one year's
        parse failure says nothing about the other years, and the run summary carries the
        per-year detail.
        """
        if company.last_discovery_at is None:
            return False
        if now - company.last_discovery_at >= timedelta(hours=self.settings.discovery_interval_hours):
            return False
        if not getattr(self.source, "publishes_all_years", False):
            return not company.last_error  # notification-style: latest filing, retry after errors
        wanted = set(getattr(self.settings, "kap_years", []) or [])
        if not wanted:
            return True
        return wanted.issubset(self.state.fresh_coverage(
            self._coverage_key(company), now=now,
            max_age=timedelta(hours=self.settings.discovery_interval_hours),
        ))

    def _coverage_key(self, company: Any) -> str:
        # Fetch coverage is a property of the source data, not of the parser: a parser upgrade
        # is applied to the stored snapshots by `reprocess` (zero HTTP), never by re-downloading.
        return f"{self.source.market_source}/{company.source_company_id}"

    def _sync_company(self, identity: CompanyIdentity, company: Any) -> list[dict[str, Any]]:
        """One summary entry per published record: a single latest annual filing for
        notification-style sources, or one per fiscal year for comparison exports."""
        candidates = self.source.list_financial_filings(identity)
        if getattr(self.source, "publishes_all_years", False):
            by_year: dict[int, list[FilingCandidate]] = {}
            for candidate in candidates:
                by_year.setdefault(candidate.fiscal_year, []).append(candidate)
            selections = [(year, select_latest_annual(by_year.get(year, []))) for year in sorted(self.settings.kap_years)]
        else:
            selections = [(None, select_latest_annual(candidates))]

        all_years = bool(getattr(self.source, "publishes_all_years", False))
        entries: list[dict[str, Any]] = []
        checked: dict[int, str] = {}
        for requested_year, selection in selections:
            entry: dict[str, Any] = {"ticker": identity.ticker, "candidates": selection.considered}
            if requested_year is not None:
                entry["fiscal_year"] = requested_year
            for withdrawn in selection.withdrawn:
                affected = self.repo.mark_withdrawn(identity.market_source, withdrawn.notification_id)
                if affected:
                    entry.setdefault("withdrawn", []).append(withdrawn.notification_id)
            if selection.selected is None:
                # For an all-years source an empty year is an answer about that year, not a
                # company error: the batch request succeeded and simply had no row for it.
                self.repo.record_discovery(company.company_id, at=self.clock(), notification_id=None,
                                           error=None if all_years else selection.reason)
                entry.update(status="no_filing", error=selection.reason)
                entries.append(entry)
                if requested_year is not None:
                    checked[requested_year] = "no_filing"
                continue
            selected = selection.selected
            self.repo.record_discovery(company.company_id, at=self.clock(), notification_id=selected.notification_id, error=None)
            entry["notification_id"] = selected.notification_id
            entry["fiscal_year"] = selected.fiscal_year
            ref, snapshot_status = self._ensure_snapshot(identity, selected, force_download=bool(company.last_error))
            entry["snapshot"] = snapshot_status
            entry.update(self._parse_and_persist(company, identity, ref))
            if all_years and entry.get("status") in CHECKED_STATUSES:
                checked[selected.fiscal_year] = entry["status"]
            entries.append(entry)
        if all_years and checked:
            self.state.add_coverage(self._coverage_key(company), sorted(checked), statuses=checked)
        return entries

    def _ensure_snapshot(self, identity: CompanyIdentity, candidate: FilingCandidate, *, force_download: bool = False) -> tuple[SnapshotRef, str]:
        existing = self.raw.snapshots_for_notification(identity.market_source, identity.source_company_id, candidate.notification_id)
        latest = self._latest_by_retrieval(existing)
        state_key = f"{identity.market_source}/{identity.source_company_id}/{candidate.notification_id}"
        now = self.clock()
        if latest is not None and not candidate.is_correction and not force_download and identity.market_source != "kap_compare":
            reval = self.state.revalidation(state_key)
            last = reval.get("last_revalidated_at") or self.raw.read_manifest(latest).get("first_retrieved_at")
            if last and now - datetime.fromisoformat(last) < timedelta(hours=self.settings.report_revalidate_hours):
                return latest, "reused (revalidation not due)"

        validator_map = self.state.revalidation(state_key).get("validators", {})
        if hasattr(self.source, "set_validators"):
            self.source.set_validators(candidate.notification_id, validator_map)
        download = self.source.fetch_filing(identity, candidate)
        if download.not_modified:
            if latest is None:
                raise StorageError(f"source answered 304 for {candidate.notification_id} but no snapshot exists")
            self.state.set_revalidation(state_key)
            return latest, "revalidated (304)"

        manifest = {
            **download.provenance,
            "primary_file": next((n for n in sorted(download.files) if n.endswith(".html")), sorted(download.files)[0]),
            "published_at": candidate.published_at.isoformat(),
            "first_retrieved_at": download.retrieved_at.isoformat(),
            "fiscal_year": candidate.fiscal_year,
            "period_end_date": candidate.period_end_date.isoformat() if candidate.period_end_date else None,
            "consolidation_scope": candidate.consolidation_scope.value if candidate.consolidation_scope else None,
            "statement_type": candidate.statement_type,
            "is_correction": candidate.is_correction,
            "source_url": candidate.source_url,
            "document_url": candidate.document_url,
            "source_urls": download.source_urls,
            "content_types": download.content_types,
            "ticker": identity.ticker,
        }
        ref, created = self.raw.write_snapshot(
            market_source=identity.market_source,
            source_company_id=identity.source_company_id,
            notification_id=candidate.notification_id,
            files=download.files,
            manifest=manifest,
        )
        self.state.set_revalidation(state_key, validators=self._flatten_validators(download.validators))
        if not created:
            return ref, "unchanged (same content hash)"
        return ref, "changed (new content hash)" if latest is not None else "captured"

    @staticmethod
    def _flatten_validators(validators: dict[str, dict[str, str]]) -> dict[str, str]:
        return {f"{name}:{key}": value for name, inner in validators.items() for key, value in inner.items()}

    def _latest_by_retrieval(self, refs: list[SnapshotRef]) -> SnapshotRef | None:
        best: tuple[str, SnapshotRef] | None = None
        for ref in refs:
            retrieved = self.raw.read_manifest(ref).get("first_retrieved_at") or ""
            if best is None or retrieved > best[0]:
                best = (retrieved, ref)
        return best[1] if best else None

    # -- parse and persist (shared by sync and reprocess) ----------------------------------------

    def _parse_and_persist(self, company: Any, identity: CompanyIdentity, ref: SnapshotRef) -> dict[str, Any]:
        if self.repo.notification_is_withdrawn(identity.market_source, ref.notification_id):
            return {"status": "withdrawn", "notification_id": ref.notification_id}
        existing = self.repo.find_report_version(identity.market_source, ref.notification_id, ref.content_hash, self.parser_version)
        manifest = self.raw.read_manifest(ref)
        files = self.raw.read_files(ref)
        if manifest.get("data_product") == "kap_compare":
            from .kap_export import read_export_blob
            files["source.xlsx"] = read_export_blob(self.raw, manifest["export_blob_sha256"])
            manifest["calendar_year_confirmed"] = self.settings.calendar_year_confirmed(identity.ticker)
        if existing is not None and existing.parse_status == ParseStatus.VALID.value:
            self.repo.record_success(company.company_id, at=self.clock())
            return {"status": "already_parsed", "report_id": existing.report_id, "parser_version": self.parser_version}
        parsed = parse_snapshot(files, manifest, parser_version=self.parser_version)
        if manifest.get("retrieved_at_basis") == "import_time_original_capture_unknown":
            parsed.warnings.append("Original export retrieval timestamp is unknown; retrieved_at is import time, not point-in-time market availability.")
        parsed_at = self.clock()
        self.raw.write_parsed(
            ref,
            self.parser_version,
            {
                "parsed_at": parsed_at.isoformat(),
                "notification_id": ref.notification_id,
                "content_hash": ref.content_hash,
                **parsed.model_dump(mode="json"),
            },
        )
        report = self._report_record(company.company_id, identity, ref, manifest, parsed, parsed_at)
        fundamentals: list[FundamentalRecord] = parsed.periods if parsed.parse_status == ParseStatus.VALID else []

        previous_row = None
        if parsed.parse_status == ParseStatus.VALID and parsed.filing_fiscal_year is not None:
            previous_row = self.repo.current_view_row(company.company_id, parsed.filing_fiscal_year, report.consolidation_scope)

        report_id, outcome = self.repo.save_report(report, fundamentals)
        entry: dict[str, Any] = {
            "report_id": report_id,
            "parse_status": parsed.parse_status.value,
            "persist": outcome,
            "parser_version": self.parser_version,
            "warnings": parsed.warnings,
            "errors": parsed.errors,
        }
        if parsed.parse_status == ParseStatus.VALID:
            self.repo.record_success(company.company_id, at=parsed_at)
            entry["status"] = "published"
            current = parsed.current_period()
            if previous_row is not None and current is not None and outcome != "exists":
                try:
                    now_current = self.repo.current_view_row(company.company_id, parsed.filing_fiscal_year, report.consolidation_scope)
                    if now_current is not None and now_current.get("report_id") != report_id:
                        # A later publication already stored for this year/scope still wins in the view.
                        entry["superseded_by_report_id"] = now_current.get("report_id")
                    else:
                        diffs = compare_rows([previous_row], [{**current.model_dump(), "is_comparative": False}])
                        entry["changed_fields"] = [d.field for row in diffs for d in row.diffs]
                        entry["previous_report_id"] = previous_row.get("report_id")
                except Exception as exc:  # the publish is committed; a summary problem must not hide it
                    log.warning("change summary failed for report %s: %s", report_id, exc)
                    entry["change_summary_error"] = str(exc)
        else:
            self.repo.record_discovery(
                company.company_id, at=parsed_at, notification_id=ref.notification_id,
                error=f"parse {parsed.parse_status.value}: {'; '.join(parsed.errors)[:800]}",
            )
            entry["status"] = parsed.parse_status.value
        return entry

    def _report_record(self, company_id: int, identity: CompanyIdentity, ref: SnapshotRef, manifest: dict[str, Any], parsed: ParsedReport, parsed_at: datetime) -> ReportRecord:
        from datetime import date

        period_end = parsed.filing_period_end_date or (date.fromisoformat(manifest["period_end_date"]) if manifest.get("period_end_date") else None)
        if period_end is None:
            period_end = date(int(manifest.get("fiscal_year") or 1900), 12, 31)
        scope = parsed.consolidation_scope.value if parsed.consolidation_scope else (manifest.get("consolidation_scope") or "consolidated")
        return ReportRecord(
            company_id=company_id,
            market_source=identity.market_source,
            notification_id=ref.notification_id,
            published_at=datetime.fromisoformat(manifest["published_at"]),
            filing_fiscal_year=parsed.filing_fiscal_year or int(manifest.get("fiscal_year") or period_end.year),
            filing_fiscal_period=ANNUAL_PERIOD,
            filing_period_start_date=parsed.filing_period_start_date,
            filing_period_end_date=period_end,
            consolidation_scope=scope,
            statement_type=parsed.statement_type or manifest.get("statement_type") or "unknown",
            source_url=manifest.get("source_url"),
            document_url=manifest.get("document_url"),
            raw_path=ref.relative_path,
            content_hash=ref.content_hash,
            parser_version=parsed.parser_version,
            retrieved_at=datetime.fromisoformat(manifest["first_retrieved_at"]),
            parsed_at=parsed_at if parsed.parse_status == ParseStatus.VALID else None,
            parse_status=parsed.parse_status.value,
            validation_summary=dump_json(parsed.validation_summary()),
        )

    def ingest_export_entries(self, entries) -> RunSummary:
        summary = RunSummary(self.settings.data_dir, "import-kap-export", clock=self.clock)
        for item in entries:
            identity = item.identity
            try:
                company = self.repo.upsert_company(identity)
                download = item.download(self.settings.calendar_year_confirmed(identity.ticker))
                candidate = download.candidate
                manifest = {**download.provenance, "primary_file": "source.json", "ticker": identity.ticker,
                    "published_at": candidate.published_at.isoformat(), "first_retrieved_at": download.retrieved_at.isoformat(),
                    "fiscal_year": candidate.fiscal_year, "period_end_date": candidate.period_end_date.isoformat(),
                    "consolidation_scope": candidate.consolidation_scope.value if candidate.consolidation_scope else None,
                    "statement_type": candidate.statement_type, "source_url": candidate.source_url,
                    "source_urls": download.source_urls, "content_types": download.content_types}
                ref, created = self.raw.write_snapshot(market_source=identity.market_source,
                    source_company_id=identity.source_company_id, notification_id=candidate.notification_id,
                    files=download.files, manifest=manifest)
                result = self._parse_and_persist(company, identity, ref)
                summary.add_company({"ticker": identity.ticker, "notification_id": candidate.notification_id, **result})
            except (SourceError, StorageError, IdentityConflict) as exc:
                summary.add_company({"ticker": identity.ticker, "status": "error", "error": str(exc)})
        return self._finish(summary)

    # -- reprocess -----------------------------------------------------------------------------

    def reprocess(self, tickers: list[str]) -> RunSummary:
        """Zero HTTP requests: re-parse every stored snapshot, preserving withdrawals."""
        summary = RunSummary(self.settings.data_dir, "reprocess", clock=self.clock)
        summary.data["requested_tickers"] = tickers
        for ticker in tickers:
            company = self.repo.get_company_by_ticker(self.source.market_source, ticker)
            if company is None:
                summary.add_company({"ticker": ticker, "status": "cache_miss", "error": "company never synced"})
                continue
            identity = CompanyIdentity(
                market_source=company.market_source, source_company_id=company.source_company_id, ticker=company.ticker,
                company_name=company.company_name, yahoo_ticker=company.yahoo_ticker,
            )
            refs = self.raw.list_snapshots(identity.market_source, identity.source_company_id)
            if not refs:
                summary.add_company({"ticker": ticker, "status": "cache_miss", "error": "no raw snapshot stored"})
                continue
            for ref in sorted(refs, key=lambda r: (r.notification_id, r.content_hash)):
                try:
                    entry = {"ticker": ticker, "notification_id": ref.notification_id, **self._parse_and_persist(company, identity, ref)}
                except (SnapshotMissing, StorageError) as exc:
                    entry = {"ticker": ticker, "status": "cache_miss", "error": str(exc)}
                summary.add_company(entry)
        return self._finish(summary)
