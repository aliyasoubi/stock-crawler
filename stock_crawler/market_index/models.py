"""Row types for the Market, MarketIndexMaster and MarketIndexData tables."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

INDEX_CODE_MAX = 20  # varchar(20)
INDEX_NAME_MAX = 100  # nvarchar(100)


@dataclass(frozen=True)
class MarketInfo:
    market_id: int
    code: str
    country_code: str
    country_name: str
    base_currency: str


@dataclass(frozen=True)
class IndexInfo:
    index_id: int
    code: str
    market_id: int
    name: str


@dataclass(frozen=True)
class MarketIndexData:
    trade_date: date
    index_id: int
    close_price: Decimal


def load_markets(path: Path) -> dict[int, MarketInfo]:
    markets: dict[int, MarketInfo] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            info = MarketInfo(
                market_id=int(row["MarketId"]),
                code=row["MarketCode"].strip().upper(),
                country_code=row["CountryCode"].strip().upper(),
                country_name=row["CountryName"].strip(),
                base_currency=row["BaseCurrency"].strip().upper(),
            )
            if info.market_id in markets:
                raise ValueError(f"{path}:{line}: duplicate MarketId {info.market_id}")
            if not (len(info.code) <= 10 and len(info.country_code) == 2
                    and len(info.country_name) <= 50 and len(info.base_currency) == 3):
                raise ValueError(f"{path}:{line}: value does not fit the Market table columns")
            markets[info.market_id] = info
    return markets


def load_index_map(path: Path) -> dict[str, IndexInfo]:
    indices: dict[str, IndexInfo] = {}
    seen_ids: set[int] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            info = IndexInfo(
                index_id=int(row["IndexId"]),
                code=row["IndexCode"].strip().upper(),
                market_id=int(row["MarketId"]),
                name=row["IndexName"].strip(),
            )
            if info.index_id in seen_ids or info.code in indices:
                raise ValueError(f"{path}:{line}: duplicate IndexId or IndexCode ({info.code})")
            if len(info.code) > INDEX_CODE_MAX or len(info.name) > INDEX_NAME_MAX:
                raise ValueError(f"{path}:{line}: IndexCode/IndexName too long for the table")
            seen_ids.add(info.index_id)
            indices[info.code] = info
    return indices
