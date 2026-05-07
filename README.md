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

Selectors are calibrated against `tulsa CJ-2024-1` (see
`data/_calibration/`). Metadata comes from OSCN's embedded
`<script id="json_style">` block; docket events from
`tr.docketRow.primary-entry`. PDF downloads are click-driven from the
case page (`a.doc-pdf` element) so each request is dispatched as a real
in-session interaction with Cloudflare-trusted referer/cookies.

### 1. Smoke test on a small batch

```bash
detection_pilot/.venv/bin/python ok_scraper/scraper.py \
  --year 2024 --type CJ --start 79 --count 3
```

Expected: 3 cases scraped, all PDFs downloaded silently (success returns
without printing), and a `register_of_actions.json` per case with
non-null `doc_filename` for every high-value action.

### 2. Mega-case test (verify the cap)

CJ-2024-82 is a 36-doc case that previously triggered an IP block.
With the per-case cap, the scraper should now download up to 5 PDFs and
defer the rest:

```bash
detection_pilot/.venv/bin/python ok_scraper/scraper.py \
  --year 2024 --type CJ --start 82 --count 1
```

Expected: 5 PDFs saved, the remaining 31 actions in
`register_of_actions.json` show `doc_filename: null`. No IP block.

### 3. Backfill batch

Auto-resume picks up where the last run left off:

```bash
detection_pilot/.venv/bin/python ok_scraper/scraper.py \
  --year 2024 --type CJ --count 25
```

The scraper writes `register_of_actions.json` per case as it goes; if
you interrupt and rerun, auto-resume continues from the highest-numbered
case directory present.

### 4. Failed-only retry pass (future)

Cases hitting the per-case cap or the consecutive-gate circuit breaker
land in `register_of_actions.json` with `doc_filename: null` for the
deferred PDFs. A future pass could reload those entries and retry their
clicks under cooler session conditions; the loader for that mode isn't
yet wired up.

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
| `--year` | `2024` | Year segment of the case number to iterate. |
| `--type` | `CJ` | Case-type prefix (single value; multi-type isn't supported in sequential mode). |
| `--start` | auto-resume | First sequence number. Defaults to `max(existing CJ_<YEAR>_N) + 1` under `data/`. |
| `--count` | `2` | How many sequential cases to attempt this run. |
| `--chrome` | off | Fall back to attaching to system Chrome over CDP (debugging only). Default is Camoufox. |

PDF downloads are serialized via a single semaphore. Per-case downloads
are capped at `PER_CASE_PDF_CAP = 5`, and a session abandons further
PDFs in a case after `MAX_CONSECUTIVE_GATES = 2` consecutive failures —
both bounds prevent a single mega-litigation case from burning the IP's
verification budget. Inter-PDF sleep is `random.uniform(5, 12)` seconds
on top of Camoufox's `humanize=True` jitter.

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
