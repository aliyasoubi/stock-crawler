"""Source access for KAP: identity resolution, filing discovery, selection, and download.

Two implementations share one small interface:

* `FixtureSourceClient` replays captured filings from a local directory with zero network
  requests. It drives the full sync pipeline offline and in tests.
* `KapClient` is the live implementation. Its network methods are deliberately unimplemented
  until the access route is established (README section 6): KAP's REST service requires a
  Borsa Istanbul data agreement, MKK authorization, registered IPs and an API key, and the
  public site's terms/robots rules must be checked for the exact host and paths. Inventing
  endpoint URLs here would violate that rule, so the methods raise `SourceAccessNotConfigured`
  with the steps that remain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from .fetch import PacedClient
from .models import MARKET_SOURCE_KAP, CompanyIdentity, ConsolidationScope, FilingCandidate, FilingDownload
from .storage import Clock, utcnow

KAP_HOSTS = {"www.kap.org.tr", "kap.org.tr"}


class SourceError(RuntimeError):
    """Base class for source-side problems that are not HTTP transport failures."""


class UnknownTicker(SourceError):
    """The ticker could not be resolved to exactly one stable source company id."""


class SourceAccessNotConfigured(SourceError):
    """Live retrieval is not wired to a verified access route yet."""


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


# -- live client -----------------------------------------------------------------------------


class KapClient:
    """Live KAP retrieval behind the paced client. See module docstring for why the network
    methods are not implemented yet.

    Implementation checklist once the access route is confirmed:
      1. resolve_company: map ticker -> stable company id from the authorized company list
         endpoint; refuse ambiguous or missing matches (never guess from a similar name).
      2. list_financial_filings: query financial-statement notifications for that company,
         following pagination within the request budget; populate `is_annual`,
         `period_end_date`, `consolidation_scope`, `is_withdrawn`, and `is_correction` from
         source metadata, and convert Istanbul wall-clock timestamps with
         units.parse_source_timestamp.
      3. fetch_filing: download the notification payload in its native format (JSON/XML/HTML)
         plus only the assets the parser needs, passing stored validators for conditional GETs.
    Capture each response as a test fixture under tests/fixtures/kap/ before parsing it.
    """

    market_source = MARKET_SOURCE_KAP

    def __init__(self, fetcher: PacedClient, *, base_url: str = "https://www.kap.org.tr") -> None:
        self.fetcher = fetcher
        self.base_url = base_url.rstrip("/")

    def _not_configured(self, step: str) -> SourceAccessNotConfigured:
        return SourceAccessNotConfigured(
            f"KAP {step} is not wired to a verified access route. Establish REST access or confirm "
            "permitted public-web endpoints and robots rules, capture a fixture, then implement "
            f"KapClient.{step} (README section 6). Use SOURCE_MODE=fixture for offline runs."
        )

    def resolve_company(self, ticker: str) -> CompanyIdentity:
        raise self._not_configured("resolve_company")

    def list_financial_filings(self, identity: CompanyIdentity) -> list[FilingCandidate]:
        raise self._not_configured("list_financial_filings")

    def fetch_filing(self, identity: CompanyIdentity, candidate: FilingCandidate) -> FilingDownload:
        raise self._not_configured("fetch_filing")


def build_source_client(settings, fetcher_factory, *, clock: Clock = utcnow) -> SourceClient:
    """Choose the client from settings. `fetcher_factory()` builds a PacedClient lazily so the
    fixture mode never opens an HTTP client."""
    if settings.source_mode == "fixture":
        return FixtureSourceClient(settings.fixture_source_dir, clock=clock)
    return KapClient(fetcher_factory())
