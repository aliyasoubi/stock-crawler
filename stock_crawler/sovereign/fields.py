"""Field -> source-series mapping for the ``MacroSovereign`` table (Türkiye).

Edit this file to change which series feeds a column (source URLs are in config/config.toml).
Every EVDS code below was verified against the live EVDS3 catalogue
(``GET /serieList/fe/type=json&code=<datagroup>``). The crawler re-checks the
codes on every run, so a renamed or rebased series fails fast instead of
silently loading nulls.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum


class Freq(IntEnum):
    """EVDS frequency codes (the ``frequency`` field of the ``/fe`` request)."""

    DAILY = 1
    BUSINESS_DAY = 2
    MONTHLY = 5
    QUARTERLY = 6
    ANNUAL = 8

    @property
    def months(self) -> int | None:
        """Length in months for calendar-aligned frequencies, else ``None``."""
        return {Freq.MONTHLY: 1, Freq.QUARTERLY: 3, Freq.ANNUAL: 12}.get(self)


class Agg(str, Enum):
    """How a finer-grained series is collapsed into a coarser period."""

    SUM = "sum"      # flows: GDP, tax revenue
    AVG = "avg"      # period-average indices: CPI
    LAST = "last"    # stocks / end-of-period rates: debt, policy rate, FX, CDS


class PeriodType(str, Enum):
    """Values written to ``MacroSovereign.PeriodType`` (varchar(10))."""

    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"

    @property
    def months(self) -> int:
        return {PeriodType.MONTHLY: 1, PeriodType.QUARTERLY: 3, PeriodType.ANNUAL: 12}[self]


@dataclass(frozen=True)
class SeriesSpec:
    code: str
    native_freq: Freq
    agg: Agg
    datagroup: str | None = None      # EVDS datagroup, used for pre-flight validation
    source: str = "evds"              # key into the sources mapping passed to the pipeline
    description: str = ""


@dataclass(frozen=True)
class FieldSpec:
    column: str
    series: tuple[SeriesSpec, ...]    # component series are summed
    precision: int                    # target column type decimal(precision, scale)
    scale: int
    unit: str                         # unit of the values, as the source publishes them
    valid_range: tuple[Decimal | None, Decimal | None] = (None, None)


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        column="Gdp",
        series=(
            SeriesSpec(
                "TP.GSYIH20.BY.B1GQ", Freq.QUARTERLY, Agg.SUM, "bie_gsyhhrccar",
                description="GDP, expenditure approach, current prices (TurkStat)",
            ),
        ),
        precision=24, scale=4, unit="thousand TRY", valid_range=(Decimal(0), None),
    ),
    FieldSpec(
        column="TaxRevenue",
        series=(
            SeriesSpec(
                "TP.KB.GEL003", Freq.MONTHLY, Agg.SUM, "bie_kbmgel",
                description="Central government budget revenues - I. Taxes (MoTF)",
            ),
        ),
        precision=24, scale=4, unit="thousand TRY", valid_range=(Decimal(0), None),
    ),
    FieldSpec(
        column="PublicDebt",
        # Monthly since 2002, released mid-month. EVDS only has general-government debt
        # quarterly (bie_finhestnks71013 ZP31+ZP34, quarterly from 2015) - see README.
        series=(
            SeriesSpec(
                "MOTF.CG.GROSS_DEBT", Freq.MONTHLY, Agg.LAST, source="treasury",
                description="Central government gross debt stock, domestic + external (MoTF)",
            ),
        ),
        precision=24, scale=4, unit="million TRY", valid_range=(Decimal(0), None),
    ),
    FieldSpec(
        column="InterestRate",
        series=(
            SeriesSpec(
                "TP.BISPOLFAIZ.TUR", Freq.MONTHLY, Agg.LAST, "bie_bispolfaiz",
                description="CBRT policy rate (one-week repo), end of period, via BIS",
            ),
        ),
        precision=8, scale=4, unit="percent", valid_range=(Decimal(0), Decimal(200)),
    ),
    FieldSpec(
        column="Cpi",
        series=(
            SeriesSpec(
                "TP.TUKFIY2025.GENEL", Freq.MONTHLY, Agg.AVG, "bie_tukfiy2025",
                description="CPI general index, 2025=100 (TurkStat)",
            ),
        ),
        precision=10, scale=4, unit="index 2025=100", valid_range=(Decimal(0), None),
    ),
    FieldSpec(
        column="FxRateUsd",
        series=(
            SeriesSpec(
                "TP.DK.USD.A.YTL", Freq.BUSINESS_DAY, Agg.LAST, "bie_dkdovytl",
                description="CBRT indicative USD/TRY forex buying rate, end of period",
            ),
        ),
        precision=14, scale=6, unit="TRY per USD", valid_range=(Decimal(0), Decimal(10_000)),
    ),
    FieldSpec(
        column="CdsSpreadBps",
        # EVDS does not publish sovereign CDS; supplied from a licensed feed via CSV.
        series=(
            SeriesSpec(
                "TURKEY_5Y_USD_CDS", Freq.DAILY, Agg.LAST, source="cds",
                description="Türkiye 5Y USD sovereign CDS mid spread, end of period",
            ),
        ),
        precision=10, scale=2, unit="bps", valid_range=(Decimal(0), Decimal(5_000)),
    ),
)
