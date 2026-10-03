"""Period parsing and completeness-checked aggregation.

EVDS can aggregate server-side, but it happily sums a partial year (e.g. an
"annual" 2026 GDP made of two quarters). Aggregating locally lets us refuse
incomplete periods instead of loading misleading numbers.
"""
from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping

from .fields import Agg, Freq, PeriodType

log = logging.getLogger(__name__)

# For daily data, the last observation must fall within this many days of the
# period end; covers weekends and public holidays.
STALE_DAYS = 7


def parse_evds_date(value: str | int, freq: Freq) -> date:
    """Convert an EVDS ``Tarih`` value to the start date of its native period."""
    text = str(value).strip()
    if freq in (Freq.DAILY, Freq.BUSINESS_DAY):
        return datetime.strptime(text, "%d-%m-%Y").date()
    if freq is Freq.MONTHLY:
        year, month = text.split("-")
        return date(int(year), int(month), 1)
    if freq is Freq.QUARTERLY:
        year, quarter = text.split("-Q")
        return date(int(year), 3 * int(quarter) - 2, 1)
    if freq is Freq.ANNUAL:
        return date(int(text), 1, 1)
    raise ValueError(f"Unsupported frequency {freq!r}")


def add_months(d: date, months: int) -> date:
    idx = d.year * 12 + d.month - 1 + months
    return date(idx // 12, idx % 12 + 1, 1)


def period_start(d: date, ptype: PeriodType) -> date:
    first_month = (d.month - 1) // ptype.months * ptype.months + 1
    return date(d.year, first_month, 1)


def period_end(start: date, ptype: PeriodType) -> date:
    last = add_months(start, ptype.months - 1)
    return date(last.year, last.month, calendar.monthrange(last.year, last.month)[1])


def aggregate(
    obs: Mapping[date, Decimal],
    native: Freq,
    target: PeriodType,
    agg: Agg,
    as_of: date,
) -> dict[date, Decimal]:
    """Collapse native observations into ``target`` periods keyed by period end date.

    Periods that have not ended by ``as_of``, or that lack the observations the
    aggregation needs, are dropped rather than estimated.
    """
    native_months = native.months
    if native_months is not None and native_months > target.months:
        return {}  # cannot disaggregate, e.g. quarterly GDP into months
    if agg is Agg.SUM and native_months is None:
        raise ValueError("SUM aggregation needs a calendar-aligned native frequency")

    buckets: dict[date, list[tuple[date, Decimal]]] = defaultdict(list)
    for d, v in obs.items():
        if v is not None:
            buckets[period_start(d, target)].append((d, v))

    out: dict[date, Decimal] = {}
    for start, points in buckets.items():
        end = period_end(start, target)
        if end > as_of:
            continue
        points.sort()
        dates = [d for d, _ in points]
        values = [v for _, v in points]

        if native_months is not None:
            expected = target.months // native_months
            complete = len(points) == expected
            has_last = dates[-1] == add_months(start, target.months - native_months)
        else:
            complete = True  # business-day calendars are not modelled; rely on staleness
            has_last = (end - dates[-1]) <= timedelta(days=STALE_DAYS)

        if agg is Agg.SUM:
            ok, value = complete, sum(values, Decimal(0))
        elif agg is Agg.AVG:
            ok, value = complete, sum(values, Decimal(0)) / len(values)
        else:
            ok, value = has_last, values[-1]

        if ok:
            out[end] = value
        else:
            log.debug("Dropping incomplete %s period ending %s (%d obs)", target.value, end, len(points))
    return out
