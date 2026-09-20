# Real KAP comparison fixture

Captured on 2026-09-14 from `POST https://www.kap.org.tr/en/api/export/compareItems`.
Exact request: `request.json`. Response: `two_companies_2023_2024.xlsx` (5,100 bytes).
This is a real source response, not a synthetic financial fixture. SHA-256:
`5e5d405b61ac05ff7204fc60554e01ef7be1e88fe11d1561e4c1899b0dddde42`.

Rows: ASELS 2023 (1262825), ASELS 2024 (1395801), THYAO 2023 (1266721),
THYAO 2024 (1396940). Downloaded using the exact endpoint/payload shape in the
user-supplied scripts, with a deliberately small request. No account credentials
or response cookies are stored. Publication timestamps belong to each row;
this file was retrieved later and must not be treated as an as-of backtest.

Source limitations: https://www.kap.org.tr/en/kalem-karsilastirma
The source warns that this is delayed, current-column data and does not include
prior-period adjustments. Workbook amounts use Turkish grouping and explicit
presentation currency/scale. The source workbook reports no exact period start/end.

The HTML fixtures under `../source/` are synthetic and remain explicitly labelled.
