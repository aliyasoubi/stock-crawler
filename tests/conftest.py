from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_crawler.core.config import Settings
from stock_crawler.crawl.kap import FixtureSourceClient
from stock_crawler.crawl.pipeline import Pipeline
from stock_crawler.core.storage import RawStore, StateStore

from .fakes import InMemoryRepository

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "kap" / "source"
THYAO_HTML = FIXTURE_ROOT / "THYAO" / "1400001" / "source.html"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        company_file=tmp_path / "companies.txt",
        data_dir=tmp_path / "data",
        source_mode="fixture",
        fixture_source_dir=FIXTURE_ROOT,
        request_delay_min_seconds=0,
        request_delay_max_seconds=0,
        mssql_password="x",
    )


@pytest.fixture
def fixture_copy(tmp_path: Path) -> Path:
    """A writable copy of the fixture source directory for tests that mutate filings."""
    target = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT, target)
    return target


@pytest.fixture
def thyao_html() -> str:
    return THYAO_HTML.read_text("utf-8")


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def make_pipeline(settings: Settings, repo: InMemoryRepository, clock: FakeClock):
    def factory(source_dir: Path | None = None, parser_version: str | None = None) -> Pipeline:
        source = FixtureSourceClient(source_dir or FIXTURE_ROOT, clock=clock)
        kwargs = {"parser_version": parser_version} if parser_version else {}
        return Pipeline(settings, repo, RawStore(settings.data_dir), StateStore(settings.data_dir, clock=clock), source, clock=clock, **kwargs)

    return factory
