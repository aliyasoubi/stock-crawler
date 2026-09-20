from pathlib import Path

import pytest

from stock_crawler.csvtools import CsvError, extract_candidate_tickers


def test_extract_candidates_handles_bom_quoted_headers_blanks_and_duplicates(tmp_path):
    csv_text = '﻿stock_code,yahoo_ticker,"Profit (Loss) Attributable To, Owners of Parent",Company\nTHYAO,THYAO.IS,1,"Türk Hava Yolları"\n,,2,Unknown\nthyao,THYAO.IS,3,"Türk Hava Yolları"\nASELS,ASELS.IS,4,Aselsan\nBAD.CODE,,5,x\n'
    src = tmp_path / "kap.csv"
    src.write_text(csv_text, encoding="utf-8")
    out = tmp_path / "config" / "candidates.txt"
    stats = extract_candidate_tickers(src, "stock_code", out)
    assert (stats.input_rows, stats.blank_rows, stats.invalid_rows, stats.duplicate_rows, stats.candidates) == (5, 1, 1, 1, 2)
    assert out.read_text().splitlines()[1:] == ["ASELS", "THYAO"]


def test_missing_header_is_rejected(tmp_path):
    src = tmp_path / "kap.csv"
    src.write_text("ticker,Company\nTHYAO,x\n", encoding="utf-8")
    with pytest.raises(CsvError, match="stock_code"):
        extract_candidate_tickers(src, "stock_code", tmp_path / "out.txt")
    with pytest.raises(CsvError):
        extract_candidate_tickers(tmp_path / "nope.csv", "stock_code", tmp_path / "out.txt")
