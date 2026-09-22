"""Normalized comparison of stored fundamentals between two report versions (section 11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..core.models import FINANCIAL_FIELDS, METHOD_FIELDS

ROW_KEY_FIELDS = ("fiscal_year", "fiscal_period", "is_comparative")


@dataclass(frozen=True)
class FieldDiff:
    field: str
    before: Decimal | str | None
    after: Decimal | str | None
    delta: Decimal | None
    kind: str  # changed | became_null | became_value


@dataclass
class RowComparison:
    key: dict[str, Any]
    status: str  # matched | only_before | only_after
    diffs: list[FieldDiff] = field(default_factory=list)


@dataclass
class Comparison:
    classification: str
    notes: list[str] = field(default_factory=list)
    rows: list[RowComparison] = field(default_factory=list)

    @property
    def changed_fields(self) -> list[str]:
        names = {diff.field for row in self.rows for diff in row.diffs}
        return sorted(names)


def classify(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Name the kind of difference between two report rows (not their values)."""
    if before["company_id"] != after["company_id"]:
        return "different_company"
    if before["consolidation_scope"] != after["consolidation_scope"]:
        return "different_scope"
    if before["notification_id"] == after["notification_id"]:
        if before["content_hash"] == after["content_hash"]:
            if before["parser_version"] != after["parser_version"]:
                return "parser_change"
            return "identical_version"
        return "source_content_change"
    if before["filing_fiscal_year"] != after["filing_fiscal_year"]:
        return "new_period"
    if before["consolidation_scope"] != after["consolidation_scope"]:
        return "different_scope"
    return "new_publication_or_correction"


def _row_key(row: dict[str, Any]) -> tuple:
    # vw_fundamentals rows carry no is_comparative column: they are current-period by contract.
    return (row["fiscal_year"], row["fiscal_period"], bool(row.get("is_comparative", False)))


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def compare_rows(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
    *,
    numeric_delta_allowed: bool = True,
) -> list[RowComparison]:
    """Match rows by fiscal year/period/role and list field differences. NULL transitions are
    reported explicitly; deltas are omitted when units or bases are incompatible."""
    before_index = {_row_key(row): row for row in before_rows}
    after_index = {_row_key(row): row for row in after_rows}
    comparisons: list[RowComparison] = []
    for key in sorted(set(before_index) | set(after_index), key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        key_dict = dict(zip(ROW_KEY_FIELDS, key))
        if key not in before_index:
            comparisons.append(RowComparison(key_dict, "only_after"))
            continue
        if key not in after_index:
            comparisons.append(RowComparison(key_dict, "only_before"))
            continue
        before, after = before_index[key], after_index[key]
        row_delta_ok = (numeric_delta_allowed and before.get("currency_code") == after.get("currency_code")
                        and before.get("measuring_unit_date") == after.get("measuring_unit_date"))
        diffs: list[FieldDiff] = []
        for name in FINANCIAL_FIELDS:
            old, new = _as_decimal(before.get(name)), _as_decimal(after.get(name))
            if old == new:
                continue
            if old is None:
                diffs.append(FieldDiff(name, None, new, None, "became_value"))
            elif new is None:
                diffs.append(FieldDiff(name, old, None, None, "became_null"))
            else:
                diffs.append(FieldDiff(name, old, new, (new - old) if row_delta_ok else None, "changed"))
        for name in (*METHOD_FIELDS, "currency_code", "currency_scale", "period_start_date", "period_end_date", "measuring_unit_date"):
            if before.get(name) != after.get(name):
                diffs.append(FieldDiff(name, before.get(name), after.get(name), None, "changed"))
        comparisons.append(RowComparison(key_dict, "matched", diffs))
    return comparisons


def compare_reports(before: dict[str, Any], before_rows: list[dict[str, Any]], after: dict[str, Any], after_rows: list[dict[str, Any]]) -> Comparison:
    classification = classify(before, after)
    notes: list[str] = []
    if classification == "different_company":
        notes.append("reports belong to different companies; rows are not comparable")
        return Comparison(classification, notes)
    if classification == "different_scope":
        notes.append("consolidation scope differs; values are not comparable across scopes")
    if classification == "new_period":
        notes.append("filing periods differ; differences are normal year-to-year changes, not corrections")
    numeric_ok = classification not in ("different_scope",)
    rows = compare_rows(before_rows, after_rows, numeric_delta_allowed=numeric_ok)
    return Comparison(classification, notes, rows)


def render_table(comparison: Comparison) -> str:
    lines = [f"classification: {comparison.classification}"]
    lines.extend(f"note: {note}" for note in comparison.notes)
    for row in comparison.rows:
        role = "comparative" if row.key["is_comparative"] else "current"
        lines.append(f"-- {row.key['fiscal_year']} period {row.key['fiscal_period']} ({role}): {row.status}")
        for diff in row.diffs:
            delta = "" if diff.delta is None else f"  delta={diff.delta}"
            lines.append(f"   {diff.field:<48} {str(diff.before):>24} -> {str(diff.after):<24} [{diff.kind}]{delta}")
        if row.status == "matched" and not row.diffs:
            lines.append("   no differences")
    return "\n".join(lines)
