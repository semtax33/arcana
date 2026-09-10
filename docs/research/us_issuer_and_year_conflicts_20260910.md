# US source scope and inconsistent dates

Reviewed 2026-09-10 KST. Source bytes remain in the SEC bronze archive with SHA-256 metadata. These checks neither infer a conversion ratio from prices nor rewrite an issuer's publication.

- Realty Income's 2021-11-12 acquisition exhibit contains **VEREIT** financial statements. The statement defines the company as VEREIT and identifies its symbol as VER. Its December 18, 2020 1-for-5 conversion therefore cannot adjust Realty Income (O) shares. A narrowly matched exclusion requires the original source hash, accession, O filing metadata, event date and ratio, and the explicit VER subject markers. The excluded event remains in the ledger review. [SEC exhibit](https://www.sec.gov/Archives/edgar/data/726728/000110465921138162/tm2132504d1_ex99-1.htm)
- RLI's November 20, 2024 8-K describes January 15, 2025 legal effectiveness, but prints January 16, 2024 for future adjusted trading. That chronology is internally inconsistent. The parser abstains; it does not silently change the year. [SEC 8-K](https://www.sec.gov/Archives/edgar/data/84246/000008424624000028/tmb-20241120x8k.htm)
- LQR House/YHC's April 2025 release likewise describes April 21, 2025 legal effectiveness and April 21, 2024 future adjusted trading. The same chronology check sends this source to review. [SEC release](https://www.sec.gov/Archives/edgar/data/1843165/000121390025033739/ea023887601ex99-1_lqrhouse.htm)

Independent final event evidence, where available elsewhere in the corpus, is still processed. Alpha Vantage action history remains a separately identified vendor source; these date checks do not promote it to official proof.
