# OSCN Scraper

Local-only scraper for Oklahoma State Courts Network (`oscn.net`) civil dockets.
Mirrors the SF Superior Court scraper's architecture so the resulting corpus
slots directly into `detection_pilot/`.

For the research motivation and scope, see [GOALS.md](GOALS.md).

## Setup

The scraper reuses the SF scraper's Python environment (Playwright + tqdm).
If you don't already have it, set up `sf_scraper_fork/.venv` first.

```bash
cd /Users/jovik/Desktop/docket_gen
source sf_scraper_fork/.venv/bin/activate
```

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

Selectors have been calibrated against `tulsa CJ-2024-1` (see
`data/_calibration/`). The parser uses OSCN's embedded
`<script id="json_style">` block for case metadata and
`tr.docketRow.primary-entry` for docket events, both confirmed against a
real page.

### 1. Pilot run on one day

Scrape a single weekday to verify end-to-end behavior:

```bash
python ok_scraper/scraper.py \
  --start-date 2024-03-15 --end-date 2024-03-15 \
  --county tulsa --type CJ,CV
```

Expected output:

```
Chrome already running on port 9223.
Dates to process: 1 (weekdays only)

Processing 2024-03-15 (tulsa, types=CJ,CV)
  CJ: 18 cases
  CV: 7 cases
  25 pending of 25 total
2024-03-15:  60%|████████  | 15/25 [01:30<01:00,  6.0s/case]
```

Confirm `data/2024-03-15/day_summary.json` shows `scraped_cases` close to
`total_cases`, and that case directories contain non-empty
`register_of_actions.json` plus PDFs.

### 2. Backfill range

```bash
python ok_scraper/scraper.py \
  --start-date 2020-01-02 --end-date 2025-12-31 \
  --county tulsa --type CJ,CV
```

Solve Cloudflare in the Chrome window when prompted (typically once per
session). The scraper writes `day_summary.json` after every day, so you
can interrupt and resume.

### 3. Failed-only retry

After a first pass, rerun only cases listed in each day's
`failed_cases.json`:

```bash
python ok_scraper/scraper.py \
  --start-date 2020-01-02 --end-date 2025-12-31 \
  --county tulsa --failed-only
```

### 4. Sequential batching (when search is unreliable)

When the OSCN search endpoint is gated harder than case-info pages, you
can iterate case numbers directly. The scraper still extracts each case's
filed date from the page and writes to the correct `YYYY-MM-DD` folder.

```bash
python ok_scraper/scraper.py \
  --year 2024 --type CJ --start-num 1 --count 200 \
  --county tulsa
```

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
| `--start-date` / `--end-date` | — | Inclusive YYYY-MM-DD range, weekdays only. Required for search mode. |
| `--county` | `tulsa` | OSCN db parameter (`tulsa` or `oklahoma`). |
| `--type` | `CJ,CV` | Comma-separated case-type prefixes. |
| `--failed-only` | off | Only retry cases in each day's `failed_cases.json`. |
| `--year` | — | Sequential batching: iterate `<TYPE>-<YEAR>-N` for `--count` cases starting at `--start-num`. Skips search. |
| `--start-num` | `1` | Start integer for `--year` batching. |
| `--count` | `10` | How many sequential cases to attempt in `--year` batching. |

Chrome runs on debug port `9223` (offset from SF's `9222`) using profile
`~/.ok_manual_profile`. PDF downloads are serialized via a single
semaphore — concurrency is intentionally conservative to stay under
OSCN's rate-limit / IP-restriction thresholds.

## Notes

- **Profile location:** `~/.ok_manual_profile`. Distinct from SF's profile so
  Cloudflare clearance for one site doesn't get tangled with the other.
- **Document discovery:** the parser keeps every docket row, but only rows
  with a `GetDocument.aspx` link get downloaded. Rows are downloaded only if
  their description matches the `is_high_value` filter (see
  `HIGH_VALUE_*_RE` patterns in `scraper.py`).
- **Filter calibration:** the `is_high_value` patterns are Oklahoma-tuned but
  not exhaustive. After your first day of scraping, run
  `examples/generate_high_value_examples.py` against `ok_scraper/data` and
  spot-check which buckets the live filter assigned to each PDF. Tighten or
  loosen patterns based on what you see.
- **`_archive/`** holds the original exploration scripts (cloudscraper,
  undetected-chromedriver, etc.) for reference. None worked end-to-end;
  the manual-Turnstile path in `scraper.py` is the surviving approach.
