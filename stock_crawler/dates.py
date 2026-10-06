"""Date helpers shared by the crawlers."""
from __future__ import annotations

from datetime import date


def years_start(end: date, years: int) -> date:
    """First day of a `years`-year period ending at `end`: 1 January, `years` years before
    `end`'s year, so the first year is complete. 10 years to 2026-10-06 start on 2016-01-01."""
    return date(end.year - years, 1, 1)
