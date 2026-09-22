# Verified historical titles

The registry uses exact, whitespace/case-normalized names and explicit aliases.
Never infer identity from XLSX row order or a partial name.

Two mechanisms were added on 2026-09-21 (see `docs/history/REVIEW-2026-09-21.md`):

- **Normalized matching** (`kap_export.loose_key`): tried only when the exact key
  finds nothing. It folds diacritics and punctuation, drops `VE` and a trailing
  legal form, and collapses a doubled final `İ`, so KAP's own spelling variants
  (`SANAYİ`/`SANAYİİ`, `MAMÜLLERİ`/`MAMULLERİ`, `BRİDGESTONE`/`BRIDGESTONE`,
  `FEDERAL-MOGUL`) match. It never drops a name word; the loader refuses a seed
  in which two tickers share a normalized key. Matches are listed in the run
  summary as `normalized_title_matches`.
- **`verify-aliases`** writes `config/kap_aliases.json`: titles proven by a
  single-company export request (only that MKK member ID was requested, so every
  returned row is that company's). Each entry records the request, the workbook
  SHA-256 and the notification IDs. Hand-verified aliases stay in this file's
  table and in `kap_companies.json`; generated ones stay in `kap_aliases.json`.

Seed correction on 2026-09-21: `SHTRP` carried a double-escaped `\u0026` instead of
`&`, so `SHELL & TURCAS PETROL A.Ş.` never matched.

Added on 2026-09-19:

| Ticker | Historical title | Evidence |
|---|---|---|
| ARSAN | ARSAN TEKSTİL TİCARET VE SANAYİ A.Ş. | ARSAN's 2025 audited financial report, note 1, printed p. 11 (PDF p. 11): title changed to Arsan Holding on 2026-02-05. |
| ARSNF | ZORLU FAKTORİNG A.Ş. | Same report, printed p. 12: subsidiary changed its title to Arsan Finans Faktoring; registration published on 2025-11-11. |

[Issuer's audited report](https://arsanholding.com.tr/wp-content/uploads/2026/03/Arsan-Holding-A.S.-31.12.2025-Bagimsiz-Denetci-Raporu.pdf)

[KAP ARSAN identity](https://kap.org.tr/en/sirket-bilgileri/ozet/4028e4a14158e41f01415b1862696142)

The uploaded archived workbook also contains ARSAN's 2024 notification 1404479
under the historical title and its 2025 notification 1571923 under the current title.
Source workbook SHA-256:
`d93478146752220d78ba31354811cd087a86e65011e1b3b731217e4e8e84c689`.

The alias for ARSNF resolves identity only. Its financial-sector statement still
requires a reviewed parser mapping and is not published as GENERAL-sector data.
