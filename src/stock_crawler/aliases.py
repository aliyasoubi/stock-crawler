"""verify-aliases: prove historical company titles with single-company exports.

The comparison export identifies companies only by the title printed on each notification,
which is the title the issuer had *at that time*. The request, however, is made by MKK member
ID. A one-company request therefore returns rows that can only belong to that ID, whatever
title they carry: that is direct evidence from the source, not an inference from batch order
or from a partial name. Each proven title is written to `config/kap_aliases.json` with the
request, workbook hash and notification IDs that prove it, and the company is made due again
so the next `sync` publishes the rows it was rejecting.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .fetch import BudgetExhausted, FetchError, PacedClient
from .kap import SourceError, UnknownTicker
from .kap_export import EXPORT_URL, SOURCE, CompanyRegistry, archive_export, export_payload, name_key, read_export, row_candidate
from .storage import RawStore, StateStore, dump_json, utcnow, write_atomic

log = logging.getLogger(__name__)


@dataclass
class VerificationOutcome:
    checked: list[dict[str, Any]] = field(default_factory=list)
    new_aliases: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str | None = None
    request_attempts: int = 0


def candidate_tickers_from_summary(summary: dict[str, Any], registry: CompanyRegistry,
                                   alias_document: dict[str, Any] | None = None) -> list[str]:
    """Tickers that could own a rejected title: they shared its batch and have no row for any
    of the years the title was rejected for. Ranked by expected resolutions, so a title with
    two possible owners is tried before one with twelve, and tickers already proven empty for
    these years are skipped."""
    missing: dict[str, set[int]] = defaultdict(set)
    requested_years = {int(y) for y in summary.get("requested_years", [])}
    for entry in summary.get("companies", []):
        if entry.get("status") == "no_filing" and entry.get("fiscal_year") is not None:
            missing[entry["ticker"]].add(int(entry["fiscal_year"]))
    titles: dict[tuple[str, tuple[str, ...]], set[int]] = defaultdict(set)
    for row in summary.get("rejected_rows", []):
        if not str(row.get("reason", "")).startswith("UnknownTicker"):
            continue
        batch = tuple(row.get("batch_tickers", []))
        try:
            registry.match(str(row["company"]), list(batch))
            continue  # already resolvable with the current registry/aliases: no request needed
        except UnknownTicker:
            pass
        try:
            titles[(str(row["company"]), batch)].add(int(row["year"]))
        except (KeyError, TypeError, ValueError):
            continue
    proven_empty = set()
    for entry in (alias_document or {}).get("verified_empty", []):
        if requested_years and requested_years <= {int(y) for y in entry.get("years", [])}:
            proven_empty.add(entry.get("ticker"))
    score: dict[str, float] = defaultdict(float)
    for (title, batch), years in titles.items():
        owners = [t for t in batch if t in registry.by_ticker and t not in proven_empty and years <= missing.get(t, set())]
        for ticker in owners:
            score[ticker] += 1.0 / len(owners)
    return sorted(score, key=lambda t: (-score[t], t))


def load_alias_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"aliases": [], "verified_empty": []}
    document = json.loads(path.read_text("utf-8-sig"))
    document.setdefault("aliases", [])
    document.setdefault("verified_empty", [])
    return document


def verify_aliases(
    tickers: list[str],
    *,
    settings: Settings,
    fetcher: PacedClient,
    registry: CompanyRegistry,
    raw: RawStore,
    state: StateStore,
    alias_path: Path,
    max_requests: int | None = None,
    clock=utcnow,
) -> VerificationOutcome:
    outcome = VerificationOutcome()
    document = load_alias_file(alias_path)
    known = {(a["ticker"], name_key(a["title"])) for a in document["aliases"]}
    years = sorted(settings.kap_years)
    budget = fetcher.remaining_budget if max_requests is None else min(max_requests, fetcher.remaining_budget)
    for ticker in tickers:
        if len(outcome.checked) >= budget:
            outcome.stopped_reason = f"verification request cap of {budget} reached"
            break
        identity = registry.resolve(ticker)
        record: dict[str, Any] = {"ticker": ticker, "years": years}
        try:
            result = fetcher.post_json(EXPORT_URL, export_payload([identity], years))
            digest = archive_export(raw, result.content)
            rows = read_export(result.content)
        except BudgetExhausted as exc:
            outcome.stopped_reason = str(exc)
            break
        except (FetchError, SourceError) as exc:
            record.update(status="error", error=str(exc))
            outcome.checked.append(record)
            log.warning("%s: verification request failed: %s", ticker, exc)
            continue
        record["workbook_sha256"] = digest
        titles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            candidate = row_candidate(row)
            titles[str(row["Company"])].append({"notification_id": candidate.notification_id, "fiscal_year": candidate.fiscal_year})
        if not rows:
            record["status"] = "no_rows"
            document["verified_empty"] = [e for e in document["verified_empty"] if e.get("ticker") != ticker]
            document["verified_empty"].append({"ticker": ticker, "years": years, "verified_at": clock().isoformat(), "workbook_sha256": digest})
            outcome.checked.append(record)
            continue
        record["status"] = "rows"
        record["titles"] = sorted(titles)
        learned = 0
        for title, notifications in titles.items():
            key = name_key(title)
            owner = registry.names.get(key)
            if owner == ticker or (ticker, key) in known:
                continue  # current title or an alias we already hold
            if owner is not None:
                # The registry seed says this title belongs to another ticker, yet the source
                # returned it for this ID. Do not guess: the seed needs a human fix.
                outcome.conflicts.append({"ticker": ticker, "title": title, "registry_ticker": owner, "notifications": notifications})
                continue
            alias = {
                "ticker": ticker,
                "title": title,
                "verified_at": clock().isoformat(),
                "evidence": {
                    "method": "single_company_export",
                    "request": {"mkkMemberIdList": [identity.source_company_id], "yearList": [str(y) for y in years]},
                    "workbook_sha256": digest,
                    "notifications": notifications,
                },
            }
            document["aliases"].append(alias)
            known.add((ticker, key))
            outcome.new_aliases.append(alias)
            learned += 1
        record["new_aliases"] = learned
        if learned:
            state.clear_coverage(f"{SOURCE}/{identity.source_company_id}")
        outcome.checked.append(record)
    document["generated_by"] = "stock-crawler verify-aliases"
    document["updated_at"] = clock().isoformat()
    write_atomic(alias_path, dump_json(document).encode("utf-8"))
    outcome.request_attempts = fetcher.attempts
    return outcome
