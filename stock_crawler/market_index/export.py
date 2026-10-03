"""CSV writers matching the SQL Server tables (UTF-8; BULK INSERT with CODEPAGE = '65001')."""
from __future__ import annotations

import csv
import os
from pathlib import Path

from .models import IndexInfo, MarketIndexData, MarketInfo

MARKET_FILE = "market.csv"
MASTER_FILE = "market_index_master.csv"
DATA_FILE = "market_index_data.csv"


def _write_atomic(path: Path, header: list[str], rows: list[list]) -> None:
    """Write via a temp file so a crash never leaves a half-written CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    os.replace(tmp, path)


def write_market_csv(markets: list[MarketInfo], path: Path) -> None:
    _write_atomic(
        path,
        ["MarketId", "MarketCode", "CountryCode", "CountryName", "BaseCurrency"],
        [[m.market_id, m.code, m.country_code, m.country_name, m.base_currency]
         for m in sorted(markets, key=lambda m: m.market_id)],
    )


def write_master_csv(indices: list[IndexInfo], path: Path) -> None:
    _write_atomic(
        path,
        ["IndexId", "IndexCode", "MarketId", "IndexName"],
        [[i.index_id, i.code, i.market_id, i.name] for i in sorted(indices, key=lambda i: i.index_id)],
    )


def write_data_csv(rows: list[MarketIndexData], path: Path) -> None:
    _write_atomic(
        path,
        ["TradeDate", "IndexId", "ClosePrice"],
        [[r.trade_date.isoformat(), r.index_id, f"{r.close_price:f}"] for r in rows],
    )
