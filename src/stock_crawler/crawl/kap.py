"""Source access for KAP: identity resolution, filing discovery, selection, and download.

Source implementations share one small interface (`SourceClient`):

* `KapExportClient` in kap_export.py is the live route: the public comparison-export
  endpoint (one XLSX per POST, up to 25 companies and 5 years).
* `FixtureSourceClient` replays captured filings from a local directory with zero network
  requests. It drives the full sync pipeline offline and in tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from .fetch import PacedClient
from ..core.models import MARKET_SOURCE_KAP, CompanyIdentity, ConsolidationScope, FilingCandidate, FilingDownload
from ..core.storage import Clock, utcnow

KAP_HOSTS = {"www.kap.org.tr", "kap.org.tr"}


class SourceError(RuntimeError):
    """Base class for source-side problems that are not HTTP transport failures."""


class UnknownTicker(SourceError):
    """The ticker could not be resolved to exactly one stable source company id."""


class SourceClient(Protocol):
    market_source: str

    def resolve_company(self, ticker: str) -> CompanyIdentity: ...

    def list_financial_filings(self, identity: CompanyIdentity) -> list[FilingCandidate]: ...

    def fetch_filing(self, identity: CompanyIdentity, candidate: FilingCandidate) -> FilingDownload: ...


# -- selection rule (README section 5) ------------------------------------------------------


@dataclass(frozen=True)
class SelectionResult:
    selected: FilingCandidate | None
    considered: int
    annual: int
    withdrawn: tuple[FilingCandidate, ...] = field(default_factory=tuple)
    reason: str | None = None


def _period_key(candidate: FilingCandidate) -> tuple[date, int]:
    end = candidate.period_end_date or date(candidate.fiscal_year, 12, 31)
    return end, candidate.fiscal_year


def _scope_rank(scope: ConsolidationScope | None) -> int:
    return {ConsolidationScope.CONSOLIDATED: 0, ConsolidationScope.UNCONSOLIDATED: 1}.get(scope, 2)


def _notification_number(notification_id: str) -> int:
    try:
        return int(notification_id)
    except ValueError:
        return -1


def select_latest_annual(candidates: list[FilingCandidate]) -> SelectionResult:
    """Latest annual period, consolidated preferred within it, latest valid publication last.

    Withdrawn notifications are excluded from selection and returned separately so the
    caller can flag stored versions. Quarterly notifications never displace an annual one.
    """
    withdrawn = tuple(c for c in candidates if c.is_withdrawn)
    annual = [c for c in candidates if c.is_annual and not c.is_withdrawn]
    if not annual:
        return SelectionResult(None, len(candidates), 0, withdrawn, "no annual financial statement notification found")
    latest_period = max(_period_key(c) for c in annual)
    in_period = [c for c in annual if _period_key(c) == latest_period]
    best_scope = min(_scope_rank(c.consolidation_scope) for c in in_period)
    if best_scope == 2:
        return SelectionResult(None, len(candidates), len(annual), withdrawn, "consolidation scope unknown for latest annual period")
    in_scope = [c for c in in_period if _scope_rank(c.consolidation_scope) == best_scope]
    in_scope.sort(key=lambda c: (c.published_at, _notification_number(c.notification_id), c.notification_id), reverse=True)
    return SelectionResult(in_scope[0], len(candidates), len(annual), withdrawn, None)


# -- fixture-backed client -------------------------------------------------------------------


class FixtureSourceClient:
    """Reads a directory of captured filings. Layout:

        {root}/companies.json                      list of CompanyIdentity dicts
        {root}/{TICKER}/filings.json               list of FilingCandidate dicts, each with
                                                   "files": {"source.html": "relative/path"}
        {root}/{TICKER}/{notification_id}/...      the referenced files
    """

    market_source = MARKET_SOURCE_KAP

    def __init__(self, root: Path, clock: Clock = utcnow) -> None:
        self.root = root
        self.clock = clock
        if not (root / "companies.json").is_file():
            raise SourceError(f"fixture source directory has no companies.json: {root}")
        raw = json.loads((root / "companies.json").read_text("utf-8"))
        self._companies = {entry["ticker"].upper(): CompanyIdentity(**entry) for entry in raw}

    def resolve_company(self, ticker: str) -> CompanyIdentity:
        try:
            return self._companies[ticker.upper()]
        except KeyError as exc:
            raise UnknownTicker(f"ticker {ticker} is not present in fixture companies.json") from exc

    def _filings(self, identity: CompanyIdentity) -> list[dict]:
        path = self.root / identity.ticker / "filings.json"
        if not path.is_file():
            return []
        return json.loads(path.read_text("utf-8"))

    def list_financial_filings(self, identity: CompanyIdentity) -> list[FilingCandidate]:
        return [FilingCandidate(**{k: v for k, v in entry.items() if k != "files"}) for entry in self._filings(identity)]

    def fetch_filing(self, identity: CompanyIdentity, candidate: FilingCandidate) -> FilingDownload:
        for entry in self._filings(identity):
            if entry["notification_id"] == candidate.notification_id:
                files: dict[str, bytes] = {}
                content_types: dict[str, str] = {}
                for name, relative in entry.get("files", {}).items():
                    files[name] = (self.root / identity.ticker / relative).read_bytes()
                    content_types[name] = "text/html; charset=utf-8" if name.endswith(".html") else "application/octet-stream"
                if not files:
                    raise SourceError(f"fixture filing {candidate.notification_id} lists no files")
                return FilingDownload(
                    candidate=candidate,
                    files=files,
                    content_types=content_types,
                    source_urls={name: f"fixture://{identity.ticker}/{candidate.notification_id}/{name}" for name in files},
                    retrieved_at=self.clock(),
                )
        raise SourceError(f"fixture filing {candidate.notification_id} not found for {identity.ticker}")


def build_source_client(settings, fetcher_factory, *, clock: Clock = utcnow) -> SourceClient:
    """Choose the client from settings. `fetcher_factory()` builds a PacedClient lazily so the
    fixture mode never opens an HTTP client."""
    if settings.source_mode == "fixture":
        return FixtureSourceClient(settings.fixture_source_dir, clock=clock)
    from .kap_export import KapExportClient
    return KapExportClient(fetcher_factory(), settings, clock=clock)


# -- live smoke checks (explicitly invoked, tiny request counts) ------------------------------


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status: int | None
    outcome: str
    detail: str
    robots_excerpt: str = ""


def probe_source(fetcher: PacedClient, base_url: str = "https://www.kap.org.tr") -> list[ProbeResult]:
    """Two paced GETs (robots.txt, then the site root) to establish reachability and stop
    signals before any crawling is attempted. Never parses financial data."""
    from .fetch import AccessBlocked, FetchError, HostCoolingDown, HostThrottled, SourceHTTPError

    results: list[ProbeResult] = []
    for path in ("/robots.txt", "/"):
        url = base_url.rstrip("/") + path
        try:
            result = fetcher.get(url, accept="text/plain, text/html;q=0.9, */*;q=0.1")
        except AccessBlocked as exc:
            results.append(ProbeResult(url, None, "blocked", str(exc)))
            break
        except (HostCoolingDown, HostThrottled) as exc:
            results.append(ProbeResult(url, None, "throttled", str(exc)))
            break
        except SourceHTTPError as exc:
            results.append(ProbeResult(url, exc.status, "http_error", str(exc)))
            continue
        except FetchError as exc:
            results.append(ProbeResult(url, None, "error", str(exc)))
            break
        excerpt = ""
        if path == "/robots.txt" and result.status == 200:
            excerpt = result.content[:4000].decode("utf-8", errors="replace")
        results.append(ProbeResult(url, result.status, "ok", f"{len(result.content)} bytes, {result.content_type}", excerpt))
    return results


def capture_fixture(
    fetcher: PacedClient,
    *,
    url: str,
    output_dir: Path,
    identity: CompanyIdentity,
    candidate: FilingCandidate,
    filename: str | None = None,
    clock: Clock = utcnow,
) -> Path:
    """Download one filing URL (supplied by the operator, not guessed) into the
    FixtureSourceClient layout so it can be parsed offline and reviewed as a test fixture."""
    result = fetcher.get(url)
    if result.not_modified:
        raise SourceError("server answered 304; pass a URL without stored validators")
    content_type = (result.content_type or "").lower()
    if filename is None:
        extension = "json" if "json" in content_type else "xml" if "xml" in content_type else "html" if "html" in content_type else "bin"
        filename = f"source.{extension}"
    ticker_dir = output_dir / identity.ticker
    filing_dir = ticker_dir / candidate.notification_id
    filing_dir.mkdir(parents=True, exist_ok=True)
    (filing_dir / filename).write_bytes(result.content)

    companies_path = output_dir / "companies.json"
    companies = json.loads(companies_path.read_text("utf-8")) if companies_path.is_file() else []
    if not any(entry.get("ticker") == identity.ticker for entry in companies):
        companies.append(identity.model_dump())
        companies_path.write_text(json.dumps(companies, ensure_ascii=False, indent=2) + "\n", "utf-8")

    filings_path = ticker_dir / "filings.json"
    filings = json.loads(filings_path.read_text("utf-8")) if filings_path.is_file() else []
    filings = [entry for entry in filings if entry.get("notification_id") != candidate.notification_id]
    entry = candidate.model_dump(mode="json")
    entry["source_url"] = entry.get("source_url") or url
    entry["files"] = {filename: f"{candidate.notification_id}/{filename}"}
    entry["captured_at"] = clock().isoformat()
    entry["content_type"] = result.content_type
    filings.append(entry)
    filings_path.write_text(json.dumps(filings, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return filing_dir / filename
