"""Parsers for KAP company pages and the Borsa İstanbul listing workbook.

Pure functions only: no network access and no file I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

LOG = logging.getLogger(__name__)

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# Borsa code suffixes: .E = equity (share), .F = fund/ETF, others are rights, certificates etc.
EQUITY_SUFFIX = "E"


@dataclass(frozen=True)
class Listing:
    listing_date: date | None
    first_trading_date: date | None


class Tables(HTMLParser):
    """Collect every table's rows as lists of whitespace-normalised cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.stack: list[list[list[str]]] = []
        self.row: list[str] | None = None
        self.cell: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.stack.append([])
        elif self.stack and tag == "tr":
            self.row = []
        elif self.row is not None and tag in ("td", "th"):
            self.cell = tag
            self.parts = []

    def handle_data(self, data):
        if self.cell:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == self.cell and self.row is not None:
            self.row.append(" ".join("".join(self.parts).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.stack:
            self.stack[-1].append(self.row)
            self.row = None
        elif tag == "table" and self.stack:
            self.tables.append(self.stack.pop())


def parse_profile_sector(html: str) -> str | None:
    """KAP profile "Şirketin Sektörü"; several sector labels are joined with '; '."""
    match = re.search(r"<h3[^>]*>Şirketin Sektörü</h3>\s*<div[^>]*>(.*?)</div>", html, re.S)
    if not match:
        return None
    sectors = [" ".join(unescape(x).split()) for x in re.findall(r"<a\b[^>]*>([^<]+)</a>", match.group(1))]
    return "; ".join(s for s in sectors if s) or None


def parse_financial_currency(html: str) -> str | None:
    """Latest non-empty KAP "Sunum Para Birimi" (financial statement presentation currency),
    as published, e.g. "TL", "1000TL" or "USD"."""
    parser = Tables()
    parser.feed(html)
    for table in parser.tables:
        for row in table:
            if row and row[0] == "Sunum Para Birimi":
                # KAP orders periods oldest to newest.
                value = next((v for v in reversed(row[1:]) if v), None)
                if value:
                    return value
    return None


def read_listing_workbook(zip_data: bytes) -> dict[str, Listing]:
    """Equity codes from Borsa İstanbul's ilkislem.zip, mapped to their listing dates.

    Column D is the market listing date; older listings leave it blank and only fill
    column E (first trading day), so both are returned.
    """
    try:
        archive = ZipFile(BytesIO(zip_data))
    except BadZipFile as exc:
        raise ValueError("Borsa listing archive is not a ZIP file") from exc
    with archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".xlsx")]
        if len(names) != 1:
            raise ValueError("Borsa listing archive must contain exactly one XLSX workbook")
        with ZipFile(BytesIO(archive.read(names[0]))) as book:
            workbook = ET.fromstring(book.read("xl/workbook.xml"))
            rows, strings = _first_sheet(book, workbook)
            props = workbook.find(f"{XLSX_NS}workbookPr")
            date1904 = props is not None and props.get("date1904") in ("1", "true")
    epoch = date(1904, 1, 1) if date1904 else date(1899, 12, 30)

    def cells(row: ET.Element) -> dict[str, str]:
        out = {}
        for cell in row.findall(f"{XLSX_NS}c"):
            column = re.match(r"[A-Z]+", cell.attrib.get("r", ""))
            if not column:
                continue
            raw = cell.findtext(f"{XLSX_NS}v")
            if cell.attrib.get("t") == "s" and raw is not None:
                out[column.group()] = strings[int(raw)]
            elif cell.attrib.get("t") == "inlineStr":
                out[column.group()] = "".join(t.text or "" for t in cell.iter(f"{XLSX_NS}t"))
            else:
                out[column.group()] = raw or ""
        return out

    def excel_date(raw: str) -> date | None:
        try:
            return epoch + timedelta(days=int(float(raw)))
        except (ValueError, OverflowError):
            return None  # blank or "VY" (not available)

    header = cells(rows[0]) if rows else {}
    if ("CURRENT CODE" not in header.get("B", "").upper()
            or "LISTING DATE" not in header.get("D", "").upper()
            or "FIRST TRADING DAY" not in header.get("E", "").upper()):
        raise ValueError("Borsa listing workbook columns changed; update read_listing_workbook")

    listings: dict[str, Listing] = {}
    for row in rows[1:]:
        values = cells(row)
        code, _, suffix = values.get("B", "").strip().upper().rpartition(".")
        if not code or suffix != EQUITY_SUFFIX:
            continue
        listing = Listing(excel_date(values.get("D", "")), excel_date(values.get("E", "")))
        previous = listings.get(code)
        if previous and previous != listing:
            LOG.warning("Borsa workbook lists %s twice; keeping the earlier dates", code)
            listing = min(previous, listing, key=lambda x: x.listing_date or x.first_trading_date or date.max)
        listings[code] = listing
    if len(listings) < 100:
        raise ValueError(f"Only {len(listings)} equity codes in Borsa workbook; format may have changed")
    return listings


def _first_sheet(book: ZipFile, workbook: ET.Element) -> tuple[list[ET.Element], list[str]]:
    sheet = workbook.find(f"{XLSX_NS}sheets/{XLSX_NS}sheet")
    if sheet is None:
        raise ValueError("No worksheets in Borsa workbook")
    relationship_id = sheet.attrib[f"{REL_NS}id"]
    relationships = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    target = next((e.attrib["Target"] for e in relationships if e.attrib.get("Id") == relationship_id), None)
    if not target:
        raise ValueError("Borsa worksheet relationship missing")
    sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if not sheet_path.startswith("xl/worksheets/"):
        raise ValueError("Unexpected Borsa worksheet path")
    strings = []
    if "xl/sharedStrings.xml" in book.namelist():
        root = ET.fromstring(book.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in item.iter(f"{XLSX_NS}t")) for item in root.findall(f"{XLSX_NS}si")]
    rows = ET.fromstring(book.read(sheet_path)).findall(f"{XLSX_NS}sheetData/{XLSX_NS}row")
    return rows, strings
