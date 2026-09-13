from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from stock_crawler.units import (
    UnitError,
    decode_presentation_currency,
    is_twelve_month_span,
    parse_dmy_date,
    parse_source_timestamp,
    parse_structured_number,
    parse_turkish_number,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.234.567", Decimal("1234567")),
        ("1.234,56", Decimal("1234.56")),
        ("0,78", Decimal("0.78")),
        ("-500.000", Decimal("-500000")),
        ("(1.234)", Decimal("-1234")),
        ("−12", Decimal("-12")),
        ("+7", Decimal("7")),
        ("0", Decimal("0")),
        ("", None),
        ("-", None),
        ("—", None),
        (None, None),
    ],
)
def test_parse_turkish_number(text, expected):
    assert parse_turkish_number(text) == expected


@pytest.mark.parametrize("text", ["1.23", "12.3456", "1,2,3", "abc", "1.234.56", "1,234.56"])
def test_parse_turkish_number_rejects_malformed(text):
    with pytest.raises(UnitError):
        parse_turkish_number(text)


def test_structured_numbers_keep_decimal_point_semantics():
    assert parse_structured_number("1234.56") == Decimal("1234.56")
    assert parse_structured_number(12) == Decimal(12)
    assert parse_structured_number(None) is None
    with pytest.raises(UnitError):
        parse_structured_number(True)


@pytest.mark.parametrize(
    "raw, expected",
    [("TL", ("TRY", 1)), ("1000TL", ("TRY", 1000)), ("1000000TL", ("TRY", 1_000_000)), ("USD", ("USD", 1)), ("1000 usd", ("USD", 1000))],
)
def test_decode_presentation_currency(raw, expected):
    assert decode_presentation_currency(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "1000000000TL", "100TL", "BTC", "Bin TL"])
def test_unknown_presentation_currency_is_an_error_not_try(raw):
    with pytest.raises(UnitError):
        decode_presentation_currency(raw)


def test_dates_and_timestamps_are_day_month_year_istanbul():
    assert parse_dmy_date("31.12.2024") == date(2024, 12, 31)
    assert parse_dmy_date("05/03/2025") == date(2025, 3, 5)
    with pytest.raises(UnitError):
        parse_dmy_date("2024-12-31")
    stamp = parse_source_timestamp("05.03.2025 18:45:00")
    assert stamp == datetime(2025, 3, 5, 15, 45, tzinfo=timezone.utc)
    assert parse_source_timestamp("2025-03-05T18:45:00+03:00") == stamp


def test_twelve_month_span():
    assert is_twelve_month_span(date(2024, 1, 1), date(2024, 12, 31))
    assert is_twelve_month_span(date(2023, 4, 1), date(2024, 3, 31))
    assert not is_twelve_month_span(date(2024, 1, 1), date(2024, 9, 30))
    assert not is_twelve_month_span(None, date(2024, 12, 31))
