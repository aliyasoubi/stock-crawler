from datetime import date, datetime, timezone
import json

import httpx

from stock_crawler.config import Settings
from stock_crawler.fetch import PacedClient
from stock_crawler.kap import FixtureSourceClient, capture_fixture, probe_source
from stock_crawler.models import CompanyIdentity, ConsolidationScope, FilingCandidate
from stock_crawler.storage import StateStore

from .conftest import FakeClock, THYAO_HTML


def make_fetcher(tmp_path, handler, clock):
    settings = Settings(_env_file=None, fixture_source_dir=tmp_path, request_delay_min_seconds=0, request_delay_max_seconds=0)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PacedClient(client, settings, StateStore(tmp_path, clock=clock), allowed_hosts={"www.kap.org.tr"}, clock=clock, sleeper=lambda s: None)


def test_probe_reports_robots_and_root(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nDisallow: /tr/api/\n")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><title>KAP</title></html>")

    fetcher = make_fetcher(tmp_path, handler, FakeClock())
    results = probe_source(fetcher)
    assert [r.outcome for r in results] == ["ok", "ok"] and "Disallow: /tr/api/" in results[0].robots_excerpt
    assert fetcher.attempts == 2


def test_probe_stops_on_challenge(tmp_path):
    body = b"<html><head><title>Just a moment...</title></head></html>"
    fetcher = make_fetcher(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=body), FakeClock())
    results = probe_source(fetcher)
    assert len(results) == 1 and results[0].outcome == "blocked"


def test_capture_fixture_writes_layout_that_fixture_client_can_replay(tmp_path):
    html = THYAO_HTML.read_bytes()
    fetcher = make_fetcher(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=html), FakeClock())
    identity = CompanyIdentity(source_company_id="real-id", ticker="THYAO", company_name="THY")
    candidate = FilingCandidate(
        notification_id="1400001", published_at=datetime(2025, 3, 5, 15, 45, tzinfo=timezone.utc), fiscal_year=2024,
        period_end_date=date(2024, 12, 31), is_annual=True, consolidation_scope=ConsolidationScope.CONSOLIDATED, statement_type="general",
    )
    out = tmp_path / "captures"
    path = capture_fixture(fetcher, url="https://www.kap.org.tr/tr/Bildirim/1400001", output_dir=out, identity=identity, candidate=candidate)
    assert path == out / "THYAO" / "1400001" / "source.html" and path.read_bytes() == html
    filings = json.loads((out / "THYAO" / "filings.json").read_text())
    assert filings[0]["files"] == {"source.html": "1400001/source.html"} and filings[0]["consolidation_scope"] == "consolidated"
    source = FixtureSourceClient(out)
    listed = source.list_financial_filings(source.resolve_company("thyao"))
    assert listed[0].notification_id == "1400001"
    download = source.fetch_filing(identity, listed[0])
    assert download.files["source.html"] == html
    # re-capturing the same notification replaces its entry instead of duplicating it
    capture_fixture(fetcher, url="https://www.kap.org.tr/tr/Bildirim/1400001", output_dir=out, identity=identity, candidate=candidate)
    assert len(json.loads((out / "THYAO" / "filings.json").read_text())) == 1
