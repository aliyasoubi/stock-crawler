from datetime import datetime, timedelta, timezone

import httpx
import pytest

from stock_crawler.config import Settings
from stock_crawler.fetch import AccessBlocked, BudgetExhausted, HostCoolingDown, HostThrottled, PacedClient, SourceHTTPError, looks_like_challenge, parse_retry_after
from stock_crawler.storage import StateStore

from .conftest import FakeClock

HOST = "www.kap.org.tr"
URL = f"https://{HOST}/tr/bildirim/1"


class Script:
    """Feeds scripted responses to httpx.MockTransport and records the requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, headers, body = item
        return httpx.Response(status, headers=headers, content=body, request=request)


def make_client(tmp_path, responses, clock, sleeps, **overrides):
    params = dict(request_delay_min_seconds=5, request_delay_max_seconds=5, max_retries=2, max_requests_per_run=6, retry_backoff_seconds=(10, 30))
    params.update(overrides)
    settings = Settings(_env_file=None, fixture_source_dir=tmp_path, **params)
    script = Script(responses)
    state = StateStore(tmp_path, clock=clock)

    def sleeper(seconds):
        sleeps.append(round(seconds, 3))
        clock.advance(seconds=seconds)

    client = PacedClient(httpx.Client(transport=httpx.MockTransport(script)), settings, state, allowed_hosts={HOST}, clock=clock, sleeper=sleeper)
    return client, script, state


def test_pacing_between_requests_to_same_host(tmp_path):
    clock, sleeps = FakeClock(), []
    client, script, _ = make_client(tmp_path, [(200, {"content-type": "text/html"}, b"a"), (200, {"content-type": "text/html"}, b"b")], clock, sleeps)
    client.get(URL)
    client.get(URL)
    assert sleeps == [5.0] and client.attempts == 2
    assert script.requests[0].headers["user-agent"].startswith("StockFundamentalsMVP")


def test_budget_counts_retries_and_stops_cleanly(tmp_path):
    clock, sleeps = FakeClock(), []
    responses = [(503, {}, b"")] * 3 + [(200, {}, b"ok")] * 10
    client, _, _ = make_client(tmp_path, responses, clock, sleeps, max_requests_per_run=3)
    with pytest.raises(SourceHTTPError):
        client.get(URL)
    assert client.attempts == 3 and sleeps.count(10.0) == 1 and sleeps.count(30.0) == 1
    with pytest.raises(BudgetExhausted):
        client.get(URL)


def test_transport_errors_retry_with_increasing_delays(tmp_path):
    clock, sleeps = FakeClock(), []
    client, _, _ = make_client(tmp_path, [httpx.ReadTimeout("t"), httpx.ConnectError("c"), (200, {}, b"ok")], clock, sleeps)
    assert client.get(URL).content == b"ok"
    assert 10.0 in sleeps and 30.0 in sleeps


def test_429_honours_retry_after_and_persists_cooldown(tmp_path):
    clock, sleeps = FakeClock(), []
    client, _, state = make_client(tmp_path, [(429, {"retry-after": "7"}, b""), (429, {}, b""), (429, {}, b"")], clock, sleeps)
    with pytest.raises(HostThrottled):
        client.get(URL)
    assert 7.0 in sleeps and 60.0 in sleeps
    until, reason = state.host_cooldown(HOST)
    assert until - clock() == timedelta(seconds=3600) and "429" in reason
    fresh_client, _, _ = make_client(tmp_path, [(200, {}, b"ok")], clock, sleeps)
    with pytest.raises(HostCoolingDown):
        fresh_client.get(URL)


def test_retry_after_http_date_and_longer_source_delay_wins(tmp_path):
    now = datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Mon, 01 Sep 2025 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("garbage", now=now) is None
    clock, sleeps = FakeClock(now), []
    client, _, state = make_client(tmp_path, [(429, {"retry-after": "7200"}, b"")] * 3, clock, sleeps, max_retries=0)
    with pytest.raises(HostThrottled):
        client.get(URL)
    until, _ = state.host_cooldown(HOST)
    assert until - clock() == timedelta(seconds=7200)


@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_blocks_host(tmp_path, status):
    clock, sleeps = FakeClock(), []
    client, script, state = make_client(tmp_path, [(status, {}, b"denied"), (200, {}, b"x")], clock, sleeps)
    with pytest.raises(AccessBlocked):
        client.get(URL)
    assert state.host_cooldown(HOST)[0] is not None
    with pytest.raises(HostCoolingDown):
        client.get(URL)
    assert len(script.requests) == 1


def test_challenge_page_with_200_blocks_host(tmp_path):
    body = b"<html><head><title>Just a moment...</title></head><body><div id='cf-challenge'></div></body></html>"
    assert looks_like_challenge("text/html", body)
    assert not looks_like_challenge("application/json", body)
    clock, sleeps = FakeClock(), []
    client, _, state = make_client(tmp_path, [(200, {"content-type": "text/html"}, body)], clock, sleeps)
    with pytest.raises(AccessBlocked):
        client.get(URL)
    assert "challenge" in state.host_cooldown(HOST)[1]


def test_conditional_request_and_304(tmp_path):
    clock, sleeps = FakeClock(), []
    client, script, _ = make_client(tmp_path, [(304, {"etag": '"abc"'}, b"")], clock, sleeps)
    result = client.get(URL, validators={"etag": '"abc"', "last_modified": "Mon, 01 Sep 2025 00:00:00 GMT"})
    assert result.not_modified and result.content == b""
    assert script.requests[0].headers["if-none-match"] == '"abc"'
    assert script.requests[0].headers["if-modified-since"].startswith("Mon")
    assert client.attempts == 1


def test_redirects_count_and_must_stay_on_verified_hosts(tmp_path):
    clock, sleeps = FakeClock(), []
    client, script, _ = make_client(tmp_path, [(302, {"location": "/tr/other"}, b""), (200, {"etag": "x"}, b"done")], clock, sleeps)
    result = client.get(URL)
    assert result.content == b"done" and client.attempts == 2 and result.validators == {"etag": "x"}
    client2, _, _ = make_client(tmp_path, [(302, {"location": "https://evil.example/x"}, b"")], clock, sleeps)
    with pytest.raises(SourceHTTPError, match="unverified host"):
        client2.get(URL)
    with pytest.raises(SourceHTTPError):
        client2.get("http://www.kap.org.tr/plain")


def test_client_errors_are_not_retried(tmp_path):
    clock, sleeps = FakeClock(), []
    client, script, _ = make_client(tmp_path, [(404, {}, b""), (200, {}, b"")], clock, sleeps)
    with pytest.raises(SourceHTTPError):
        client.get(URL)
    assert len(script.requests) == 1
