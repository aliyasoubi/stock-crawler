"""
Fills the CompanyFundamental columns that KAP's item search does not publish, from each
statement's full financial report on KAP.

`fundamentals` leaves Ebitda, TotalDebtShort, TotalDebtLong, CashAndEquivalents,
FreeCashFlow, Eps and SharesOutstanding empty. Every one of them is in the full report
of the same statement, so run this command after `fundamentals`:

    python -m stock_crawler fundamentals
    python -m stock_crawler fundamental_reports

How it works
------------
1. output/fundamentals/company_fundamental.csv lists the rows to fill, and
   company_fundamental_raw.csv gives each row's NotificationId: the statement `fundamentals`
   chose (consolidated before solo, then the latest).
2. A quarter with many reports missing: GET /en/api/financialTable/download/{year}/{quarter}
   -> one zip with every company's full report for that quarter (KAP's home page "Financial
   Statements" download without a company; ~3 min per quarter). Anything still missing:
   GET /en/api/notification/export/excel/{NotificationId} -> one full report, the file behind
   the "EXCEL" link on the disclosure page (KAP allows ~10 of these per 5 minutes).
3. The report's filled lines (current period only) are cached in
   cache/fundamental_reports/{NotificationId}.json.gz. A published notification never
   changes, so the cache never expires: after a new `fundamentals` run this command only
   downloads the new notifications, and changing FIELDS below needs no new downloads.
4. The columns are written into company_fundamental.csv in place; the report lines each
   value came from go to output/fundamental_reports/report_lines.csv.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import html
import json
import logging
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from stock_crawler.fundamentals.crawler import FIRST_YEAR, fmt, normalize_label, parse_number, write_csv
from stock_crawler.http_client import USER_AGENT, FetchError, HttpClient, NotFound

log = logging.getLogger("kap_reports")

REPORT_URL = "https://www.kap.org.tr/en/api/notification/export/excel/{id}"
MARKET_URL = "https://www.kap.org.tr/en/api/financialTable/download/{year}/{quarter}"
COLUMNS = ["Ebitda", "TotalDebtShort", "TotalDebtLong", "CashAndEquivalents", "FreeCashFlow", "Eps",
           "SharesOutstanding"]

# --------------------------------------------------------------------------- #
# Reading a report
# --------------------------------------------------------------------------- #
# The file is HTML saved as .xls. Each statement is a table whose rows are named after the
# statement template and XBRL role: "general_role_210015-row-4 data-input-row ...". Share-class
# values (EPS) follow their line ("typed-dimension-row ... general_role_310003-row-68 ...") as
# "new-type-row" rows.
_ROW = re.compile(r'<tr class="(?:[^"]*?\b([a-z-]+)_role_(\d+)-row-\d+([^"]*)|new-type-row)"')
_LABEL = re.compile(r'multi-language-content content-en[^"]*"[^>]*>\s*(.*?)\s*</div>', re.S)
_CELL = re.compile(r'<td class="taxonomy-context-value[^"]*">(.*?)</td>', re.S)
_TITLE = re.compile(r'title="([^"]*)"')
_CURRENCY = re.compile(r'Presentation Currency</td>\s*<td>([^<]*)</td>')


@dataclass
class Report:
    template: str                        # KAP statement template: general, holding, banks, ...
    currency: str                        # as the fundamentals CSV writes it: TL, 1000TL, ...
    lines: list[tuple[str, str, str]]    # (role, label, current-period value) of filled lines


def parse_report(text: str) -> Report:
    """Current-period value of every filled line. A share-class value is stored as
    'parent line | class caption'."""
    currency = _CURRENCY.search(text)
    rows = list(_ROW.finditer(text))
    lines: list[tuple[str, str, str]] = []
    template, role, label = "", "", ""
    for m, nxt in zip(rows, rows[1:] + [None]):
        body = text[m.end():nxt.start() if nxt else len(text)]
        cells = _CELL.findall(body)
        if m.group(2):  # an ordinary line
            template = template or m.group(1)
            if "data-input-row" not in m.group(3):
                continue
            found = _LABEL.search(body)
            role, label = m.group(2), html.unescape(found.group(1)) if found else ""
            value = _TITLE.search(cells[0]) if cells else None
            if value and value.group(1):
                lines.append((role, label, value.group(1)))
        elif cells and role:  # a share-class row of the line above
            caption = re.search(r'typed-dimension-field-caption">\s*(.*?)\s*</div>', body, re.S)
            value = " ".join(re.sub(r"<[^>]+>", " ", cells[0]).split())
            if value:
                lines.append((role, f"{label} | {html.unescape(caption.group(1)) if caption else ''}", value))
    return Report(template=template,
                  currency=re.sub(r"[.\s]", "", currency.group(1)) if currency else "",  # "1.000 TL" -> "1000TL"
                  lines=lines)


def to_decimal(text: str) -> Decimal | None:
    """Raw values ('-1234567', '0.5') and share-class values in KAP's format ('2,19000000')."""
    try:
        value = Decimal(text) if "," not in text else parse_number(text)
    except InvalidOperation:
        return None
    return value + 0 if value is not None else None  # -0 -> 0


# --------------------------------------------------------------------------- #
# From report lines to table columns
# --------------------------------------------------------------------------- #
BALANCE, INCOME, CASH_FLOW = "balance", "income", "cash flow"


def statement(role: str) -> str | None:
    """Role 2100xx: balance sheet (2105xx is off-balance sheet), 3xxxxx: income statement,
    5xxxxx: cash flow."""
    if role.startswith("2100"):
        return BALANCE
    return {"3": INCOME, "5": CASH_FLOW}.get(role[:1])


class Lookup:
    """First value of a line by statement and label; remembers the lines it used."""

    def __init__(self, report: Report):
        self.values: dict[tuple[str, str], tuple[str, Decimal]] = {}
        for role, label, value in report.lines:
            number = to_decimal(value)
            part = statement(role)
            if part and number is not None:
                self.values.setdefault((part, normalize_label(label)), (label, number))
        self.used: list[tuple[str, str, Decimal]] = []

    def get(self, part: str, *labels: str) -> Decimal | None:
        for wanted in labels:
            hit = self.values.get((part, normalize_label(wanted)))
            if hit:
                self.used.append((part, *hit))
                return hit[1]
        return None


def total(*values: Decimal | None) -> Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


def ebitda(v: Lookup) -> Decimal | None:
    operating = v.get(INCOME, "Profit (Loss) from Operating Activities")
    depreciation = v.get(CASH_FLOW, "Adjustments for depreciation and amortisation expense")
    return None if operating is None or depreciation is None else operating + depreciation


def free_cash_flow(v: Lookup) -> Decimal | None:
    operating = v.get(CASH_FLOW, "Cash Flows from (used in) Operating Activities")
    if operating is None:
        return None
    capex = v.get(CASH_FLOW, "Purchase of Property, Plant, Equipment and Intangible Assets")
    if capex is None:
        capex = total(v.get(CASH_FLOW, "Purchase of property, plant and equipment"),
                      v.get(CASH_FLOW, "Purchase of intangible assets"))
    return operating - abs(capex or 0)  # no purchase lines: nothing was bought


def shares(capital_label: str) -> Callable[[Lookup, Decimal], Decimal | None]:
    """Issued capital in TL / 1 TL nominal value per share (the BIST standard)."""
    def fn(v: Lookup, multiplier: Decimal) -> Decimal | None:
        capital = v.get(BALANCE, capital_label)
        return None if capital is None else capital * multiplier
    return fn


_GENERAL = {
    "CashAndEquivalents": lambda v: v.get(BALANCE, "Cash and cash equivalents"),
    "TotalDebtShort": lambda v: total(v.get(BALANCE, "Current Borrowings"),
                                      v.get(BALANCE, "Current Portion of Non-current Borrowings")),
    "TotalDebtLong": lambda v: v.get(BALANCE, "Long Term Borrowings"),
    "Ebitda": ebitda,
    "FreeCashFlow": free_cash_flow,
}
# Columns per KAP statement template. Financial companies have no EBITDA or free cash flow,
# and their balance sheets do not split debt into short and long term.
FIELDS: dict[str, dict[str, Callable[[Lookup], Decimal | None]]] = {
    "general": _GENERAL,
    "holding": _GENERAL,
    "banks": {
        "CashAndEquivalents": lambda v: v.get(BALANCE, "Cash and cash equivalents"),
    },
    "insurance": {
        "CashAndEquivalents": lambda v: v.get(BALANCE, "Cash and cash equivalents"),
    },
    "finance": {},  # CashAndEquivalents already comes from `fundamentals`
}
FIELDS["par-banks"] = FIELDS["banks"]  # participation banks: same lines
SHARES = {"general": shares("Issued capital"), "holding": shares("Issued capital"),
          "banks": shares("Issued capital"), "par-banks": shares("Issued capital"), "finance": shares("Issued capital"),
          "insurance": shares("Paid in Capital")}


def multiplier(currency: str) -> Decimal | None:
    """'1000TL' -> 1000; None for a non-TL statement (its capital is not a share count)."""
    m = re.fullmatch(r"(\d*)TL", currency)
    return Decimal(m.group(1) or 1) if m else None


EPS_DIGITS = Decimal("0.00000001")  # 8 decimals, as KAP publishes EPS


def columns_from(report: Report, net_income: Decimal | None = None
                 ) -> tuple[dict[str, Decimal], list[tuple[str, str, str, Decimal]]]:
    """Table columns found in the report, and (column, statement, line, value) for each line used.

    Eps is computed as net_income (the row's NetIncome, in the statement's unit) x unit /
    SharesOutstanding: companies publish EPS in different units (TL, kuruş, per 0.01 TL of
    capital), or not at all."""
    fields = FIELDS.get(report.template)
    if fields is None:
        return {}, []
    v = Lookup(report)
    out: dict[str, Decimal] = {}
    used: list[tuple[str, str, str, Decimal]] = []
    calls = dict(fields)
    mult = multiplier(report.currency)
    if report.template in SHARES and mult is not None:
        calls["SharesOutstanding"] = lambda lk: SHARES[report.template](lk, mult)
    for column, fn in calls.items():
        v.used = []
        value = fn(v)
        if value is not None:
            out[column] = value
            used += [(column, *line) for line in v.used]
    shares_out = out.get("SharesOutstanding")
    if net_income is not None and shares_out:
        out["Eps"] = (net_income * mult / shares_out).quantize(EPS_DIGITS)
        used += [("Eps", "fundamentals", "NetIncome", net_income),
                 ("Eps", "balance", *next(line[2:] for line in used if line[0] == "SharesOutstanding"))]
    return out, used


# --------------------------------------------------------------------------- #
# Fetching with a permanent cache
# --------------------------------------------------------------------------- #
class ReportCache:
    def __init__(self, http: HttpClient, root: Path, url: str):
        self.http, self.root, self.url = http, root, url
        root.mkdir(parents=True, exist_ok=True)

    def path(self, notification_id: str) -> Path:
        return self.root / f"{notification_id}.json.gz"

    def load(self, notification_id: str) -> Report | None:
        path = self.path(notification_id)
        if not path.exists():
            return None
        data = json.loads(gzip.decompress(path.read_bytes()))
        return Report(data["template"], data["currency"], [tuple(line) for line in data["lines"]])

    def save(self, notification_id: str, html_text: str) -> None:
        report = parse_report(html_text)
        data = {"template": report.template, "currency": report.currency, "lines": report.lines}
        path = self.path(notification_id)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8")))
        tmp.replace(path)

    def fetch(self, notification_id: str) -> None:
        body = self.http.get(self.url.format(id=notification_id), validate=lambda b: b"financial-table" in b)
        self.save(notification_id, body.decode("utf-8"))


# KAP's home page "Financial Statements" download without a company: every company's report
# for one quarter in one zip, named CODE_NotificationId_Year_Period.xls. KAP builds it while
# sending (~25 MB, 1-5 minutes per quarter), but it is one request instead of hundreds.
MARKET_ATTEMPTS = 5  # KAP often drops the connection after 3-5 minutes; a later try usually completes
CURL_TRANSFER_ERRORS = {7, 18, 28, 35, 52, 55, 56}  # connect, partial file, timeout, TLS, empty, send, receive
_MARKET_ENTRY = re.compile(r"_(\d+)_\d{4}_\d\.xls$")


def download_market(cache: ReportCache, url: str, year: int, quarter: int, proxy: str | None) -> int:
    """Cache every report in KAP's zip of one quarter; returns how many were new."""
    path = cache.root / "market" / f"{year}-Q{quarter}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["curl", "--location", "--silent", "--show-error", "--fail", "--user-agent", USER_AGENT,
               "--connect-timeout", "30", "--speed-limit", "100", "--speed-time", "600",  # stalled 10 min: give up
               "--output", str(path), url.format(year=year, quarter=quarter)]
    if proxy:
        command += ["--proxy", proxy, "--noproxy", ""]
    log.info("Downloading every company's %d Q%d reports in one file", year, quarter)
    started = time.monotonic()
    try:
        for attempt in range(1, MARKET_ATTEMPTS + 1):
            cache.http.throttle.wait()
            result = subprocess.run(command, capture_output=True)
            if result.returncode == 0:
                break
            error = result.stderr.decode("utf-8", "replace").strip()
            if result.returncode not in CURL_TRANSFER_ERRORS or attempt == MARKET_ATTEMPTS:
                log.warning("No market file for %d Q%d (%s); fetching its reports one by one", year, quarter, error)
                return 0
            log.warning("%d Q%d market file broke off (%s); retry %d/%d in %d min", year, quarter, error,
                        attempt, MARKET_ATTEMPTS - 1, 2 * attempt)
            time.sleep(120 * attempt)
        new = 0
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                m = _MARKET_ENTRY.search(name)
                if m and not cache.path(m.group(1)).exists():
                    cache.save(m.group(1), z.read(name).decode("utf-8"))
                    new += 1
        log.info("%d Q%d: %d reports in %.0f min (%d new)", year, quarter, len(z.namelist()),
                 (time.monotonic() - started) / 60, new)
        return new
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        log.warning("Market file for %d Q%d is unreadable (%s); fetching its reports one by one", year, quarter, exc)
        return 0
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
Key = tuple[int, int, int]


def row_key(r: dict) -> Key:
    return int(r["CompanyId"]), int(r["FiscalYear"]), int(r["FiscalQuarter"])


def chosen_notifications(raw_csv: Path) -> dict[Key, dict]:
    """The notification behind each table row, picked as `fundamentals` picks it:
    consolidated first, then the latest publish date, then the highest id."""
    best: dict[Key, tuple[tuple, dict]] = {}
    with raw_csv.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r["NotificationId"]:
                continue
            key = row_key(r)
            rank = (r["StatementNature"].lower().startswith("consolidated"), r["PublishDate"],
                    int(r["NotificationId"]))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, r)
    return {k: r for k, (_, r) in best.items()}


def download_markets(cache: ReportCache, wanted: list[dict], url: str, min_missing: int,
                     proxy: str | None) -> bool:
    """For each quarter with at least `min_missing` reports not cached yet, newest first, take
    KAP's one-file download of the whole quarter. Returns False if interrupted with Ctrl-C."""
    missing: dict[tuple[int, int], int] = {}
    for r in wanted:
        if not cache.path(r["NotificationId"]).exists():
            quarter = int(r["FiscalYear"]), int(r["FiscalQuarter"])
            missing[quarter] = missing.get(quarter, 0) + 1
    quarters = sorted((q for q, n in missing.items() if min_missing and n >= min_missing), reverse=True)
    if quarters:
        log.info("%d quarters have %d+ reports missing; downloading them as whole-market files first",
                 len(quarters), min_missing)
    try:
        for year, quarter in quarters:
            download_market(cache, url, year, quarter, proxy)
    except KeyboardInterrupt:
        log.warning("Interrupted - filling what was downloaded so far (re-run to resume)")
        return False
    return True


def download(cache: ReportCache, wanted: list[dict], cooldown: float, max_cooldowns: int) -> bool:
    """Download the reports not yet cached. Returns False if interrupted with Ctrl-C.

    When a report still fails after the client's retries, KAP is usually blocking us for a
    while: pause `cooldown` seconds and retry the same report. Stop after `max_cooldowns`
    pauses in a row did not help. Everything downloaded stays cached.
    """
    todo = [r for r in wanted if not cache.path(r["NotificationId"]).exists()]
    log.info("%d reports in the cache, %d to download", len(wanted) - len(todo), len(todo))
    started, cooldowns, index = time.monotonic(), 0, 0
    try:
        while index < len(todo):
            r = todo[index]
            try:
                cache.fetch(r["NotificationId"])
            except NotFound:
                log.warning("No report for %s %s Q%s (notification %s)", r["StockCode"], r["FiscalYear"],
                            r["FiscalQuarter"], r["NotificationId"])
            except (FetchError, UnicodeDecodeError) as exc:
                if cooldowns >= max_cooldowns:
                    log.error("Still failing after %d cool-downs (%s); stopping. Re-run later to resume.",
                              cooldowns, exc)
                    break
                cooldowns += 1
                log.warning("%s. Cooling down for %.0f min (%d/%d) before retrying.",
                            exc, cooldown / 60, cooldowns, max_cooldowns)
                cache.http.close()
                time.sleep(cooldown)
                continue
            cooldowns = 0
            index += 1
            if index % 100 == 0 or index == len(todo):
                elapsed = time.monotonic() - started
                log.info("Reports %d/%d (%s %s Q%s), %.0f min elapsed, ~%.0f min left, request gap %.1fs",
                         index, len(todo), r["StockCode"], r["FiscalYear"], r["FiscalQuarter"], elapsed / 60,
                         elapsed / index * (len(todo) - index) / 60, cache.http.throttle.interval)
    except KeyboardInterrupt:
        log.warning("Interrupted - filling what was downloaded so far (re-run to resume)")
        return False
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m stock_crawler fundamental_reports",
                                description="Fill Ebitda, debt, cash, free cash flow, EPS and share count "
                                            "in the fundamentals CSV from KAP's full financial reports.")
    p.add_argument("--years", type=int, default=5,
                   help="download reports for fiscal years back from the current one (default: %(default)s); "
                        "rows of older years are still filled from the cache")
    p.add_argument("--symbols", help="comma separated stock codes to restrict downloads to (e.g. ASELS,THYAO)")
    p.add_argument("--report-url", default=REPORT_URL, help="KAP report export, {id} = NotificationId "
                                                            "(default: %(default)s)")
    p.add_argument("--market-url", default=MARKET_URL,
                   help="KAP's whole-quarter download, {year} and {quarter} (default: %(default)s)")
    p.add_argument("--market-min-missing", type=int, default=50,
                   help="use the whole-quarter file when at least this many of a quarter's reports are "
                        "missing; fewer are downloaded one by one (default: %(default)s, 0 = never)")
    p.add_argument("--fundamentals-dir", type=Path, default=Path("output/fundamentals"),
                   help="where `fundamentals` wrote its CSVs; company_fundamental.csv is updated in place "
                        "(default: %(default)s)")
    p.add_argument("--out-dir", type=Path, default=Path("output/fundamental_reports"),
                   help="report_lines.csv: the report lines behind each value (default: %(default)s)")
    p.add_argument("--cache-dir", type=Path, default=Path("cache/fundamental_reports"), help="(default: %(default)s)")
    p.add_argument("--request-interval", type=float, default=30.0,
                   help="normal seconds between requests, +/-30%% jitter; KAP allows about 10 reports per "
                        "5 minutes (default: %(default)s)")
    p.add_argument("--max-request-interval", type=float, default=120.0,
                   help="upper limit when slowing down after push-back from KAP (default: %(default)s)")
    p.add_argument("--cooldown-minutes", type=float, default=10.0,
                   help="pause when KAP keeps refusing requests, then retry (default: %(default)s)")
    p.add_argument("--max-cooldowns", type=int, default=3,
                   help="stop after this many cool-downs in a row without success (default: %(default)s)")
    p.add_argument("--proxy", help="HTTP or SOCKS5 proxy for all requests, e.g. http://127.0.0.1:12334")
    p.add_argument("--ca-bundle", help="PEM file of trusted CAs (e.g. a corporate/VPN root)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.years < 1:
        p.error("--years must be at least 1")
    if "{id}" not in args.report_url:
        p.error("--report-url must contain {id}")
    if "{year}" not in args.market_url or "{quarter}" not in args.market_url:
        p.error("--market-url must contain {year} and {quarter}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    main_csv = args.fundamentals_dir / "company_fundamental.csv"
    raw_csv = args.fundamentals_dir / "company_fundamental_raw.csv"
    if not main_csv.exists() or not raw_csv.exists():
        log.error("%s and %s are needed; run `python -m stock_crawler fundamentals` first", main_csv, raw_csv)
        return 2

    with main_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    notifications = chosen_notifications(raw_csv)
    first_year = max(dt.date.today().year - args.years, FIRST_YEAR)
    symbols = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None

    # Newest first, so a stopped run already has the recent years.
    wanted = sorted(((row_key(r), notifications[row_key(r)]) for r in rows if row_key(r) in notifications),
                    key=lambda kr: (-kr[0][1], -kr[0][2], kr[0][0]))
    in_scope = [(k, r) for k, r in wanted if k[1] >= first_year
                and (not symbols or symbols & {s.strip() for s in r["StockCode"].split(",")})]
    log.info("%d rows in %s, %d with a notification, %d in scope (FY %d+%s)", len(rows), main_csv, len(wanted),
             len(in_scope), first_year, f", {args.symbols}" if symbols else "")

    http = HttpClient("auto", args.proxy, args.request_interval, max_interval=args.max_request_interval,
                      ca_bundle=args.ca_bundle)
    cache = ReportCache(http, args.cache_dir, args.report_url)
    try:
        wanted_rows = [r for _, r in in_scope]
        completed = (download_markets(cache, wanted_rows, args.market_url, args.market_min_missing, args.proxy)
                     and download(cache, wanted_rows, args.cooldown_minutes * 60, args.max_cooldowns))
    finally:
        http.close()
    missing = sum(not cache.path(r["NotificationId"]).exists() for _, r in in_scope)

    # Fill every row whose report is cached, in scope or not.
    for column in COLUMNS:
        if column not in header:
            header.append(column)
    by_key = {row_key(r): r for r in rows}
    lines_out, filled, unknown_templates = [], {c: 0 for c in COLUMNS}, set()
    for key, n in wanted:
        report = cache.load(n["NotificationId"])
        if report is None:
            continue
        row = by_key[key]
        if report.template not in FIELDS:
            unknown_templates.add(report.template)
            continue
        if report.currency != row.get("PresentationCurrency", report.currency):
            log.warning("%s %s Q%s: report in %s but the row in %s - skipped", n["StockCode"], key[1], key[2],
                        report.currency, row.get("PresentationCurrency"))
            continue
        values, used = columns_from(report, to_decimal(row["NetIncome"]) if row.get("NetIncome") else None)
        for column, value in values.items():
            row[column] = fmt(value)
        for column in COLUMNS:
            filled[column] += bool(row.get(column))
        lines_out += [{"CompanyId": key[0], "StockCode": n["StockCode"], "FiscalYear": key[1],
                       "FiscalQuarter": key[2], "NotificationId": n["NotificationId"],
                       "PresentationCurrency": report.currency, "Column": column, "Statement": part,
                       "Line": label, "Value": value}
                      for column, part, label, value in used]
    if unknown_templates:
        log.warning("No column mapping for statement template(s) %s; add them to FIELDS",
                    ", ".join(sorted(unknown_templates)))

    write_csv(main_csv, header, rows)
    write_csv(args.out_dir / "report_lines.csv",
              ["CompanyId", "StockCode", "FiscalYear", "FiscalQuarter", "NotificationId", "PresentationCurrency",
               "Column", "Statement", "Line", "Value"], lines_out, encoding="utf-8-sig")
    log.info("Filled in %s: %s", main_csv, ", ".join(f"{c} {n}/{len(rows)}" for c, n in filled.items()))
    if not completed:
        return 130
    if missing:
        log.warning("%d reports in scope are not downloaded yet; re-run to continue", missing)
        return 3
    return 0
