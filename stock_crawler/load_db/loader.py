"""Load the output CSV files into SQL Server. Run: python -m stock_crawler load_db

config/db_tables.csv lists which CSV goes into which table, in load order (parent tables
first), and the key columns that identify a row. Each table is loaded in one transaction:

  1. the CSV is copied into a temporary table #stage with the table's column types; the
     values are sent as text (empty = NULL) and SQL Server converts them, as BULK INSERT does;
  2. rows whose key is already in the table are updated, if a value changed;
  3. the other rows are inserted.

Nothing is deleted, so running it again is safe. Only the CSV columns the table has are
loaded; the others are named in the log. In a table with a SourcePriority column, a row
never replaces one from a preferred source (lower SourcePriority).

The password is read from the DB_PASSWORD environment variable, not from the config.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from dataclasses import dataclass
from itertools import islice
import logging
import os
from pathlib import Path
import time

log = logging.getLogger("load_db")

TABLES_CSV = Path("config/db_tables.csv")
PASSWORD_ENV = "DB_PASSWORD"
CHUNK_ROWS = 10_000   # rows sent to the server per round trip
SQL_WVARCHAR = -9     # pyodbc.SQL_WVARCHAR: values are sent as text, SQL Server converts them


class LoadError(Exception):
    """A CSV that does not fit its table; the table is left unchanged."""


@dataclass
class Table:
    name: str           # e.g. dbo.MarketData
    csv: Path
    keys: list[str]     # columns that identify a row


def read_tables(path: Path) -> list[Table]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [Table(row["Table"], Path(row["Csv"]), row["Keys"].split()) for row in csv.DictReader(f)]


def quote(name: str) -> str:
    """SQL Server identifier: Ticker -> [Ticker], dbo.Company -> [dbo].[Company]."""
    return ".".join("[" + part.replace("]", "]]") + "]" for part in name.split("."))


def connection_string(connection: str, password: str | None) -> str:
    """The configured string plus the password; the braces keep ; and } in it literal."""
    if not password:
        return connection
    return f"{connection.rstrip(';')};PWD={{{password.replace('}', '}}')}}}"


def upsert_sql(table: str, columns: list[str], keys: list[str]) -> tuple[str | None, str]:
    """UPDATE of the rows that changed (None if every column is a key), and INSERT of the new rows."""
    target = quote(table)
    match = " AND ".join(f"t.{quote(k)} = s.{quote(k)}" for k in keys)
    values = [c for c in columns if c not in keys]
    update = None
    if values:
        # EXCEPT compares NULLs as equal, so a row is only rewritten when a value really changed.
        changed = (f"EXISTS (SELECT {', '.join('s.' + quote(c) for c in values)}"
                   f" EXCEPT SELECT {', '.join('t.' + quote(c) for c in values)})")
        if "SourcePriority" in values:
            changed += " AND s.[SourcePriority] <= t.[SourcePriority]"
        update = (f"UPDATE t SET {', '.join(f'{quote(c)} = s.{quote(c)}' for c in values)}"
                  f" FROM {target} AS t JOIN #stage AS s ON {match} WHERE {changed}")
    names = ", ".join(map(quote, columns))
    insert = (f"INSERT INTO {target} ({names}) SELECT {names} FROM #stage AS s"
              f" WHERE NOT EXISTS (SELECT 1 FROM {target} AS t WHERE {match})")
    return update, insert


def read_rows(path: Path, positions: list[int], keys: list[int]):
    """The values at `positions` of every data row, "" as None. Checks the row length and keys."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        width = len(next(reader))
        for row in reader:
            if len(row) != width:
                raise LoadError(f"{path} line {reader.line_num}: {len(row)} values, the header has {width}")
            values = [row[i] or None for i in positions]
            if any(values[k] is None for k in keys):
                raise LoadError(f"{path} line {reader.line_num}: a key column is empty")
            yield values


def load_table(cursor, table: Table) -> tuple[int, int, int]:
    """Stage `table.csv` and merge it into the table. Returns (rows in the file, updated, inserted)."""
    with table.csv.open(newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    existing = {row[0] for row in cursor.execute(
        "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(?) AND is_computed = 0", table.name)}
    if not existing:
        raise LoadError(f"table {table.name} not found")
    if not table.keys:
        raise LoadError(f"no Keys for {table.name} in the tables CSV")
    columns = [c for c in header if c in existing]
    if ignored := [c for c in header if c not in existing]:
        log.warning("%s: no column %s in %s, not loaded", table.csv, ", ".join(ignored), table.name)
    if missing := [k for k in table.keys if k not in columns]:
        raise LoadError(f"key column {', '.join(missing)} must be in both {table.csv} and {table.name}")
    positions = [header.index(c) for c in columns]
    keys = [columns.index(k) for k in table.keys]

    # First pass: check every row and find the longest value of each column.
    rows, widths = 0, [1] * len(columns)
    for values in read_rows(table.csv, positions, keys):
        rows += 1
        widths = [max(w, len(v or "")) for w, v in zip(widths, values)]

    # #stage gets the table's column types and collations, so it compares like for like with the table.
    cursor.execute("DROP TABLE IF EXISTS #stage")
    cursor.execute(f"SELECT TOP 0 {', '.join(map(quote, columns))} INTO #stage FROM {quote(table.name)}")
    cursor.setinputsizes([(SQL_WVARCHAR, w if w <= 4000 else 0, 0) for w in widths])
    insert_stage = f"INSERT INTO #stage VALUES ({', '.join('?' * len(columns))})"
    values = read_rows(table.csv, positions, keys)
    while chunk := list(islice(values, CHUNK_ROWS)):
        cursor.executemany(insert_stage, chunk)
    if (staged := cursor.execute("SELECT COUNT(*) FROM #stage").fetchone()[0]) != rows:
        raise LoadError(f"only {staged:,} of {rows:,} rows reached #stage")

    key_list = ", ".join(map(quote, table.keys))
    duplicate = cursor.execute(
        f"SELECT TOP 1 {key_list} FROM #stage GROUP BY {key_list} HAVING COUNT(*) > 1").fetchone()
    if duplicate:
        raise LoadError(f"{table.csv}: key {dict(zip(table.keys, duplicate))} is in more than one row")

    update, insert = upsert_sql(table.name, columns, table.keys)
    log.debug("%s\n%s", update, insert)
    updated = cursor.execute(update).rowcount if update else 0
    inserted = cursor.execute(insert).rowcount
    cursor.execute("DROP TABLE #stage")
    return rows, updated, inserted


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m stock_crawler load_db", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--connection", required=True,
                   help=f"ODBC connection string without the password (that comes from ${PASSWORD_ENV})")
    p.add_argument("--tables-csv", type=Path, default=TABLES_CSV,
                   help="which CSV goes into which table, in load order (default: %(default)s)")
    p.add_argument("--tables", nargs="+", metavar="TABLE",
                   help="load only these tables, e.g. MarketIndexData MacroSovereign (default: all)")
    p.add_argument("--dry-run", action="store_true",
                   help="load every table, report the counts, then roll back: nothing is changed")
    p.add_argument("-v", "--verbose", action="store_true", help="also log the SQL statements")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tables = read_tables(args.tables_csv)
    if args.tables:
        wanted = set(args.tables)
        if unknown := wanted - {t.name for t in tables} - {t.name.split(".")[-1] for t in tables}:
            log.error("Not in %s: %s", args.tables_csv, ", ".join(sorted(unknown)))
            return 2
        tables = [t for t in tables if t.name in wanted or t.name.split(".")[-1] in wanted]
    try:
        import pyodbc
    except ImportError as exc:  # pyodbc, or the ODBC driver manager it needs, is not installed
        log.error("load_db needs pyodbc and Microsoft's ODBC Driver for SQL Server (README section 9): %s", exc)
        return 1
    try:
        connection = pyodbc.connect(connection_string(args.connection, os.environ.get(PASSWORD_ENV)))
    except pyodbc.Error as exc:
        log.error("Cannot connect to SQL Server: %s", exc)
        return 1

    failed = []
    with closing(connection):
        connection.execute("SET DATEFORMAT ymd")  # reads YYYY-MM-DD correctly into datetime columns too
        for table in tables:
            if not table.csv.exists():
                log.warning("%-24s skipped: %s not found", table.name, table.csv)
                continue
            started = time.monotonic()
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.fast_executemany = True
                    rows, updated, inserted = load_table(cursor, table)
            except (LoadError, OSError, UnicodeError, csv.Error, pyodbc.Error) as exc:
                connection.rollback()
                log.error("%-24s not loaded, unchanged: %s", table.name, exc)
                failed.append(table.name)
                continue
            if not args.dry_run:  # a dry run keeps every table pending, so child tables find their parents
                connection.commit()
            log.info("%-24s %9s rows: %s inserted, %s updated, %s unchanged (%.0f s)",
                     table.name, f"{rows:,}", f"{inserted:,}", f"{updated:,}",
                     f"{rows - inserted - updated:,}", time.monotonic() - started)
        if args.dry_run:
            connection.rollback()
            log.info("Dry run: everything rolled back, the database is unchanged")
    if failed:
        log.error("Failed: %s", ", ".join(failed))
    return 1 if failed else 0
