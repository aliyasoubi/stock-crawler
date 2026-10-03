"""Pluggable data sources. The pipeline looks up ``SeriesSpec.source`` in a mapping of these."""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Protocol

from .fields import SeriesSpec
from .evds_client import EvdsClient

log = logging.getLogger(__name__)


class Source(Protocol):
    def validate(self, specs: Iterable[SeriesSpec]) -> None: ...

    def fetch(self, spec: SeriesSpec, start: date, end: date) -> dict[date, Decimal]: ...


class EvdsSource:
    def __init__(self, client: EvdsClient) -> None:
        self.client = client

    def validate(self, specs: Iterable[SeriesSpec]) -> None:
        by_group: dict[str, list[str]] = {}
        for spec in specs:
            if spec.datagroup:
                by_group.setdefault(spec.datagroup, []).append(spec.code)
        for datagroup, codes in by_group.items():
            self.client.assert_series_exist(datagroup, codes)

    def fetch(self, spec: SeriesSpec, start: date, end: date) -> dict[date, Decimal]:
        return self.client.fetch_series(spec.code, spec.native_freq, start, end, spec.agg)


class CsvSource:
    """Daily observations from a CSV export, e.g. Türkiye 5Y CDS from Bloomberg/LSEG/S&P.

    Expected columns: a date column (``date``) and a value column (``value`` or
    the series code). Dates may be ISO (2025-01-31) or dd.mm.yyyy / dd/mm/yyyy.
    """

    DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y")

    def __init__(self, path: str | Path, date_column: str = "date", value_column: str = "value") -> None:
        self.path = Path(path)
        self.date_column = date_column
        self.value_column = value_column

    def validate(self, specs: Iterable[SeriesSpec]) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(f"CSV source not found: {self.path}")

    def _parse_date(self, text: str) -> date:
        for fmt in self.DATE_FORMATS:
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                pass
        raise ValueError(f"Unrecognised date {text!r} in {self.path}")

    def fetch(self, spec: SeriesSpec, start: date, end: date) -> dict[date, Decimal]:
        out: dict[date, Decimal] = {}
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            value_column = spec.code if spec.code in (reader.fieldnames or []) else self.value_column
            for line_no, row in enumerate(reader, start=2):
                raw = (row.get(value_column) or "").strip()
                if not raw:
                    continue
                d = self._parse_date(row[self.date_column])
                if start <= d <= end:
                    try:
                        out[d] = Decimal(raw.replace(",", ""))
                    except InvalidOperation as exc:
                        raise ValueError(f"{self.path}:{line_no}: bad value {raw!r}") from exc
        return out


class EmptySource:
    """Placeholder for a field with no configured provider; yields no data."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def validate(self, specs: Iterable[SeriesSpec]) -> None:
        for spec in specs:
            log.warning("%s: no source configured (%s); column will be NULL", spec.code, self.reason)

    def fetch(self, spec: SeriesSpec, start: date, end: date) -> dict[date, Decimal]:
        return {}
