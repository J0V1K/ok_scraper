# Oklahoma Expansion: Research Goals

The project's central question is whether generative-AI use in court filings is rising over time. SF Superior Court provides one regional sample. Adding Oklahoma broadens coverage along three axes:

- **Geographic** — California → Oklahoma (different legal culture, bar size, document conventions).
- **Procedural** — SF's CGC civil docket is predominantly transactional/commercial; Oklahoma includes a meaningful share of criminal (`CF`, `CM`) and small-civil (`CV`, `SC`) filings the SF pilot doesn't see.
- **Court infrastructure** — OSCN is a unified statewide system with cross-county document availability, while SF is a single county's portal. The contrast reveals whether AI-writing patterns are state-bar-specific or generally national.

## Required corpus characteristics

For the Liang-MLE detector (`detection_pilot/`) to produce comparable estimates between SF and OK, the OK corpus must satisfy:

1. **Time-series coverage 2020 → present**, indexed by **filing date**, with weekday-resolution day buckets so the same monthly aggregation as `pilot_v2` can be applied.
2. **Attorney-authored briefs and motions** as the primary subtype — the same Liang counterfactual rewrite pipeline operates on this style of substantive prose. Pleadings, summons, and proofs of service are filtered out.
3. **Shape-matched corpus structure** — `data/YYYY-MM-DD/<CASE_NUMBER>/register_of_actions.json` plus per-case PDFs, identical to the SF layout, so `detection_pilot/scripts/inventory_cgc_motion_candidates.py`, `extract_pdf_texts_from_manifest.py`, `sample_manifest_by_month.py`, etc. work on OK output with at most a `--filter-mode` switch.
4. **Resumability** — `day_summary.json` records `total_cases` / `scraped_cases` / `failed_cases`; `failed_cases.json` lists incomplete cases for `--failed-only` retries.
5. **Auditable counts** — every filing day's record must distinguish "discovered cases" from "scraped cases with documents" so coverage tables analogous to the SF dataset card can be built.

## Scope decisions

- **Counties:** Start with **Tulsa** (testing) and expand to **Oklahoma County** (Oklahoma City). Other Oklahoma counties have docket text available via OSCN but few PDFs; they are not viable for the document-text portion of the analysis. Federal Eastern, Northern, and Western District of Oklahoma cases are out of scope (they live on PACER, not OSCN).
- **Case types in scope:** `CJ` (Civil Judgment — formal civil cases) and `CV` (Civil — general civil) are the closest analog to SF's `CGC`. `CF`/`CM` (criminal felony / misdemeanor) are tracked in metadata but not the primary detection target.
- **Out of scope:** Probate (`PB`), small claims (`SC`), traffic (`TR`), juvenile (`JF`/`JD`), domestic (`DM`) — these have either too little prose content (small claims, traffic) or restricted access (juvenile, domestic).
- **Date range:** Primary collection target `2020-01-01` → `2025-12-31` matching the SF scope; pre-2020 only as needed for an uncontaminated baseline.

## Success criteria

By the time the OK collection is "complete enough" to feed the detection pilot:

- ≥ 2,000 cases with at least one `register_of_actions.json` per case across `2020-2025`.
- ≥ 500 documents matching the `attorney_memoranda` filter in each year (sufficient for monthly Liang-MLE estimation per year).
- A coverage matrix (year × month × county) showing no month has zero filings.
- An OK-equivalent of `detection_pilot/runs/pilot_v2/report/pilot_v2_report.md` with monthly alpha estimates, calibration validation, and human-vs-AI stylistic differentials. The expectation is that the output mirrors SF's report so the two can be compared head-to-head.

## Known constraints / risks

- **Cloudflare Turnstile.** OSCN gates dockets behind Turnstile (introduced ~2024). Like the SF scraper, this requires a manual solve once per browser profile; the scraper resumes once cleared.
- **No request-path bypass.** Unlike SF (where `GetROA` returns JSON given a SessionID), OSCN serves only HTML. Every scrape is a browser navigation. This makes OK slower than SF per case.
- **PDF availability.** Many OSCN docket entries link to documents but a substantial fraction of older entries have no PDF at all (just docket text). The detection pilot's expected effective sample size will be lower per case than SF.
- **Document-type vocabulary differs.** Oklahoma uses different terminology (`MOTION TO DISMISS`, `BRIEF IN SUPPORT`) than California (`MEMORANDUM OF POINTS AND AUTHORITIES`, `RFO`). The `is_high_value` filter in `scraper.py` is Oklahoma-tuned; SF filters do not transfer directly.
- **Selector calibration required.** OSCN's HTML structure is not yet verified from a live successful fetch. The scraper's `--calibrate` mode dumps real HTML so selectors can be locked in before a long run.
