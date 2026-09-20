"""Source-to-canonical mapping with reporting-context, scope, and unit validation.

Pipeline: raw bytes -> `StatementFacts` (an intermediate representation: header values,
period contexts, and labelled facts with section ancestry) -> `ParsedReport`.
Only `extract_facts_from_html` depends on the source layout; everything after it is
layout-independent and fixture-tested.

Label dictionaries below follow the KAP Turkish taxonomy and KAP's English rendering. They
are reviewed mappings, not fuzzy matches: a label must match exactly after normalisation,
and ambiguous matches are rejected rather than collapsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from lxml import etree, html

from . import metrics
from .models import (
    ANNUAL_PERIOD,
    ConsolidationScope,
    FundamentalRecord,
    ParsedReport,
    ParseStatus,
)
from .units import UnitError, decode_presentation_currency, is_twelve_month_span, parse_dmy_date, parse_turkish_number

PARSER_VERSION = "1.1.1"

StatementKind = Literal["balance_sheet", "income_statement", "cash_flow", "notes", "other"]
Unit = Literal["monetary", "per_share", "shares"]

_DATE_RE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}")
_WS_RE = re.compile(r"\s+")
_TRAILING_RE = re.compile(r"(\s*\(-\)|\s*\*+|\s*:)+$")
_TR_MAP = str.maketrans({"İ": "i", "I": "ı", "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u"})


def normalize_label(text: str) -> str:
    """Turkish-aware lowercase, whitespace collapse, trailing `(-)`/`*` removal."""
    cleaned = text.translate(_TR_MAP).lower().replace(" ", " ")
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    cleaned = _TRAILING_RE.sub("", cleaned).strip()
    return cleaned


# -- intermediate representation ------------------------------------------------------------


@dataclass(frozen=True)
class PeriodContext:
    context_id: str
    kind: Literal["instant", "duration"]
    start: date | None
    end: date
    label: str
    is_current: bool


@dataclass(frozen=True)
class Fact:
    statement: StatementKind
    label: str
    label_key: str
    section_path: tuple[str, ...]
    code: str | None
    context_id: str
    raw_value: str
    value: Decimal | None


@dataclass
class StatementFacts:
    header: dict[str, str] = field(default_factory=dict)
    contexts: dict[str, PeriodContext] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    statements_found: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


# -- HTML extraction (layout dependent) -----------------------------------------------------

_HEADER_KEYS = {
    "finansal tablo niteliği": "consolidation",
    "nature of financial statement": "consolidation",
    "sunum para birimi": "presentation_currency",
    "presentation currency": "presentation_currency",
    "finansal tablo türü": "statement_type",
    "sectoral statement type": "statement_type",
    "financial statement type": "statement_type",
    "dönem tipi": "period_type",
    "dönem": "period_type",
    "period": "period_type",
    "period type": "period_type",
    "yıl": "fiscal_year",
    "year": "fiscal_year",
}
_STATEMENT_KEYWORDS: tuple[tuple[str, StatementKind], ...] = (
    ("finansal durum tablosu", "balance_sheet"),
    ("bilanço", "balance_sheet"),
    ("balance sheet", "balance_sheet"),
    ("financial position", "balance_sheet"),
    ("kar veya zarar", "income_statement"),
    ("gelir tablosu", "income_statement"),
    ("profit or loss", "income_statement"),
    ("income statement", "income_statement"),
    ("nakit akış", "cash_flow"),
    ("cash flow", "cash_flow"),
    ("dipnot", "notes"),
    ("notes", "notes"),
)


def _cell_text(cell: etree._Element) -> str:
    return _WS_RE.sub(" ", " ".join(cell.itertext())).strip()


def _statement_kind(table: etree._Element) -> StatementKind:
    explicit = table.get("data-statement")
    if explicit in ("balance_sheet", "income_statement", "cash_flow", "notes", "other"):
        return explicit  # type: ignore[return-value]
    texts: list[str] = []
    caption = table.find("caption")
    if caption is not None:
        texts.append(_cell_text(caption))
    for ancestor in table.iterancestors():
        if ancestor.get("data-statement") in ("balance_sheet", "income_statement", "cash_flow", "notes", "other"):
            return ancestor.get("data-statement")  # type: ignore[return-value]
    for heading in table.itersiblings(preceding=True):
        if heading.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            texts.append(_cell_text(heading))
            break
    parent = table.getparent()
    while parent is not None and not texts:
        for heading in parent.itersiblings(preceding=True):
            if heading.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                texts.append(_cell_text(heading))
                break
        parent = parent.getparent()
    for text in texts:
        key = normalize_label(text)
        for keyword, kind in _STATEMENT_KEYWORDS:
            if keyword in key:
                return kind
    return "other"


def _period_context(text: str) -> PeriodContext | None:
    dates = _DATE_RE.findall(text)
    if not dates:
        return None
    try:
        parsed = [parse_dmy_date(item) for item in dates]
    except UnitError:
        return None
    key = normalize_label(text)
    is_current = "cari" in key or "current" in key
    if len(parsed) >= 2:
        start, end = parsed[0], parsed[-1]
        return PeriodContext(f"duration:{start.isoformat()}:{end.isoformat()}", "duration", start, end, text, is_current)
    end = parsed[0]
    return PeriodContext(f"instant:{end.isoformat()}", "instant", None, end, text, is_current)


def _row_level(row: etree._Element, first_cell: etree._Element) -> int:
    for element in (row, first_cell):
        value = element.get("data-level")
        if value is not None and value.isdigit():
            return int(value)
        for cls in (element.get("class") or "").split():
            if cls.startswith("level-") and cls[6:].isdigit():
                return int(cls[6:])
    return 0


def extract_facts_from_html(data: bytes) -> StatementFacts:
    """Extract header, contexts, and facts from a KAP-style financial statement page."""
    result = StatementFacts()
    try:
        root = html.fromstring(data)
    except (etree.ParserError, ValueError) as exc:
        result.warnings.append(f"html parse error: {exc}")
        return result
    for br in root.iter("br"):
        br.tail = " " + (br.tail or "")

    for table in root.iter("table"):
        rows = [row for row in table.iter("tr")]
        if not rows:
            continue
        # Header table: two-cell rows whose first cell is a known header key.
        two_cell_rows = [(row, [c for c in row if c.tag in ("td", "th")]) for row in rows]
        if all(len(cells) == 2 for _, cells in two_cell_rows):
            matched = 0
            for _, cells in two_cell_rows:
                key = _HEADER_KEYS.get(normalize_label(_cell_text(cells[0])))
                if key:
                    matched += 1
                    result.header.setdefault(key, _cell_text(cells[1]))
            if matched:
                continue

        # Statement table: a header row with at least one dated period column.
        columns: dict[int, str] = {}
        header_index = -1
        for index, row in enumerate(rows):
            cells = [c for c in row if c.tag in ("td", "th")]
            found = {i: _period_context(_cell_text(c)) for i, c in enumerate(cells) if i > 0}
            found = {i: ctx for i, ctx in found.items() if ctx is not None}
            if found:
                for i, ctx in found.items():
                    result.contexts.setdefault(ctx.context_id, ctx)
                    columns[i] = ctx.context_id
                header_index = index
                break
        if not columns:
            continue
        kind = _statement_kind(table)
        result.statements_found.add(kind)
        stack: list[tuple[int, str]] = []
        for row in rows[header_index + 1 :]:
            cells = [c for c in row if c.tag in ("td", "th")]
            if not cells:
                continue
            label = _cell_text(cells[0])
            if not label:
                continue
            level = _row_level(row, cells[0])
            while stack and stack[-1][0] >= level:
                stack.pop()
            section_path = tuple(key for _, key in stack)
            label_key = normalize_label(label)
            stack.append((level, label_key))
            code = row.get("data-code") or cells[0].get("data-code")
            for column_index, context_id in columns.items():
                if column_index >= len(cells):
                    continue
                raw_value = _cell_text(cells[column_index])
                if not raw_value:
                    continue
                try:
                    value = parse_turkish_number(raw_value)
                except UnitError as exc:
                    result.warnings.append(f"{kind}: {label!r} [{context_id}] unparseable value {raw_value!r}: {exc}")
                    continue
                result.facts.append(Fact(kind, label, label_key, section_path, code, context_id, raw_value, value))
    return result


# -- concept dictionary (layout independent) ------------------------------------------------


@dataclass(frozen=True)
class ConceptSpec:
    name: str
    statement: StatementKind | None
    labels: tuple[str, ...]
    unit: Unit = "monetary"
    within: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    required: bool = False


_SECTION_CURRENT = ("kısa vadeli yükümlülükler", "current liabilities")
_SECTION_NON_CURRENT = ("uzun vadeli yükümlülükler", "non-current liabilities")
_SECTION_PROFIT_SPLIT = (
    "dönem karının (zararının) dağılımı",
    "dönem karı (zararı) dağılımı",
    "dönem karının dağılımı",
    "profit (loss) attributable to",
    "profit attributable to",
)
BORROWING_SUBTOTAL_KEYS = (
    "kısa vadeli borçlanmalar",
    "uzun vadeli borçlanmaların kısa vadeli kısımları",
    "uzun vadeli borçlanmalar",
    "short-term borrowings",
    "short term borrowings",
    "current portion of long-term borrowings",
    "long-term borrowings",
    "long term borrowings",
)

CONCEPTS: tuple[ConceptSpec, ...] = (
    ConceptSpec("total_assets", "balance_sheet", ("toplam varlıklar", "total assets"), required=True),
    ConceptSpec(
        "total_liabilities_and_equity",
        "balance_sheet",
        ("toplam kaynaklar", "total liabilities and equity", "total equity and liabilities"),
        required=True,
    ),
    ConceptSpec("current_liabilities", "balance_sheet", ("kısa vadeli yükümlülükler", "current liabilities", "total current liabilities"), required=True),
    ConceptSpec("non_current_liabilities", "balance_sheet", ("uzun vadeli yükümlülükler", "non-current liabilities", "total non-current liabilities"), required=True),
    ConceptSpec("total_equity", "balance_sheet", ("toplam özkaynaklar", "özkaynaklar", "total equity", "equity"), required=True),
    ConceptSpec("cash_and_cash_equivalents", "balance_sheet", ("nakit ve nakit benzerleri", "cash and cash equivalents")),
    ConceptSpec("short_term_borrowings", "balance_sheet", ("kısa vadeli borçlanmalar", "short-term borrowings", "short term borrowings"), within=_SECTION_CURRENT),
    ConceptSpec(
        "current_portion_of_long_term_borrowings",
        "balance_sheet",
        ("uzun vadeli borçlanmaların kısa vadeli kısımları", "current portion of long-term borrowings"),
        within=_SECTION_CURRENT,
    ),
    ConceptSpec("long_term_borrowings", "balance_sheet", ("uzun vadeli borçlanmalar", "long-term borrowings", "long term borrowings"), within=_SECTION_NON_CURRENT),
    ConceptSpec("lease_liabilities", "balance_sheet", ("kiralama işlemlerinden borçlar", "lease liabilities")),
    ConceptSpec("issued_capital", "balance_sheet", ("ödenmiş sermaye", "issued capital", "paid-in capital")),
    ConceptSpec("revenue", "income_statement", ("hasılat", "revenue"), required=True),
    ConceptSpec("finance_sector_revenue", "income_statement", ("finans sektörü faaliyetleri hasılatı", "revenue from finance sector operations")),
    ConceptSpec(
        "operating_income",
        "income_statement",
        ("esas faaliyet karı (zararı)", "esas faaliyet karı/zararı", "profit (loss) from operating activities", "operating profit (loss)"),
    ),
    ConceptSpec(
        "net_profit",
        "income_statement",
        ("dönem karı (zararı)", "dönem karı/zararı", "net profit (loss)", "profit (loss) for the period", "profit (loss)"),
        required=True,
    ),
    ConceptSpec(
        "profit_attributable_to_owners_of_parent",
        "income_statement",
        ("ana ortaklık payları", "owners of parent", "attributable to owners of parent"),
        within=_SECTION_PROFIT_SPLIT,
    ),
    ConceptSpec(
        "profit_attributable_to_non_controlling_interests",
        "income_statement",
        ("kontrol gücü olmayan paylar", "non-controlling interests"),
        within=_SECTION_PROFIT_SPLIT,
    ),
    ConceptSpec(
        "eps",
        "income_statement",
        ("pay başına kazanç", "earnings per share", "basic earnings per share", "sürdürülen faaliyetlerden pay başına kazanç", "earnings per share from continuing operations"),
        unit="per_share",
        exclude_terms=("sulandırılmış", "diluted"),
    ),
    ConceptSpec("reported_ebitda", "income_statement", ("favök", "ebitda")),
    ConceptSpec(
        "operating_cash_flow",
        "cash_flow",
        ("işletme faaliyetlerinden nakit akışları", "cash flows from operating activities", "net cash flows from operating activities"),
    ),
    ConceptSpec(
        "depreciation_and_amortisation",
        "cash_flow",
        ("amortisman ve itfa gideri ile ilgili düzeltmeler", "adjustments for depreciation and amortisation expense"),
    ),
    ConceptSpec(
        "ppe_purchases",
        "cash_flow",
        ("maddi duran varlık alımından kaynaklanan nakit çıkışları", "purchase of property, plant and equipment"),
    ),
    ConceptSpec(
        "intangible_purchases",
        "cash_flow",
        ("maddi olmayan duran varlık alımından kaynaklanan nakit çıkışları", "purchase of intangible assets"),
    ),
    ConceptSpec(
        "ppe_and_intangible_purchases",
        "cash_flow",
        ("maddi ve maddi olmayan duran varlıkların alımından kaynaklanan nakit çıkışları", "purchase of property, plant, equipment and intangible assets"),
    ),
    ConceptSpec("shares_outstanding_direct", None, ("dolaşımdaki pay adedi", "number of shares outstanding", "shares outstanding"), unit="shares"),
    ConceptSpec("nominal_value_per_share", None, ("pay başına nominal değer", "nominal value per share"), unit="per_share"),
    ConceptSpec("treasury_share_count", None, ("geri alınmış pay adedi", "number of treasury shares"), unit="shares"),
)
CONCEPT_BY_NAME = {spec.name: spec for spec in CONCEPTS}

_PERIOD_TYPE_ANNUAL = ("yıllık", "annual", "12 aylık", "yearly")
_PERIOD_TYPE_INTERIM = ("3 aylık", "6 aylık", "9 aylık", "ara dönem", "quarter", "interim")
_SCOPE_MAP = {
    "konsolide": ConsolidationScope.CONSOLIDATED,
    "consolidated": ConsolidationScope.CONSOLIDATED,
    "konsolide olmayan": ConsolidationScope.UNCONSOLIDATED,
    "unconsolidated": ConsolidationScope.UNCONSOLIDATED,
    "solo": ConsolidationScope.UNCONSOLIDATED,
}
_STATEMENT_TYPE_MAP = {
    "genel": "general",
    "general": "general",
    "banka": "banks",
    "bankalar": "banks",
    "banks": "banks",
    "sigorta": "insurance",
    "insurance": "insurance",
    "finans": "finance",
    "finance": "finance",
    "holding": "holding",
}
SUPPORTED_STATEMENT_TYPES = frozenset({"general"})


@dataclass
class _Match:
    fact: Fact
    spec: ConceptSpec


class _Resolver:
    """Finds exactly one fact per (concept, context) or reports why it could not."""

    def __init__(self, facts: StatementFacts) -> None:
        self.facts = facts
        self.warnings: list[str] = []

    def candidates(self, spec: ConceptSpec, context_id: str) -> list[Fact]:
        found: list[Fact] = []
        for fact in self.facts.facts:
            if fact.context_id != context_id or fact.label_key not in spec.labels:
                continue
            if spec.statement is not None and fact.statement != spec.statement:
                continue
            haystack = " ".join((fact.label_key, *fact.section_path))
            if any(term in haystack for term in spec.exclude_terms):
                continue
            if spec.within and not any(any(w == part for w in spec.within) for part in fact.section_path):
                # Accept a lone match without ancestry information; reject if others exist.
                if fact.section_path:
                    continue
            found.append(fact)
        if len(found) > 1:
            # Prefer the earliest listed label (priority order) when they differ.
            best_rank = min(spec.labels.index(f.label_key) for f in found)
            ranked = [f for f in found if spec.labels.index(f.label_key) == best_rank]
            if len(ranked) == 1:
                return ranked
            return found
        return found

    def resolve(self, name: str, context_id: str) -> tuple[Fact | None, str | None]:
        spec = CONCEPT_BY_NAME[name]
        found = self.candidates(spec, context_id)
        if not found:
            return None, "not present"
        if len(found) > 1:
            labels = sorted({f"{f.label} <{'/'.join(f.section_path) or '-'}>" for f in found})
            return None, f"ambiguous: {len(found)} matches {labels}"
        return found[0], None


def _context_for(facts: StatementFacts, kind: Literal["instant", "duration"], end: date) -> PeriodContext | None:
    matches = [ctx for ctx in facts.contexts.values() if ctx.kind == kind and ctx.end == end]
    if len(matches) == 1:
        return matches[0]
    return None


def _decode_header(facts: StatementFacts, report: ParsedReport) -> tuple[str, int, str] | None:
    raw_currency = facts.header.get("presentation_currency")
    try:
        code, scale = decode_presentation_currency(raw_currency)
    except UnitError as exc:
        report.errors.append(str(exc))
        return None
    return code, scale, raw_currency or ""


def build_report(facts: StatementFacts, *, parser_version: str = PARSER_VERSION) -> ParsedReport:
    """Map extracted facts to canonical periods, validate, and derive metrics."""
    report = ParsedReport(parser_version=parser_version, parse_status=ParseStatus.FAILED)
    report.warnings.extend(facts.warnings)

    scope_key = normalize_label(facts.header.get("consolidation", ""))
    report.consolidation_scope = _SCOPE_MAP.get(scope_key)
    if report.consolidation_scope is None:
        report.errors.append(f"consolidation scope unknown: {facts.header.get('consolidation')!r}")

    type_key = normalize_label(facts.header.get("statement_type", "general"))
    report.statement_type = _STATEMENT_TYPE_MAP.get(type_key, type_key or "unknown")
    if report.statement_type not in SUPPORTED_STATEMENT_TYPES:
        report.errors.append(f"statement family {report.statement_type!r} has no reviewed mapping")
        report.parse_status = ParseStatus.UNSUPPORTED
        return report

    period_type = normalize_label(facts.header.get("period_type", ""))
    if period_type and any(term in period_type for term in _PERIOD_TYPE_INTERIM) and not any(
        term in period_type for term in _PERIOD_TYPE_ANNUAL
    ):
        report.errors.append(f"interim period {facts.header.get('period_type')!r}; only annual statements are supported")
        report.parse_status = ParseStatus.UNSUPPORTED
        return report

    for required_statement in ("balance_sheet", "income_statement"):
        if required_statement not in facts.statements_found:
            report.errors.append(f"{required_statement} table not found")
    if "cash_flow" not in facts.statements_found:
        report.warnings.append("cash_flow table not found; cash-flow derived metrics unavailable")

    units = _decode_header(facts, report)
    if report.errors:
        return report
    currency_code, scale, raw_currency = units  # type: ignore[misc]

    durations = [ctx for ctx in facts.contexts.values() if ctx.kind == "duration"]
    if not durations:
        report.errors.append("no duration reporting context found in income statement headers")
        return report
    current = [ctx for ctx in durations if ctx.is_current] or [max(durations, key=lambda c: c.end)]
    current_ctx = max(current, key=lambda c: c.end)
    if not is_twelve_month_span(current_ctx.start, current_ctx.end):
        report.errors.append(f"current period {current_ctx.start}..{current_ctx.end} is not a twelve-month span")
        report.parse_status = ParseStatus.UNSUPPORTED
        return report
    report.filing_period_start_date = current_ctx.start
    report.filing_period_end_date = current_ctx.end
    report.filing_fiscal_year = current_ctx.end.year
    header_year = facts.header.get("fiscal_year")
    if header_year and header_year.strip().isdigit() and int(header_year) != report.filing_fiscal_year:
        report.warnings.append(f"header year {header_year} differs from current period end year {report.filing_fiscal_year}")

    resolver = _Resolver(facts)
    tolerance = Decimal(scale) * 3
    comparatives = sorted((ctx for ctx in durations if ctx is not current_ctx), key=lambda c: c.end, reverse=True)
    for ctx in [current_ctx, *comparatives]:
        is_comparative = ctx is not current_ctx
        if is_comparative and not is_twelve_month_span(ctx.start, ctx.end):
            report.warnings.append(f"comparative context {ctx.context_id} is not a twelve-month span; skipped")
            continue
        instant = _context_for(facts, "instant", ctx.end)
        if instant is None:
            message = f"no unique balance-sheet context for period end {ctx.end}"
            if is_comparative:
                report.warnings.append(message + "; comparative skipped")
                continue
            report.errors.append(message)
            return report
        record, sources, derivations, problems = _build_period(
            resolver, instant, ctx, currency_code=currency_code, scale=scale, raw_currency=raw_currency,
            is_comparative=is_comparative, measuring_unit_date=current_ctx.end,
        )
        key = f"{record.fiscal_year}:{'comparative' if is_comparative else 'current'}"
        report.field_sources[key] = sources
        report.derivations[key] = derivations
        if problems and not is_comparative:
            report.errors.extend(problems)
            return report
        if problems:
            report.warnings.extend(f"comparative {record.fiscal_year}: {p}" for p in problems)
            continue
        for finding in metrics.identity_checks(record.financial_values(), tolerance=tolerance):
            report.warnings.append(f"{key} identity check: {finding}")
        report.periods.append(record)

    report.warnings.extend(resolver.warnings)
    instants_unused = {ctx.end for ctx in facts.contexts.values() if ctx.kind == "instant"} - {r.period_end_date for r in report.periods}
    for end in sorted(instants_unused):
        report.warnings.append(f"balance-sheet context {end} has no matching twelve-month income statement context; not stored")

    report.warnings = list(dict.fromkeys(report.warnings))
    report.parse_status = ParseStatus.VALID
    return report


def _build_period(
    resolver: _Resolver,
    instant: PeriodContext,
    duration: PeriodContext,
    *,
    currency_code: str,
    scale: int,
    raw_currency: str,
    is_comparative: bool,
    measuring_unit_date: date | None = None,
) -> tuple[FundamentalRecord, dict[str, Any], dict[str, Any], list[str]]:
    sources: dict[str, Any] = {}
    problems: list[str] = []
    values: dict[str, Decimal | None] = {}

    def pull(name: str) -> Decimal | None:
        spec = CONCEPT_BY_NAME[name]
        if spec.statement == "balance_sheet":
            context = instant
        elif spec.statement in ("income_statement", "cash_flow"):
            context = duration
        else:
            fact, reason = resolver.resolve(name, instant.context_id)
            if fact is None:
                fact, reason = resolver.resolve(name, duration.context_id)
            return _record(name, spec, fact, reason)
        fact, reason = resolver.resolve(name, context.context_id)
        return _record(name, spec, fact, reason)

    def _record(name: str, spec: ConceptSpec, fact: Fact | None, reason: str | None) -> Decimal | None:
        if fact is None:
            sources[name] = {"status": reason}
            if reason and reason.startswith("ambiguous"):
                if spec.required:
                    problems.append(f"{name}: {reason}")
                else:
                    resolver.warnings.append(f"{name}: {reason}; left NULL")
            elif spec.required:
                problems.append(f"required concept {name} not present")
            return None
        value = fact.value
        scaled_by = 1
        if spec.unit == "monetary" and value is not None:
            value = value * scale
            scaled_by = scale
        sources[name] = {
            "label": fact.label,
            "code": fact.code,
            "context": fact.context_id,
            "section_path": list(fact.section_path),
            "raw_value": fact.raw_value,
            "unit": spec.unit,
            "scaled_by": scaled_by,
        }
        return value

    for name in (
        "total_assets",
        "total_liabilities_and_equity",
        "current_liabilities",
        "non_current_liabilities",
        "total_equity",
        "cash_and_cash_equivalents",
        "revenue",
        "finance_sector_revenue",
        "operating_income",
        "net_profit",
        "profit_attributable_to_owners_of_parent",
        "profit_attributable_to_non_controlling_interests",
        "eps",
    ):
        values[name] = pull(name)

    derivations: dict[str, Any] = {}

    # total_debt: borrowings subtotals; leases counted once (see _lease_components).
    short_term = pull("short_term_borrowings")
    current_portion = pull("current_portion_of_long_term_borrowings")
    long_term = pull("long_term_borrowings")
    lease_current, lease_non_current, lease_reported, lease_problem = _lease_components(resolver, instant, scale)
    if lease_problem:
        debt = metrics.MetricResult(None, "missing", lease_problem)
    else:
        debt = metrics.total_debt(
            metrics.DebtComponents(
                short_term_borrowings=short_term,
                current_portion_of_long_term_borrowings=current_portion,
                long_term_borrowings=long_term,
                short_term_lease_liabilities=lease_current,
                long_term_lease_liabilities=lease_non_current,
                lease_lines_reported=lease_reported,
            )
        )
    derivations["total_debt"] = {**debt.as_dict(), "lease_policy": "leases inside borrowing subtotals are counted once; leases outside are added"}

    ebitda = metrics.ebitda(pull("reported_ebitda"), values["operating_income"], pull("depreciation_and_amortisation"), da_deducted_in_operating_income=False)
    if ebitda.method == "operating_income_plus_da":
        resolver.warnings.append("ebitda: D&A taken from cash-flow adjustments and assumed fully within operating income")
    derivations["ebitda"] = ebitda.as_dict()

    fcf = metrics.free_cash_flow(pull("operating_cash_flow"), pull("ppe_purchases"), pull("intangible_purchases"), pull("ppe_and_intangible_purchases"))
    derivations["free_cash_flow"] = fcf.as_dict()

    treasury_count = pull("treasury_share_count")
    shares = metrics.shares_outstanding(
        pull("shares_outstanding_direct"),
        pull("issued_capital"),
        pull("nominal_value_per_share"),
        treasury_count,
        treasury_shares_known=treasury_count is not None,
    )
    derivations["shares_outstanding"] = shares.as_dict()

    record = FundamentalRecord(
        fiscal_year=duration.end.year,
        fiscal_period=ANNUAL_PERIOD,
        period_start_date=duration.start,
        period_end_date=duration.end,
        is_comparative=is_comparative,
        measuring_unit_date=measuring_unit_date,
        currency_code=currency_code,
        currency_scale=scale,
        presentation_currency_raw=raw_currency,
        total_debt=debt.value,
        total_debt_method=debt.method,
        ebitda=ebitda.value,
        ebitda_method=ebitda.method,
        free_cash_flow=fcf.value,
        free_cash_flow_method=fcf.method,
        shares_outstanding=shares.value,
        shares_outstanding_method=shares.method,
        **values,
    )
    return record, sources, derivations, problems


def _lease_components(resolver: _Resolver, instant: PeriodContext, scale: int) -> tuple[Decimal | None, Decimal | None, bool, str | None]:
    """Classify lease-liability lines by ancestry: inside a borrowing subtotal (already
    counted) or standalone under current / non-current liabilities (must be added)."""
    spec = CONCEPT_BY_NAME["lease_liabilities"]
    lease_facts = [f for f in resolver.facts.facts if f.context_id == instant.context_id and f.label_key in spec.labels and f.statement == "balance_sheet"]
    if not lease_facts:
        return None, None, False, None
    current_total: Decimal | None = None
    non_current_total: Decimal | None = None
    for fact in lease_facts:
        if not fact.section_path:
            return None, None, True, "lease liabilities present but section ancestry unavailable; cannot tell whether they sit inside borrowing subtotals"
        if any(part in BORROWING_SUBTOTAL_KEYS for part in fact.section_path):
            continue  # inside a borrowing subtotal: already included exactly once
        if fact.value is None:
            continue
        amount = fact.value * scale
        if any(part in _SECTION_CURRENT for part in fact.section_path):
            current_total = (current_total or Decimal(0)) + amount
        elif any(part in _SECTION_NON_CURRENT for part in fact.section_path):
            non_current_total = (non_current_total or Decimal(0)) + amount
        else:
            return None, None, True, f"lease liability line {fact.label!r} has unrecognised ancestry {fact.section_path}"
    standalone = current_total is not None or non_current_total is not None
    if standalone:
        # Both liability sections were parsed (they are required concepts), so a bucket with
        # no standalone lease line is a verified absence rather than an unknown.
        return current_total or Decimal(0), non_current_total or Decimal(0), True, None
    return None, None, False, None


def parse_snapshot(files: dict[str, bytes], manifest: dict[str, Any], *, parser_version: str = PARSER_VERSION) -> ParsedReport:
    """Parse a stored snapshot. The manifest names the primary file and expected scope."""
    if manifest.get("data_product") == "kap_compare":
        import json
        from .kap_export import read_export, parse_export_row
        from .kap import SourceError
        try:
            original = json.loads(files["source.json"])
            matches = [r for r in read_export(files["source.xlsx"]) if str(r["Notification ID"]) == str(manifest["notification_id"]) and r["Company"] == original["Company"]]
            if len(matches) != 1:
                raise SourceError("native export must contain exactly one matching company/notification row")
            return parse_export_row(matches[0], calendar_year_confirmed=bool(manifest.get("calendar_year_confirmed")), parser_version=parser_version)
        except (SourceError, ValueError, KeyError) as exc:
            return ParsedReport(parser_version=parser_version, parse_status=ParseStatus.FAILED, errors=[str(exc)])
    primary = manifest.get("primary_file") or next((name for name in sorted(files) if name.endswith(".html")), None)
    if primary is None or primary not in files:
        report = ParsedReport(parser_version=parser_version, parse_status=ParseStatus.FAILED)
        report.errors.append(f"primary file {primary!r} not present in snapshot")
        return report
    facts = extract_facts_from_html(files[primary])
    report = build_report(facts, parser_version=parser_version)
    for field, actual in (("fiscal_year", report.filing_fiscal_year), ("period_end_date", report.filing_period_end_date)):
        if manifest.get(field) is not None and actual is not None and str(manifest[field]) != str(actual):
            report.errors.append(f"{field} mismatch: listing said {manifest[field]}, statement says {actual}")
            report.parse_status = ParseStatus.FAILED
    expected_scope = manifest.get("consolidation_scope")
    if expected_scope and report.consolidation_scope and report.consolidation_scope.value != expected_scope:
        report.errors.append(f"scope mismatch: listing said {expected_scope}, statement says {report.consolidation_scope.value}")
        report.parse_status = ParseStatus.FAILED
    return report
