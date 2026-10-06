"""load_db against a real SQL Server. Skipped unless LOAD_DB_TEST_CONNECTION is set:

    export LOAD_DB_TEST_CONNECTION="DRIVER={ODBC Driver 18 for SQL Server};SERVER=127.0.0.1,1433;DATABASE=LoadDbTest;UID=sa;Encrypt=yes;TrustServerCertificate=yes"
    export DB_PASSWORD='...'
    .venv/bin/python -m pytest tests/test_load_db_live.py -v

It creates two tables of its own, dbo.LoadDbTest_Market and dbo.LoadDbTest_Price, and drops
them afterwards. Nothing else is touched; still, point it at a test database.
"""
from datetime import date, datetime
from decimal import Decimal
import logging
import os
import re

import pytest

from stock_crawler.load_db import loader

CONNECTION = os.environ.get("LOAD_DB_TEST_CONNECTION")
pytestmark = pytest.mark.skipif(not CONNECTION, reason="set LOAD_DB_TEST_CONNECTION to test against SQL Server")

MARKET, PRICE = "dbo.LoadDbTest_Market", "dbo.LoadDbTest_Price"
CREATE = f"""
CREATE TABLE {MARKET} (MarketId int NOT NULL PRIMARY KEY, MarketCode varchar(10) NOT NULL, CountryName nvarchar(50) NULL);
CREATE TABLE {PRICE} (
    PriceId int IDENTITY PRIMARY KEY,
    TradeDate date NOT NULL,
    MarketId int NOT NULL REFERENCES {MARKET} (MarketId),
    Ticker varchar(10) NOT NULL,
    ClosePrice decimal(18,4) NULL,
    Volume bigint NULL,
    IsActive bit NULL,
    LoadedAt datetime NULL,
    SourcePriority tinyint NOT NULL,
    UNIQUE (TradeDate, MarketId, Ticker));
"""
DROP = f"DROP TABLE IF EXISTS {PRICE}; DROP TABLE IF EXISTS {MARKET};"

MARKETS = "﻿MarketId,MarketCode,CountryName,NotInTable\n1,BIST,Türkiye,x\n"
PRICES = ("TradeDate,MarketId,Ticker,ClosePrice,Volume,IsActive,LoadedAt,SourcePriority\n"
          "2026-09-15,1,AKBNK,13892.29981234,1000,1,2026-09-15 18:30:00,1\n"
          "2026-09-15,1,THYAO,,,0,,2\n")


@pytest.fixture
def db():
    pyodbc = pytest.importorskip("pyodbc")
    # No connection pooling: a pooled connection keeps the manual-commit mode load_db left it
    # in, although autocommit=True is asked for, and its CREATE TABLE would block load_db.
    pyodbc.pooling = False
    connection = pyodbc.connect(loader.connection_string(CONNECTION, os.environ.get(loader.PASSWORD_ENV)),
                                autocommit=True)
    connection.execute(DROP)
    connection.execute(CREATE)
    yield connection
    connection.execute(DROP)
    connection.close()


def load(tmp_path, caplog, markets, prices, *options):
    """Run load_db on the two CSVs. Returns its exit code and {table: (inserted, updated)} from its log."""
    (tmp_path / "markets.csv").write_text(markets, encoding="utf-8")
    (tmp_path / "prices.csv").write_text(prices, encoding="utf-8")
    tables = tmp_path / "tables.csv"
    tables.write_text(f"Table,Csv,Keys\n{MARKET},{tmp_path / 'markets.csv'},MarketId\n"
                      f"{PRICE},{tmp_path / 'prices.csv'},TradeDate MarketId Ticker\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="load_db"):
        code = loader.main(["--connection", CONNECTION, "--tables-csv", str(tables), *options])
    counts = {}
    for record in caplog.records:
        if m := re.match(r"(\S+) +[\d,]+ rows: ([\d,]+) inserted, ([\d,]+) updated", record.getMessage()):
            counts[m[1]] = (int(m[2].replace(",", "")), int(m[3].replace(",", "")))
    return code, counts


def prices(db):
    return [tuple(row) for row in db.execute(
        f"SELECT TradeDate, Ticker, ClosePrice, Volume, IsActive, LoadedAt, SourcePriority FROM {PRICE}"
        " ORDER BY TradeDate, Ticker")]


def test_load_then_rerun_then_change(db, tmp_path, caplog):
    # First load: every row is inserted, and SQL Server converts the text to each column's type.
    assert load(tmp_path, caplog, MARKETS, PRICES) == (0, {MARKET: (1, 0), PRICE: (2, 0)})
    assert "NotInTable" in caplog.text  # a CSV column the table doesn't have: named in the log
    assert tuple(db.execute(f"SELECT MarketCode, CountryName FROM {MARKET}").fetchone()) == ("BIST", "Türkiye")
    assert prices(db) == [
        (date(2026, 9, 15), "AKBNK", Decimal("13892.2998"), 1000, True, datetime(2026, 9, 15, 18, 30), 1),
        (date(2026, 9, 15), "THYAO", None, None, False, None, 2)]

    # The same files again: nothing to do (the extra decimals round to the same stored value).
    assert load(tmp_path, caplog, MARKETS, PRICES) == (0, {MARKET: (0, 0), PRICE: (0, 0)})

    # One changed value and one new day.
    changed = PRICES.replace(",1000,", ",2000,") + "2026-09-16,1,AKBNK,14000,500,1,,1\n"
    assert load(tmp_path, caplog, MARKETS, changed) == (0, {MARKET: (0, 0), PRICE: (1, 1)})
    assert [row[3] for row in prices(db)] == [2000, None, 500]


def test_a_row_never_replaces_one_from_a_preferred_source(db, tmp_path, caplog):
    load(tmp_path, caplog, MARKETS, PRICES)  # THYAO has SourcePriority 2
    worse = PRICES.replace("THYAO,,,0,,2", "THYAO,99,,0,,3")
    assert load(tmp_path, caplog, MARKETS, worse)[1][PRICE] == (0, 0)
    better = PRICES.replace("THYAO,,,0,,2", "THYAO,99,,0,,1")
    assert load(tmp_path, caplog, MARKETS, better)[1][PRICE] == (0, 1)
    thyao = prices(db)[1]
    assert (thyao[2], thyao[6]) == (Decimal(99), 1)  # ClosePrice, SourcePriority


@pytest.mark.parametrize("bad_prices, error", [
    (PRICES + "2026-09-16,7,AKBNK,1,1,1,,1\n", "FOREIGN KEY"),               # MarketId 7 is not a market
    (PRICES.replace("13892.29981234", "n/a"), "converting|reached #stage"),  # not a number
    (PRICES + PRICES.splitlines(keepends=True)[1], "more than one row"),     # the same key twice
])
def test_a_failing_table_is_rolled_back_and_the_others_are_loaded(db, tmp_path, caplog, bad_prices, error):
    assert load(tmp_path, caplog, MARKETS, bad_prices) == (1, {MARKET: (1, 0)})
    assert re.search(error, caplog.text)
    assert prices(db) == []


def test_dry_run_changes_nothing(db, tmp_path, caplog):
    # The prices find their market although it is never committed: a dry run keeps every table pending.
    assert load(tmp_path, caplog, MARKETS, PRICES, "--dry-run") == (0, {MARKET: (1, 0), PRICE: (2, 0)})
    assert db.execute(f"SELECT COUNT(*) FROM {MARKET}").fetchval() == 0
    assert prices(db) == []
