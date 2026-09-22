"""KAP comparison XLSX adapter, registry, and legacy/v6 manifest ingestion.

This data product is deliberately named kap_compare. It is a delayed current-column
snapshot, not a notification feed or point-in-time/restatement-complete dataset.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from openpyxl import load_workbook

from .fetch import PacedClient
from .kap import SourceError, UnknownTicker
from ..core.models import CompanyIdentity, ConsolidationScope, FilingCandidate, FilingDownload, FundamentalRecord, ParsedReport, ParseStatus
from .metrics import identity_checks
from ..core.storage import RawStore, StorageError, dump_json, sha256_bytes, utcnow, write_atomic
from ..core.units import decode_presentation_currency, parse_source_timestamp, parse_structured_number, parse_turkish_number

log = logging.getLogger(__name__)

SOURCE = "kap_compare"
EXPORT_URL = "https://www.kap.org.tr/en/api/export/compareItems"
PAGE_URL = "https://www.kap.org.tr/en/kalem-karsilastirma"
# Exact headers observed in a real 2-company / 2-year / 10-item export on 2026-09-14.
FIELDS = {
    "Total Liabilities and Equity": ("total_liabilities_and_equity", "ifrs-full_EquityAndLiabilities"),
    "Profit (Loss) Attributable To, Non-controlling Interests": ("profit_attributable_to_non_controlling_interests", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests"),
    "Profit (Loss) Attributable To, Owners of Parent": ("profit_attributable_to_owners_of_parent", "ifrs-full_ProfitLossAttributableToOwnersOfParent"),
    "Current Liabilities": ("current_liabilities", "ifrs-full_CurrentLiabilities"),
    "Non-current Liabilities": ("non_current_liabilities", "ifrs-full_NoncurrentLiabilities"),
    "Total Equity": ("total_equity", "ifrs-full_Equity"),
    "Total Assets": ("total_assets", "ifrs-full_Assets"),
    "Revenue": ("revenue", "ifrs-full_Revenue"),
    "Net Profit (Loss)": ("net_profit", "ifrs-full_ProfitLoss"),
    "Revenue from Finance Sector Operations": ("finance_sector_revenue", "kap-fr_RevenueFromFinanceSectorOperations"),
}
META_HEADERS = ("Company", "Notification ID", "Publish Date", "Year", "Period", "Nature of Financial Statement", "Presentation Currency", "Sectoral Statement Type")
MAX_WORKBOOK_BYTES = 20 * 1024 * 1024
MAX_MANIFEST_BYTES = 200 * 1024 * 1024
MAX_ROWS = 20000
LIMITATION = "Delayed KAP comparison export: current columns only; prior-period restatements and withdrawal discovery are not covered."
# KAP financial statement formats whose ten exported items carry the same meaning. The HOLDING
# format splits turnover into "Revenue" (non-finance operations) and "Revenue from Finance Sector
# Operations"; both are kept as reported. Bank, insurance and finance formats are not mapped.
SUPPORTED_STATEMENT_TYPES = ("general", "holding")
HOLDING_REVENUE_SPLIT = ("HOLDING format: revenue is non-finance turnover only; finance-sector turnover is reported "
                         "separately in finance_sector_revenue and is not added in.")
REVENUE_FINANCE_ONLY = ("Revenue is not reported; the issuer's turnover appears only under "
                        "finance_sector_revenue (GENERAL-format investment/brokerage holding).")


def name_key(value: str) -> str:
    # Preserve punctuation and diacritics. Names are matched exactly after whitespace/case.
    return " ".join(value.replace("İ", "i").replace("I", "ı").casefold().split())


_LOOSE_TRANSLATE = str.maketrans({"ı": "i", "ö": "o", "ü": "u", "ş": "s", "ç": "c", "ğ": "g", "â": "a", "î": "i", "û": "u"})
_LEGAL_FORM = re.compile(r"\s*\b(t\.?\s*a\.?\s*ş\.?|a\.?\s*ş\.?|anonim\s+şirketi|anonim\s+ortaklığı)\s*$")


def loose_key(value: str) -> str:
    """Spelling-insensitive title key, used only when the exact key finds nothing.

    KAP's own export writes the title as it stood on each notification, and those differ from
    the registry seed by orthography rather than identity: SANAYİ/SANAYİİ, MAMÜLLERİ/MAMULLERİ,
    BRİDGESTONE/BRIDGESTONE, FEDERAL-MOGUL/FEDERAL MOGUL, a dropped "VE" or legal form. The key
    folds diacritics and punctuation, drops "ve" and a trailing legal form, and collapses a
    doubled final i. It never drops or reorders name words, so it cannot merge two issuers whose
    names differ in substance; the registry loader refuses a seed where two tickers share a key.
    """
    text = _LEGAL_FORM.sub("", name_key(value)).translate(_LOOSE_TRANSLATE)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(re.sub(r"ii$", "i", token) for token in text.split() if token != "ve")


class CompanyRegistry:
    """A dated identity seed, not a guarantee that all companies remain listed.

    `aliases_path` is the file `verify-aliases` writes: historical titles proven by a
    single-company export, kept apart from the hand-maintained seed.
    """
    def __init__(self, path: Path, aliases_path: Path | None = None):
        self.entries = json.loads(path.read_text("utf-8-sig"))
        self.by_ticker = {}
        self.names = {}
        self.loose_names = {}
        ids = set()
        for entry in self.entries:
            ticker, sid = entry["ticker"], entry["source_company_id"]
            if not re.fullmatch(r"[A-Z0-9]{2,10}", ticker) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
                raise SourceError("invalid registry identifier")
            if ticker in self.by_ticker or sid in ids:
                raise SourceError("duplicate ticker or company ID in registry")
            ids.add(sid)
            self.by_ticker[ticker] = entry
            for title in [entry["company_name"], *entry.get("aliases", [])]:
                self._add_title(title, ticker)
        self.verified_aliases: list[dict] = []
        if aliases_path is not None and aliases_path.is_file():
            document = json.loads(aliases_path.read_text("utf-8-sig"))
            for alias in document.get("aliases", []):
                if alias["ticker"] not in self.by_ticker:
                    raise SourceError(f"verified alias for unknown ticker {alias['ticker']}")
                self._add_title(alias["title"], alias["ticker"])
                self.verified_aliases.append(alias)

    def _add_title(self, title: str, ticker: str) -> None:
        key = name_key(title)
        if key in self.names and self.names[key] != ticker:
            raise SourceError(f"ambiguous company title in registry: {title}")
        self.names[key] = ticker
        loose = loose_key(title)
        if loose in self.loose_names and self.loose_names[loose] != ticker:
            raise SourceError(f"ambiguous company title in registry after normalization: {title}")
        self.loose_names[loose] = ticker

    def resolve(self, ticker: str) -> CompanyIdentity:
        entry = self.by_ticker.get(ticker)
        if not entry:
            raise UnknownTicker(f"{ticker} is not in the KAP registry; add a verified ID and title")
        return CompanyIdentity(market_source=SOURCE, **{k: entry[k] for k in ("ticker", "source_company_id", "company_name")})

    def match(self, title: str, allowed_tickers: list[str] | None = None) -> CompanyIdentity:
        ticker, _ = self.match_with_basis(title, allowed_tickers)
        return self.resolve(ticker)

    def match_with_basis(self, title: str, allowed_tickers: list[str] | None = None) -> tuple[str, str]:
        """Return (ticker, basis) where basis is 'exact' or 'normalized'."""
        ticker, basis = self.names.get(name_key(title)), "exact"
        if ticker is None:
            ticker, basis = self.loose_names.get(loose_key(title)), "normalized"
        if ticker is None or (allowed_tickers is not None and ticker not in allowed_tickers):
            raise UnknownTicker(f"unmatched export company {title!r}; run verify-aliases or add a verified alias to the registry")
        return ticker, basis


def export_payload(identities: list[CompanyIdentity], years: list[int]) -> dict:
    if not identities or len(identities) > 25 or not years or len(years) > 5:
        raise SourceError("an export request needs 1-25 companies and 1-5 years")
    return {"companyType": "IGS", "mkkMemberIdList": [i.source_company_id for i in identities],
            "mkkMemberTitleList": [i.company_name for i in identities], "yearList": [str(y) for y in sorted(years)],
            "periodList": ["4"], "itemIdList": [v[1] for v in FIELDS.values()], "sectors": ["GENERAL"]}


def read_export(data: bytes, *, extra_headers=()) -> list[dict]:
    """Validate XLSX structure before parsing. Never interpret HTML/JSON errors as data."""
    if len(data) > MAX_WORKBOOK_BYTES or not data.startswith(b"PK\x03\x04"):
        raise SourceError("response is not a supported XLSX workbook (or exceeds 20 MiB)")
    try:
        with ZipFile(BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 1000 or sum(i.file_size for i in infos) > 100 * 1024 * 1024:
                raise SourceError("workbook exceeds expanded-size limits")
            if "xl/workbook.xml" not in z.namelist() or "[Content_Types].xml" not in z.namelist():
                raise SourceError("ZIP is not an XLSX workbook")
            if any(i.filename.endswith("vbaProject.bin") or "externalLinks/" in i.filename for i in infos):
                raise SourceError("macros/external links are not supported in source exports")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style")
            workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
        results = []
        try:
            if len(workbook.worksheets) != 1:
                raise SourceError("expected one KAP export worksheet; layout requires review")
            sheet = workbook.worksheets[0]
            if sheet.max_row and sheet.max_row > MAX_ROWS or sheet.max_column and sheet.max_column > 100:
                raise SourceError("workbook dimensions exceed import limits")
            headers = None
            for number, cells in enumerate(sheet.iter_rows(), 1):
                if number > MAX_ROWS:
                    raise SourceError("workbook exceeds row limit")
                if any(c.data_type in ("f", "e") for c in cells):
                    raise SourceError(f"formula or spreadsheet error at row {number}; refusing cached/guessed values")
                values = [c.value for c in cells]
                if not any(v not in (None, "") for v in values):
                    continue
                if headers is None:
                    if values[:len(META_HEADERS)] == list(META_HEADERS):
                        headers = [str(v).strip() if v is not None else "" for v in values]
                        if len(set(headers)) != len(headers) or any(h not in (*META_HEADERS, *FIELDS, *extra_headers) for h in headers):
                            raise SourceError("duplicate or unknown export header; layout requires review")
                    elif number > 30:
                        raise SourceError("KAP English export header not found in first 30 rows")
                    continue
                if len(values) != len(headers):
                    raise SourceError(f"row {number} width differs from header")
                row = {key: (None if value == "" else value) for key, value in zip(headers, values)}
                if not row.get("Company") or not re.fullmatch(r"[0-9]{1,32}", str(row.get("Notification ID", ""))):
                    raise SourceError(f"invalid company/notification identity at row {number}")
                # Date cells are not expected in the observed source; support native datetime explicitly.
                if isinstance(row["Publish Date"], datetime):
                    row["Publish Date"] = row["Publish Date"].isoformat()
                results.append(row)
            if headers is None:
                raise SourceError("KAP export header missing (possible error workbook)")
            return results
        finally:
            workbook.close()
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"invalid XLSX export: {type(exc).__name__}: {exc}") from exc


def row_candidate(row: dict) -> FilingCandidate:
    try:
        year, period = int(row["Year"]), int(row["Period"])
        if not 2000 <= year <= 2100 or str(row["Year"]) != str(year) or str(row["Period"]) != str(period):
            raise ValueError("invalid year/period")
        scope = {"consolidated": ConsolidationScope.CONSOLIDATED, "unconsolidated": ConsolidationScope.UNCONSOLIDATED}.get(str(row["Nature of Financial Statement"]).strip().lower())
        return FilingCandidate(notification_id=str(row["Notification ID"]), published_at=parse_source_timestamp(str(row["Publish Date"])),
            fiscal_year=year, period_end_date=date(year, 12, 31), is_annual=period == 4,
            consolidation_scope=scope, statement_type=str(row["Sectoral Statement Type"]).lower(),
            source_url=PAGE_URL)
    except (ValueError, TypeError, KeyError) as exc:
        raise SourceError(f"invalid export metadata: {exc}") from exc


def parse_export_row(row: dict, *, calendar_year_confirmed: bool, parser_version: str) -> ParsedReport:
    report = ParsedReport(parser_version=parser_version, parse_status=ParseStatus.FAILED, warnings=[LIMITATION])
    try:
        candidate = row_candidate(row)
        report.filing_fiscal_year = candidate.fiscal_year
        report.consolidation_scope = candidate.consolidation_scope
        report.statement_type = candidate.statement_type
        if not candidate.is_annual or candidate.statement_type not in SUPPORTED_STATEMENT_TYPES:
            report.parse_status = ParseStatus.UNSUPPORTED
            report.errors.append("only annual GENERAL and HOLDING comparison exports have a reviewed mapping")
            return report
        if not calendar_year_confirmed:
            report.parse_status = ParseStatus.UNSUPPORTED
            report.errors.append("export omits exact period dates and this issuer is listed in KAP_NON_CALENDAR_YEAR_TICKERS")
            return report
        if candidate.consolidation_scope is None:
            raise SourceError("unknown consolidation scope")
        currency, scale = decode_presentation_currency(str(row["Presentation Currency"]))
        values, sources = {}, {}
        for header, (field, item_id) in FIELDS.items():
            raw = row.get(header)
            amount = parse_turkish_number(raw) if isinstance(raw, str) else parse_structured_number(raw)
            if amount is not None and not amount.is_finite():
                raise SourceError(f"non-finite {field}")
            values[field] = None if amount is None else amount * scale
            sources[field] = {"label": header, "item_id": item_id, "raw_value": raw, "scaled_by": scale}
        required = ("total_assets", "total_liabilities_and_equity", "current_liabilities", "non_current_liabilities", "total_equity", "net_profit")
        absent = [f for f in required if values[f] is None]
        # Brokerage/investment holdings filed in the GENERAL format report their turnover only
        # under "Revenue from Finance Sector Operations"; that is their revenue line, not a gap.
        # It stays in its own column so the client mapping can say which one it used.
        if values["revenue"] is None and values["finance_sector_revenue"] is None:
            absent.append("revenue")
        if absent:
            raise SourceError("missing required values: " + ", ".join(absent))
        end, start = candidate.period_end_date, date(candidate.fiscal_year, 1, 1)
        report.filing_period_start_date, report.filing_period_end_date = start, end
        report.warnings.append("Period dates inferred from Year + Period=4 for an explicitly confirmed calendar-year issuer.")
        if values["revenue"] is None:
            report.warnings.append(REVENUE_FINANCE_ONLY)
        if candidate.statement_type == "holding":
            report.warnings.append(HOLDING_REVENUE_SPLIT)
        report.warnings.extend(identity_checks(values, tolerance=Decimal(scale) * 3))
        report.periods = [FundamentalRecord(fiscal_year=candidate.fiscal_year, period_start_date=start, period_end_date=end,
            is_comparative=False, currency_code=currency, currency_scale=scale, measuring_unit_date=end,
            presentation_currency_raw=str(row["Presentation Currency"]),
            total_debt_method="missing", ebitda_method="missing", free_cash_flow_method="missing", shares_outstanding_method="missing", **values)]
        report.field_sources = {f"{candidate.fiscal_year}:current": sources}
        report.parse_status = ParseStatus.VALID
    except (SourceError, ValueError, TypeError, KeyError) as exc:
        report.errors.append(str(exc))
    return report


def archive_export(raw: RawStore, data: bytes) -> str:
    digest = sha256_bytes(data)
    path = raw.root / "_exports" / digest / "source.xlsx"
    if not path.exists():
        write_atomic(path, data)
    elif sha256_bytes(path.read_bytes()) != digest:
        raise StorageError("existing export blob is corrupt")
    return digest


def read_export_blob(raw: RawStore, digest: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise StorageError("invalid export blob hash")
    path = raw.root / "_exports" / digest / "source.xlsx"
    if not path.is_file():
        raise StorageError(f"missing native export blob: {path}")
    data = path.read_bytes()
    if sha256_bytes(data) != digest:
        raise StorageError("native export blob hash mismatch")
    return data


@dataclass
class ExportEntry:
    identity: CompanyIdentity
    row: dict
    blob_hash: str
    retrieved_at: datetime
    retrieved_at_known: bool = True

    def download(self, calendar_year_confirmed: bool) -> FilingDownload:
        return FilingDownload(candidate=row_candidate(self.row), files={"source.json": dump_json(self.row).encode()},
            content_types={"source.json": "application/json"}, source_urls={"source.json": EXPORT_URL},
            retrieved_at=self.retrieved_at,
            provenance={"data_product": SOURCE, "export_blob_sha256": self.blob_hash,
                "calendar_year_confirmed": calendar_year_confirmed,
                "retrieved_at_basis": "source_capture" if self.retrieved_at_known else "import_time_original_capture_unknown"})


class KapExportClient:
    """Live comparison-export source.

    `plan()` receives the companies a sync run is about to process; `list_financial_filings`
    then downloads the whole 25-company batch that contains the requested ticker (one POST,
    like the original browser scripts) and serves every member of that batch from memory.
    Without a plan, each company costs one POST.
    """

    market_source = SOURCE
    publishes_all_years = True  # every requested annual row is a record, not just the latest
    BATCH_SIZE = 25

    def __init__(self, fetcher: PacedClient, settings, *, clock=utcnow):
        self.fetcher, self.settings, self.clock = fetcher, settings, clock
        self.registry = CompanyRegistry(settings.kap_company_registry, settings.kap_alias_file)
        self.raw = RawStore(settings.data_dir)
        self._entries: dict[str, ExportEntry] = {}
        self._candidates: dict[str, list[FilingCandidate]] = {}
        self._batch_of: dict[str, tuple[CompanyIdentity, ...]] = {}
        self._failed: dict[str, str] = {}
        # Export rows that could not be matched or decoded. Surfaced in the run summary so a
        # renamed issuer shows up as a work item instead of disappearing from coverage.
        self.rejected_rows: list[dict[str, str | list[str]]] = []
        # Issuers excluded before any request: the GENERAL-sector export has no reviewed
        # mapping for them. Also surfaced in the run summary.
        self.excluded_tickers: list[dict[str, str]] = []
        # Rows matched through loose_key rather than the exact title; surfaced so an operator
        # can promote the observed spelling to a verified alias.
        self.normalized_matches: list[dict[str, str]] = []

    def resolve_company(self, ticker: str) -> CompanyIdentity:
        return self.registry.resolve(ticker)

    def plan(self, identities: list[CompanyIdentity]) -> None:
        """Group the run's companies into export batches; unsupported issuers are left out."""
        # A client may be reused for another run or year window. Refresh must issue a new
        # request, and prior failures/rejections must not leak into the next summary.
        self._entries.clear()
        self._candidates.clear()
        self._batch_of.clear()
        self._failed.clear()
        self.rejected_rows.clear()
        self.excluded_tickers.clear()
        self.normalized_matches.clear()
        supported = []
        for identity in identities:
            if self.settings.calendar_year_confirmed(identity.ticker):
                supported.append(identity)
            else:
                self.excluded_tickers.append(
                    {"ticker": identity.ticker, "reason": "listed in KAP_NON_CALENDAR_YEAR_TICKERS; export omits exact period dates"}
                )
        for start in range(0, len(supported), self.BATCH_SIZE):
            batch = tuple(supported[start:start + self.BATCH_SIZE])
            for identity in batch:
                self._batch_of[identity.ticker] = batch

    def list_financial_filings(self, identity: CompanyIdentity) -> list[FilingCandidate]:
        ticker = identity.ticker
        if not self.settings.calendar_year_confirmed(ticker):
            raise SourceError(f"{ticker} is listed in KAP_NON_CALENDAR_YEAR_TICKERS; the export omits exact period dates, so it is not published")
        if ticker in self._failed:
            raise SourceError(f"batch request containing {ticker} failed: {self._failed[ticker]}")
        if ticker not in self._candidates:
            self._fetch_batch(self._batch_of.get(ticker, (identity,)))
        return list(self._candidates[ticker])

    def _fetch_batch(self, batch: tuple[CompanyIdentity, ...]) -> None:
        tickers = [i.ticker for i in batch]
        try:
            result = self.fetcher.post_json(EXPORT_URL, export_payload(list(batch), self.settings.kap_years))
            # Archive even an invalid response as evidence; read_export will refuse to publish it.
            digest = archive_export(self.raw, result.content)
            rows = read_export(result.content)
            found: dict[str, list[FilingCandidate]] = {t: [] for t in tickers}
            # Quarantine EVERY occurrence of a conflicting notification, including the
            # first. Keeping the first row would publish an arbitrary financial value.
            first_rows: dict[str, dict] = {}
            conflicts: set[str] = set()
            for row in rows:
                nid = str(row["Notification ID"])
                previous = first_rows.setdefault(nid, row)
                if previous != row or (nid in self._entries and self._entries[nid].row != row):
                    conflicts.add(nid)
            for row in rows:
                try:
                    if str(row["Notification ID"]) in conflicts:
                        raise SourceError("conflicting duplicate notification rows; all occurrences in this batch rejected")
                    matched_ticker, basis = self.registry.match_with_basis(str(row["Company"]), tickers)
                    mapped = self.registry.resolve(matched_ticker)
                    if basis == "normalized":
                        self.normalized_matches.append({"company": str(row["Company"])[:256], "ticker": matched_ticker,
                                                        "registry_title": mapped.company_name})

                    candidate = row_candidate(row)

                    if candidate.fiscal_year not in self.settings.kap_years:
                        raise SourceError("export returned an unrequested year")

                    if candidate.notification_id in self._entries:
                        if self._entries[candidate.notification_id].row != row:
                            raise SourceError("conflicting duplicate notification rows")
                        continue

                    self._entries[candidate.notification_id] = ExportEntry(
                        mapped,
                        row,
                        digest,
                        self.clock()
                    )

                    found[mapped.ticker].append(candidate)

                except (UnknownTicker, SourceError, ValueError, TypeError, KeyError) as exc:
                    # One unusable row must not discard the other 24 companies in the batch,
                    # but a silently dropped row is invisible coverage loss: record it too.
                    self.rejected_rows.append(
                        {
                            "company": str(row.get("Company"))[:256],
                            "notification_id": str(row.get("Notification ID"))[:32],
                            "year": str(row.get("Year"))[:8],
                            "reason": f"{type(exc).__name__}: {exc}"[:500],
                            "batch_tickers": list(tickers),
                        }
                    )
                    log.warning("Rejected export row company=%s error=%s", row.get("Company"), exc)
                    continue

        except Exception as exc:
            for t in tickers:
                self._failed[t] = str(exc)
            raise
        self._candidates.update(found)

    def fetch_filing(self, identity: CompanyIdentity, candidate: FilingCandidate) -> FilingDownload:
        entry = self._entries[candidate.notification_id]
        if entry.identity.source_company_id != identity.source_company_id:
            raise SourceError("export identity mismatch")
        return entry.download(self.settings.calendar_year_confirmed(identity.ticker))


def load_manifest(path: Path, registry: CompanyRegistry, raw: RawStore, *, clock=utcnow) -> tuple[list[ExportEntry], list[dict]]:
    """Import originals unchanged; never trust batch index as a company or year key."""
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise SourceError("manifest exceeds 200 MiB; split into smaller files")
    document = json.loads(path.read_text("utf-8-sig"))
    batches = document if isinstance(document, list) else document.get("batches") if isinstance(document, dict) else None
    if not isinstance(batches, list) or len(batches) > 1000:
        raise SourceError("expected a legacy manifest array or a v6 object with batches")
    entries, errors, seen = [], [], {}
    for index, batch in enumerate(batches):
        try:
            if not isinstance(batch, dict) or not isinstance(batch.get("base64"), str):
                raise SourceError("batch has no base64 payload")
            if len(batch["base64"]) > MAX_WORKBOOK_BYTES * 4 // 3 + 4:
                raise SourceError("encoded workbook exceeds limit")
            data = base64.b64decode(batch["base64"], validate=True)
            digest = archive_export(raw, data)
            if batch.get("sha256") and batch["sha256"] != digest:
                raise SourceError("batch SHA-256 mismatch")
            tickers = batch.get("tickers")
            if not isinstance(tickers, list) or not tickers or any(not isinstance(t, str) for t in tickers):
                raise SourceError("batch tickers must be a nonempty list")
            retrieved = parse_source_timestamp(batch["fetchedAt"]) if batch.get("fetchedAt") else clock()
            for row in read_export(data):
                try:
                    identity = registry.match(str(row["Company"]), tickers)
                    candidate = row_candidate(row)
                    key = (identity.ticker, candidate.notification_id)
                    row_hash = sha256_bytes(dump_json(row).encode())
                    if (key, row_hash) in seen:
                        continue
                    seen[(key, row_hash)] = True
                    entries.append(ExportEntry(identity, row, digest, retrieved, bool(batch.get("fetchedAt"))))
                except (ValueError, SourceError) as exc:
                    errors.append({"batch": index, "company": row.get("Company"), "error": str(exc)})
        except (ValueError, SourceError, binascii.Error, TypeError) as exc:
            errors.append({"batch": index, "error": str(exc)})
    return entries, errors
