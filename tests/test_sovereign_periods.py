from datetime import date
from decimal import Decimal as D

import pytest

from stock_crawler.sovereign.fields import Agg, Freq, PeriodType
from stock_crawler.sovereign.periods import aggregate, parse_evds_date, period_end, period_start

AS_OF = date(2026, 9, 26)


@pytest.mark.parametrize(
    "raw, freq, expected",
    [
        ("22-12-2025", Freq.BUSINESS_DAY, date(2025, 12, 22)),
        ("2021-01", Freq.MONTHLY, date(2021, 1, 1)),
        ("2024-Q3", Freq.QUARTERLY, date(2024, 7, 1)),
        (2025, Freq.ANNUAL, date(2025, 1, 1)),
    ],
)
def test_parse_evds_date(raw, freq, expected):
    assert parse_evds_date(raw, freq) == expected


def test_period_bounds():
    assert period_start(date(2024, 8, 15), PeriodType.QUARTERLY) == date(2024, 7, 1)
    assert period_end(date(2024, 1, 1), PeriodType.MONTHLY) == date(2024, 1, 31)
    assert period_end(date(2024, 1, 1), PeriodType.QUARTERLY) == date(2024, 3, 31)
    assert period_end(date(2024, 1, 1), PeriodType.ANNUAL) == date(2024, 12, 31)


def quarters(year, values):
    return {date(year, 3 * i + 1, 1): D(v) for i, v in enumerate(values)}


def test_annual_sum_drops_partial_year():
    obs = {**quarters(2025, [1, 2, 3, 4]), **quarters(2026, [5, 6])}
    out = aggregate(obs, Freq.QUARTERLY, PeriodType.ANNUAL, Agg.SUM, date(2027, 6, 1))
    assert out == {date(2025, 12, 31): D(10)}


def test_period_not_ended_is_dropped():
    obs = {date(2026, m, 1): D(1) for m in range(7, 10)}
    assert aggregate(obs, Freq.MONTHLY, PeriodType.QUARTERLY, Agg.SUM, AS_OF) == {}


def test_last_requires_final_subperiod():
    # Q4 missing -> no year-end stock value
    obs = quarters(2025, [1, 2, 3])
    assert aggregate(obs, Freq.QUARTERLY, PeriodType.ANNUAL, Agg.LAST, AS_OF) == {}
    obs[date(2025, 10, 1)] = D(9)
    assert aggregate(obs, Freq.QUARTERLY, PeriodType.ANNUAL, Agg.LAST, AS_OF) == {date(2025, 12, 31): D(9)}


def test_monthly_avg_to_quarter():
    obs = {date(2025, 1, 1): D(1), date(2025, 2, 1): D(2), date(2025, 3, 1): D(6)}
    assert aggregate(obs, Freq.MONTHLY, PeriodType.QUARTERLY, Agg.AVG, AS_OF) == {date(2025, 3, 31): D(3)}


def test_no_disaggregation():
    assert aggregate(quarters(2025, [1, 2, 3, 4]), Freq.QUARTERLY, PeriodType.MONTHLY, Agg.SUM, AS_OF) == {}


def test_daily_last_with_staleness_guard():
    obs = {date(2025, 1, 31): D("35.1"), date(2025, 2, 10): D("36.0")}
    out = aggregate(obs, Freq.BUSINESS_DAY, PeriodType.MONTHLY, Agg.LAST, AS_OF)
    # January ends on a fresh value; February's last print is 18 days stale.
    assert out == {date(2025, 1, 31): D("35.1")}
