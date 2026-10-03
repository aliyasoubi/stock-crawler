"""Date helpers shared by the crawlers."""
from __future__ import annotations

from datetime import date


def years_before(day: date, years: int) -> date:
    """The same calendar day `years` years earlier (28 February for 29 February)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)
