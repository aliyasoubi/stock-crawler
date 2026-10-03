"""Assemble ``MacroSovereign`` rows from the configured fields and sources."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping

from .fields import FIELDS, FieldSpec, PeriodType, SeriesSpec
from .periods import aggregate, period_start
from .sources import Source

log = logging.getLogger(__name__)

KEY_COLUMNS = ("MarketId", "AsOfDate", "PeriodType", "PublishDate")


@dataclass
class MacroRow:
    MarketId: int
    AsOfDate: date
    PeriodType: str
    PublishDate: date
    values: dict[str, Decimal | None] = field(default_factory=dict)

    def as_dict(self, columns: Iterable[str]) -> dict[str, object]:
        row: dict[str, object] = {k: getattr(self, k) for k in KEY_COLUMNS}
        row.update({c: self.values.get(c) for c in columns})
        return row


def check_fits_column(value: Decimal, spec: FieldSpec) -> Decimal:
    """Refuse values too large for the column's decimal(p, s). The value is not rounded."""
    if abs(value) >= Decimal(10) ** (spec.precision - spec.scale):
        raise OverflowError(f"{spec.column}={value} does not fit decimal({spec.precision},{spec.scale})")
    return value


def in_range(value: Decimal, spec: FieldSpec) -> bool:
    lo, hi = spec.valid_range
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def build_rows(
    sources: Mapping[str, Source],
    market_id: int,
    period_types: Iterable[PeriodType],
    start: date,
    end: date,
    publish_date: date,
    fields: tuple[FieldSpec, ...] = FIELDS,
) -> list[MacroRow]:
    period_types = list(period_types)
    all_specs = [s for f in fields for s in f.series]

    # Fail fast on configuration/catalogue problems before downloading anything.
    for name in {s.source for s in all_specs}:
        if name not in sources:
            raise KeyError(f"No source registered for {name!r}")
        sources[name].validate([s for s in all_specs if s.source == name])

    native: dict[SeriesSpec, dict[date, Decimal]] = {}
    for spec in all_specs:
        if spec not in native:
            native[spec] = sources[spec.source].fetch(spec, start, end)
            log.info("Fetched %-26s %5d obs", spec.code, len(native[spec]))

    table: dict[tuple[PeriodType, date], dict[str, Decimal | None]] = {}
    for fspec in fields:
        for ptype in period_types:
            parts = [
                aggregate(native[s], s.native_freq, ptype, s.agg, end)
                for s in fspec.series
            ]
            common = set.intersection(*(set(p) for p in parts)) if parts else set()
            for as_of_date in sorted(common):
                if period_start(as_of_date, ptype) < start:
                    continue
                value = check_fits_column(sum((p[as_of_date] for p in parts), Decimal(0)), fspec)
                if not in_range(value, fspec):
                    log.error("%s %s %s = %s outside %s; dropped", fspec.column, ptype.value,
                              as_of_date, value, fspec.valid_range)
                    continue
                table.setdefault((ptype, as_of_date), {})[fspec.column] = value

    order = {p: i for i, p in enumerate(PeriodType)}
    return [
        MacroRow(market_id, as_of_date, ptype.value, publish_date, values)
        for (ptype, as_of_date), values in sorted(table.items(), key=lambda kv: (order[kv[0][0]], kv[0][1]))
    ]


def coverage_report(rows: list[MacroRow], fields: tuple[FieldSpec, ...] = FIELDS) -> str:
    """Plain-text table of non-null counts per period type and column."""
    columns = [f.column for f in fields]
    header = f"{'PeriodType':<10} {'rows':>5} " + " ".join(f"{c:>12}" for c in columns)
    lines = [header, "-" * len(header)]
    for ptype in PeriodType:
        subset = [r for r in rows if r.PeriodType == ptype.value]
        if not subset:
            continue
        counts = " ".join(f"{sum(r.values.get(c) is not None for r in subset):>12}" for c in columns)
        span = f"  {subset[0].AsOfDate}..{subset[-1].AsOfDate}"
        lines.append(f"{ptype.value:<10} {len(subset):>5} {counts}{span}")
    return "\n".join(lines)
