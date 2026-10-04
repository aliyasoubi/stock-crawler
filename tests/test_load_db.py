"""load_db: CSV -> #stage -> table, checked with a fake cursor. Offline, no SQL Server needed."""
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

from stock_crawler.load_db import loader
from stock_crawler.load_db.loader import LoadError, Table

TABLES_CSV = Path(__file__).resolve().parent.parent / "config" / "db_tables.csv"
CREATE_TABLES_SQL = Path(__file__).resolve().parent.parent / "deploy" / "create_tables.sql"


class FakeCursor:
    """Answers the few statements load_table sends, and records them."""

    def __init__(self, columns, duplicate=None):
        self.columns, self.duplicate = columns, duplicate
        self.sql, self.staged, self.sizes = [], [], None
        self.rowcount, self._rows = -1, []

    def execute(self, sql, *params):
        self.sql.append(sql)
        self._rows = [(c,) for c in self.columns] if "sys.columns" in sql else []
        self.rowcount = {"UPDATE": 1, "INSERT": 2}.get(sql.split()[0], -1)
        self._one = (len(self.staged),) if sql.startswith("SELECT COUNT(*)") else self.duplicate
        return self

    def executemany(self, sql, rows):
        self.staged += rows

    def setinputsizes(self, sizes):
        self.sizes = sizes

    def fetchone(self):
        return self._one

    def __iter__(self):
        return iter(self._rows)

    def close(self):
        pass


def write(tmp_path, text, name="data.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_tables_csv_has_keys_for_every_table():
    tables = loader.read_tables(TABLES_CSV)
    assert tables[0].name == "dbo.Market"  # parents first
    assert all(t.keys for t in tables)
    assert {t.name: t.keys for t in tables}["dbo.MarketData"] == ["TradeDate", "CompanyId"]


def test_create_tables_sql_matches_the_tables_csv():
    """deploy/create_tables.sql creates exactly the tables in db_tables.csv, each with its key columns."""
    created = dict(re.findall(r"CREATE TABLE (\S+) \((.*?)\);\n", CREATE_TABLES_SQL.read_text(encoding="utf-8"), re.S))
    tables = loader.read_tables(TABLES_CSV)
    assert sorted(created) == sorted(t.name for t in tables)
    for table in tables:
        columns = {line.split()[0] for line in created[table.name].splitlines()[1:] if line.strip()}
        assert set(table.keys) <= columns, table.name


def test_quote_escapes_brackets_and_splits_schema():
    assert loader.quote("dbo.Company") == "[dbo].[Company]"
    assert loader.quote("a]b") == "[a]]b]"


def test_password_is_appended_and_escaped():
    assert loader.connection_string("SERVER=x;", None) == "SERVER=x;"
    assert loader.connection_string("SERVER=x;", "p;w}d") == "SERVER=x;PWD={p;w}}d}"


def test_upsert_updates_only_changed_rows_and_respects_source_priority():
    update, insert = loader.upsert_sql("dbo.MarketData", ["TradeDate", "CompanyId", "ClosePrice", "SourcePriority"],
                                       ["TradeDate", "CompanyId"])
    assert update == (
        "UPDATE t SET [ClosePrice] = s.[ClosePrice], [SourcePriority] = s.[SourcePriority]"
        " FROM [dbo].[MarketData] AS t JOIN #stage AS s ON t.[TradeDate] = s.[TradeDate] AND t.[CompanyId] = s.[CompanyId]"
        " WHERE EXISTS (SELECT s.[ClosePrice], s.[SourcePriority] EXCEPT SELECT t.[ClosePrice], t.[SourcePriority])"
        " AND s.[SourcePriority] <= t.[SourcePriority]")
    assert insert == (
        "INSERT INTO [dbo].[MarketData] ([TradeDate], [CompanyId], [ClosePrice], [SourcePriority])"
        " SELECT [TradeDate], [CompanyId], [ClosePrice], [SourcePriority] FROM #stage AS s"
        " WHERE NOT EXISTS (SELECT 1 FROM [dbo].[MarketData] AS t"
        " WHERE t.[TradeDate] = s.[TradeDate] AND t.[CompanyId] = s.[CompanyId])")


def test_upsert_of_a_key_only_table_only_inserts():
    update, _ = loader.upsert_sql("dbo.Pair", ["A", "B"], ["A", "B"])
    assert update is None


def test_load_table_stages_the_columns_the_table_has(tmp_path, caplog):
    path = write(tmp_path, "﻿Ticker,MarketId,FullName,IpoDateSource\r\n"
                           "AKBNK,1,AKBANK T.A.Ş.,listing_date\r\nA1CAP,1,,listing_date\r\n")
    cursor = FakeCursor(["CompanyId", "Ticker", "MarketId", "FullName"])
    rows, updated, inserted = loader.load_table(cursor, Table("dbo.Company", path, ["MarketId", "Ticker"]))

    assert (rows, updated, inserted) == (2, 1, 2)
    assert cursor.staged == [["AKBNK", "1", "AKBANK T.A.Ş."], ["A1CAP", "1", None]]  # BOM gone, "" -> NULL
    assert "SELECT TOP 0 [Ticker], [MarketId], [FullName] INTO #stage FROM [dbo].[Company]" in cursor.sql
    assert cursor.sizes == [(loader.SQL_WVARCHAR, 5, 0), (loader.SQL_WVARCHAR, 1, 0), (loader.SQL_WVARCHAR, 13, 0)]
    assert cursor.sql[-1] == "DROP TABLE #stage"
    assert "IpoDateSource" in caplog.text  # not in the table: named in the log


@pytest.mark.parametrize("text, columns, message", [
    ("A,B\n1,2\n", [], "table dbo.T not found"),
    ("A,B\n1,2\n", ["B"], "key column A"),
    ("A,B\n,2\n", ["A", "B"], "line 2: a key column is empty"),
    ("A,B\n1,2,3\n", ["A", "B"], "line 2: 3 values, the header has 2"),
])
def test_load_table_refuses_a_csv_that_does_not_fit(tmp_path, text, columns, message):
    cursor = FakeCursor(columns)
    with pytest.raises(LoadError, match=message):
        loader.load_table(cursor, Table("dbo.T", write(tmp_path, text), ["A"]))
    assert not cursor.staged


def test_load_table_refuses_duplicate_keys(tmp_path):
    cursor = FakeCursor(["A", "B"], duplicate=("1",))
    with pytest.raises(LoadError, match="key {'A': '1'} is in more than one row"):
        loader.load_table(cursor, Table("dbo.T", write(tmp_path, "A,B\n1,2\n1,3\n"), ["A"]))
    assert not any(sql.startswith(("UPDATE", "INSERT INTO [")) for sql in cursor.sql)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor, self.calls = cursor, []

    def execute(self, sql):
        self.calls.append(sql)

    def cursor(self):
        return self._cursor

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


@pytest.mark.parametrize("data, options, calls, code", [
    ("A,B\n1,2\n", [], ["commit"], 0),
    ("A,B\n1,2\n", ["--dry-run"], ["rollback"], 0),          # once, at the end
    ("A,B\n,2\n", [], ["rollback"], 1),                      # empty key: refused, table unchanged
])
def test_main_commits_each_table_and_skips_missing_files(tmp_path, monkeypatch, data, options, calls, code):
    tables = write(tmp_path, f"Table,Csv,Keys\ndbo.T,{write(tmp_path, data)},A\n"
                             f"dbo.Later,{tmp_path / 'not_crawled_yet.csv'},A\n", name="tables.csv")
    connection = FakeConnection(FakeCursor(["A", "B"]))
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=lambda _: connection, Error=RuntimeError))
    assert loader.main(["--connection", "DSN=x", "--tables-csv", str(tables), *options]) == code
    assert connection.calls == ["SET DATEFORMAT ymd", *calls, "close"]
