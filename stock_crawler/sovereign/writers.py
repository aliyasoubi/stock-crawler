"""CSV output."""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from .fields import FIELDS
from .pipeline import KEY_COLUMNS, MacroRow

VALUE_COLUMNS = tuple(f.column for f in FIELDS)
ALL_COLUMNS = KEY_COLUMNS + VALUE_COLUMNS


def write_csv(rows: list[MacroRow], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ALL_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _text(v) for k, v in row.as_dict(VALUE_COLUMNS).items()})
    tmp.replace(path)  # atomic: readers never see a half-written file
    return path


def _text(value: object) -> object:
    if value is None:
        return ""
    return f"{value:f}" if isinstance(value, Decimal) else value  # plain digits, never 1E+3
