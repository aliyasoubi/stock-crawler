"""Standard-library CSV utility: extract candidate tickers without touching KAP."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..core.config import TICKER_PATTERN


class CsvError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateStats:
    input_rows: int
    blank_rows: int
    invalid_rows: int
    duplicate_rows: int
    candidates: int


def extract_candidate_tickers(input_path: Path, ticker_column: str, output_path: Path) -> CandidateStats:
    """Read one identifier column, write a de-duplicated sorted list, report counts.

    Accepts UTF-8 with or without BOM and quoted headers containing commas. Missing tickers
    are counted and skipped; they are never turned into name guesses or `.IS` suffixes.
    """
    if not input_path.is_file():
        raise CsvError(f"input file not found: {input_path}")
    seen: set[str] = set()
    input_rows = blank_rows = invalid_rows = duplicate_rows = 0
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or ticker_column not in reader.fieldnames:
            raise CsvError(f"column {ticker_column!r} not found in header {reader.fieldnames}")
        for row in reader:
            input_rows += 1
            value = (row.get(ticker_column) or "").strip().upper()
            if not value:
                blank_rows += 1
                continue
            if not TICKER_PATTERN.match(value):
                invalid_rows += 1
                continue
            if value in seen:
                duplicate_rows += 1
                continue
            seen.add(value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Candidate tickers extracted from CSV; review before adding to companies.txt", *sorted(seen)]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return CandidateStats(input_rows, blank_rows, invalid_rows, duplicate_rows, len(seen))
