"""Central government gross debt stock from the Ministry of Treasury and Finance (MoTF).

hmb.gov.tr is a single-page app on a WordPress backend. The statistics page
lists Excel files whose names carry a content hash that changes with every
upload, so the current file URL is discovered from the page on each run.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date
from decimal import Decimal
from typing import Iterable

import requests

from .fields import SeriesSpec
from .evds_client import USER_AGENT

log = logging.getLogger(__name__)

PAGES_API = "https://www.hmb.gov.tr/portal/v2/pages"
STATS_PAGE_SLUG = "kamu-finansmani-istatistikleri"
DEBT_FILE_LINK_TEXT = "Merkezi Yönetim Borç Stoku Enstrüman Dağılımı"
TOTAL_COLUMN = "TOPLAM STOK"
TIMEOUT = (15, 180)  # the statistics page is large and slow (~40 s)

TR_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
}


class TreasuryError(RuntimeError):
    pass


def find_file_url(page_html: str, link_text: str) -> str:
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.S):
        label = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if label == link_text and re.search(r"\.xlsx?$", href):
            return href
    raise TreasuryError(f"Link {link_text!r} not found on the MoTF statistics page; the page layout may have changed")


def parse_debt_workbook(content: bytes) -> dict[date, Decimal]:
    """Monthly total stock (million TRY) from the one-sheet-per-year workbook."""
    import xlrd  # .xls reader; optional dependency

    book = xlrd.open_workbook(file_contents=content)
    out: dict[date, Decimal] = {}
    for sheet in book.sheets():
        if not re.fullmatch(r"\d{4}", sheet.name.strip()):
            continue  # skips the long-run annual sheet, e.g. "1986-2025"
        year = int(sheet.name)
        header_row = total_col = None
        for r in range(min(sheet.nrows, 10)):
            for c in range(sheet.ncols):
                if str(sheet.cell_value(r, c)).strip().upper().startswith(TOTAL_COLUMN):
                    header_row, total_col = r, c
                    break
            if header_row is not None:
                break
        if header_row is None:
            raise TreasuryError(f"Sheet {sheet.name}: column {TOTAL_COLUMN!r} not found")
        for r in range(header_row + 1, sheet.nrows):
            month = TR_MONTHS.get(str(sheet.cell_value(r, 1)).strip().lower())
            value = sheet.cell_value(r, total_col)
            if month and isinstance(value, float):
                out[date(year, month, 1)] = Decimal(repr(value))
    if not out:
        raise TreasuryError("No observations parsed from the MoTF debt workbook")
    return out


class TreasuryDebtSource:
    def __init__(self, verify: bool | str = True, pages_api: str = PAGES_API) -> None:
        self.pages_api = pages_api
        self.verify = verify
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._data: dict[date, Decimal] | None = None

    def _get(self, url: str, **params: str) -> requests.Response:
        resp = self.session.get(url, params=params or None, timeout=TIMEOUT, verify=self.verify)
        resp.raise_for_status()
        return resp

    def _load(self) -> dict[date, Decimal]:
        if self._data is None:
            pages = self._get(self.pages_api, slug=STATS_PAGE_SLUG, _fields="content").json()
            if not pages:
                raise TreasuryError(f"MoTF page {STATS_PAGE_SLUG!r} not found")
            url = find_file_url(pages[0]["content"]["rendered"], DEBT_FILE_LINK_TEXT)
            log.info("Downloading MoTF debt stock workbook %s", url)
            self._data = parse_debt_workbook(self._get(url).content)
        return self._data

    def validate(self, specs: Iterable[SeriesSpec]) -> None:
        self._load()

    def fetch(self, spec: SeriesSpec, start: date, end: date) -> dict[date, Decimal]:
        return {d: v for d, v in self._load().items() if start <= d <= end}
