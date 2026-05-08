# OSCN Scraper

Local-only scraper for Oklahoma State Courts Network (`oscn.net`) civil dockets.
Mirrors the SF Superior Court scraper's architecture so the resulting corpus
slots directly into `detection_pilot/`.

For the research motivation and scope, see [GOALS.md](GOALS.md).

## Setup

The scraper runs under `detection_pilot/.venv` (Python 3.13), which has
both Playwright and Camoufox installed. Camoufox is a fingerprint-hardened
Playwright build that's necessary to clear Cloudflare gates on OSCN
document downloads — running under plain Chrome via CDP causes the IP to
trip CF's "failed verification" counter and get restricted.

```bash
cd /Users/jovik/Desktop/docket_gen
detection_pilot/.venv/bin/python ok_scraper/scraper.py --help
```

The Camoufox path is the default. `--chrome` falls back to attaching to
a system Chrome over CDP (port 9223) — kept only as a debugging aid;
expect significantly more CF gates and a faster path to IP restriction.

The Camoufox profile is ephemeral by default (managed in a tempdir per
launch), so there's no manual reset needed between runs. The system
Chrome profile at `~/.ok_manual_profile` is only relevant when running
with `--chrome`.

## Output layout

```
ok_scraper/data/
├── 2024-03-15/
│   ├── day_summary.json        — total/scraped/failed counts for the day
│   ├── failed_cases.json       — incomplete cases (only present if any)
│   ├── CJ-2024-1234/
│   │   ├── register_of_actions.json
│   │   └── 03-15-2024_DocID.pdf
│   └── ...
└── _calibration/               — HTML/JSON from --calibrate runs
```

The schema of `register_of_actions.json` matches the SF scraper's, so
`detection_pilot/scripts/*` work directly against this tree.

## Workflow

The scraper iterates **weekdays in `[--start-date, --end-date]`** and, for
each day, hits OSCN's `Results.aspx` once per case-type in `--type`. Cases
are written under `data/<filing_iso>/<CASE_NUMBER>/` keyed by the case's
**actual** Filed-On date (taken from the case-info page, with the search
day as fallback). Per-day state lives in `day_summary.json` and
`failed_cases.json`.

Selectors are calibrated against `tulsa CJ-2024-1` (see
`data/_calibration/`). Metadata comes from OSCN's embedded
`<script id="json_style">` block; docket events from
`tr.docketRow.primary-entry`. Search-result rows come from
`tr.resultTableRow`. PDF downloads use a session-request fast path with
a click-popup fallback when CF challenges fire.

### 1. Smoke test on one weekday

```bash
detection_pilot/.venv/bin/python ok_scraper/scraper.py \
  --start-date 2024-01-02 --end-date 2024-01-02 \
  --county tulsa --type CJ,CV
```

Expected: per-type search counts logged, cases scraped, a
`data/2024-01-02/day_summary.json` written with `total_cases`,
`scraped_cases`, and `per_type_kept` breakdown.

### 2. Multi-type day (civil + criminal)

```bash
detection_pilot/.venv/bin/python ok_scraper/scraper.py \
  --start-date 2024-01-03 --end-date 2024-01-03 \
  --type CJ,CV,CF,CM
```

If any single-day search returns ≥ 480 rows, the scraper logs a
`WATERMARK` warning and dumps the raw HTML under
`data/<day>/_search_dumps/<type>.html` for inspection. From the
diagnostic capture, real CJ counts per Tulsa weekday are well under
this; the watermark catches the day this assumption fails.

### 3. Backfill range with workers

```bash
detection_pilot/.venv/bin/python ok_scraper/scraper.py \
  --start-date 2024-01-02 --end-date 2024-01-31 \
  --type CJ,CV,CF,CM --workers 3
```

Workers chunk the date range into N contiguous slices; each spawns its
own Camoufox instance and writes per-worker logs to
`data/_worker_logs/worker_<i>_<start>_to_<end>.log`. Auto-resume skips
weekdays whose `day_summary.json` shows `scraped_cases >= total_cases`
and zero failures.

### 4. Force re-scrape

```bash
... --start-date 2024-01-02 --end-date 2024-01-02 --force
```

Re-scrapes even days marked complete. Per-case `register_of_actions.json`
files are still skipped if present — pass through `--force` doesn't
overwrite case-level data.

### 5. Failed-only retry pass (future)

Per-day `failed_cases.json` lists cases that errored mid-scrape (CF gate
overruns, popup timeouts). A `--failed-only` flag is on the roadmap; for
now you can manually re-run a small date range to retry — the new code
auto-skips already-complete cases.

### 5. Hand off to detection_pilot

Once enough days are populated:

```bash
python detection_pilot/scripts/inventory_cgc_motion_candidates.py \
  --data-root ok_scraper/data \
  --output-dir detection_pilot/manifests/ok_2024 \
  --filter-mode attorney_memoranda \
  --case-prefixes CJ,CV
```

The pilot's `extract_pdf_texts_from_manifest.py`, `sample_manifest_by_month.py`,
`build_liang_ready_inputs.py`, and `validate_known_mixtures.py` work the same
way they did for SF.

## CLI reference

| Flag | Default | Purpose |
|---|---|---|
| `--start-date` / `--end-date` | required | Inclusive YYYY-MM-DD range; weekdays only. |
| `--county` | `tulsa` | OSCN db parameter (`tulsa` or `oklahoma`). |
| `--type` | `CJ,CV,CF,CM` | Comma-separated case-type prefixes searched per day. |
| `--workers` | `1` | Parallel scraper processes; each gets a contiguous date slice. |
| `--chrome` | off | Fall back to attaching to system Chrome over CDP (debugging only). |
| `--force` | off | Re-scrape days even if `day_summary.json` marks them complete. |

PDF downloads are serialized via a single semaphore. Per-case downloads
are capped at `PER_CASE_PDF_CAP = 5`, and a session abandons further
PDFs in a case after `MAX_CONSECUTIVE_GATES = 2` consecutive failures —
both bounds prevent a single mega-litigation case from burning the IP's
verification budget. Inter-PDF sleep is `random.uniform(1, 3)` seconds
on top of Camoufox's `humanize=True` jitter.

### Migration: existing `data/CJ_YYYY_N/` data

Run once to move the old flat structure into the date-bucketed layout
and OCR every PDF with Tesseract:

```bash
detection_pilot/.venv/bin/python ok_scraper/migrate_existing.py
```

OCR result is per-action telemetry in each `register_of_actions.json`
(`text_filename`, `text_chars`, `text_extraction_status`, etc.). On
successful OCR the source PDF is deleted and replaced with the `.txt`;
on failure the PDF is preserved for a re-OCR pass with different
settings.

## Notes

- **Document discovery:** every docket row with a `GetDocument.aspx` link
  is recorded in `register_of_actions.json`. Only rows whose description
  matches `is_high_value` get downloaded (see `HIGH_VALUE_*_RE` patterns
  in `scraper.py`).
- **Filter calibration:** the `is_high_value` patterns are Oklahoma-tuned
  but not exhaustive. After your first batch, run
  `examples/generate_high_value_examples.py` against `ok_scraper/data`
  and spot-check assigned buckets. Tighten or loosen patterns based on
  what you see.
- **`_archive/`** holds the original exploration scripts (cloudscraper,
  undetected-chromedriver, etc.) for reference; none worked end-to-end.
