import asyncio
import csv
import contextlib
import json
import os
import random
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import async_playwright
from tqdm import tqdm

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

# Inline OCR helper (Tesseract + pdfimages). Imported here so the
# scrape path can convert each saved PDF to text immediately and
# discard the PDF on successful extraction (storage win ~50x).
sys_path_ocr = str(Path(__file__).resolve().parent)
if sys_path_ocr not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_ocr)
from ocr import ocr_pdf as _ocr_pdf  # noqa: E402

# Cross-scraper heartbeat helper (lives in <repo>/monitor/).
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in __import__("sys").path:
    __import__("sys").path.insert(0, _repo_root)
from monitor.heartbeat import Heartbeat, probe_public_ip, rotation_managed  # noqa: E402

# Heartbeat singleton — populated in main()/worker init, accessed by the
# day/case loops so per-step state is visible in the monitor dashboard.
HEARTBEAT: Heartbeat | None = None

# Module-level toggles set by main() and read by download_pdf.
# Default behavior: OCR every saved PDF, delete the PDF on OCR success.
RUN_OCR = True
KEEP_PDFS = False
DISABLE_FILTER = False  # When True, attempt every doc-bearing action (still capped at PER_CASE_PDF_CAP).
DISABLE_CAP = False     # When True, ignore PER_CASE_PDF_CAP — mega-cases will burn through CF budget.
DOCTYPE_ANNOTATIONS: dict[str, bool | None] | None = None
DOCTYPE_REVIEW_NEEDED: dict[str, dict] = {}
PDF_REQUEST_JITTER = (0.05, 0.2)
CASE_START_JITTER = (0.1, 0.3)
# Default off: the popup-CF "fallback" path empirically does NOT reliably
# unlock subsequent session requests, so each cycle costs an OSCN
# verification credit without producing the document. Default behavior
# now records the doc as gated and moves on. Opt back in via --enable-popup-fallback.
ENABLE_POPUP_FALLBACK = False

# --- Configuration ---
DEBUG_PORT = 9223
CHROME_PROFILE = Path.home() / ".ok_manual_profile"
BASE_URL = "https://www.oscn.net/dockets"
SEARCH_URL = f"{BASE_URL}/Results.aspx"
CASE_URL = f"{BASE_URL}/GetCaseInformation.aspx"
DOC_URL = f"{BASE_URL}/GetDocument.aspx"
DATA_ROOT = Path(__file__).resolve().parent / "data"

# OSCN's Results.aspx caps responses at ~500 rows. If a single-day search
# comes back at or above this watermark we dump the raw HTML so we know
# the cap was real (vs. saturated by archived noise that filters out).
SEARCH_RESULT_WATERMARK = 480

# Default civil + criminal scope: civil-judgment, civil, criminal felony,
# criminal misdemeanor. Adjust per --type.
DEFAULT_TYPES = ("CJ", "CV", "CF", "CM")

# Case-type prefix → OSCN's `dcct` (Docket Case-Class Type) numeric ID.
# Source: the dropdown on OSCN's search form (`<select id="dcct">`).
# When a `dcct` is supplied to Results.aspx, the response is filtered
# server-side, which sidesteps the 500-row response cap that would
# otherwise saturate on busy multi-type Tulsa days.
TYPE_TO_DCCT = {
    "CJ": "2",    # Civil relief more than $10,000
    "CV": "1",    # Civil relief less than $10,000
    "CF": "31",   # Criminal Felony
    "CM": "32",   # Criminal Misdemeanor
    "CS": "33",   # Criminal Miscellaneous
    "PO": "34",   # Protective Order
    "PB": "7",    # Probate
    "SC": "26",   # Small Claims
    "TR": "18",   # Traffic
    "YO": "82",   # Youthful Offender
    "FD": "3",    # Family and Domestic
    "DM": "3",    # Family and Domestic (alt prefix)
    "PA": "5",    # Paternity
    "MI": "22",   # Civil Misc.
    "BC": "61",   # Civil Administrative
    "TS": "10",   # Trusts
    "TX": "43",   # Tax Liens
    "WC": "30",   # Writ (Habeas)
}

# --- Globals ---
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)

# --- Doc-type sampling (opt-in via --doc-type-samples) ---
# When enabled, the scraper records a per-worker dictionary of OSCN docket
# `code` -> {count, sample proceedings, text metrics, exemplar PDF path} and
# retains ONE PDF exemplar per code (skips the post-OCR delete for the first
# doc of each code). Bootstraps a document-type dictionary a human later
# annotates high-value vs not. Default OFF -> identical behavior to today.
DOC_TYPE_SAMPLING = False
DOC_TYPE_DICT: dict = {}
EXEMPLAR_CLAIMED: set = set()
WORKER_TAG = "main"
MAX_SAMPLE_PROCEEDINGS = 3


def _doc_type_dict_path() -> Path:
    return DATA_ROOT / f"_doc_type_dictionary.{WORKER_TAG}.json"


def load_doc_type_dict() -> None:
    """Seed DOC_TYPE_DICT / EXEMPLAR_CLAIMED from an existing per-worker file so
    rotate.py relaunches and --force resumes accumulate rather than overwrite."""
    path = _doc_type_dict_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        return
    if isinstance(data, dict):
        DOC_TYPE_DICT.update(data)
        for code, rec in data.items():
            if isinstance(rec, dict) and rec.get("exemplar_relpath"):
                EXEMPLAR_CLAIMED.add(code)


def write_doc_type_dict() -> None:
    path = _doc_type_dict_path()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(DOC_TYPE_DICT, indent=2, sort_keys=True))
        tmp.replace(path)
    except Exception as e:
        print(f"  doc-type dict write failed: {e}")


def update_doc_type_dict(actions: list[dict], filing_iso: str, case_num: str) -> None:
    """Fold one case's doc-bearing actions into DOC_TYPE_DICT, keyed by OSCN
    docket `code`. Records counts, sample proceedings, light text metrics, and
    the relpath of the first retained PDF exemplar per code."""
    for a in actions:
        if not a.get("doc_url"):
            continue
        code = (a.get("code") or "").strip() or "_UNCODED"
        rec = DOC_TYPE_DICT.get(code)
        if rec is None:
            rec = {
                "count": 0,
                "first_seen": utc_now_iso(),
                "sample_proceedings": [],
                "exemplar_relpath": None,
                "text_chars_total": 0,
                "text_pages_total": 0,
                "n_with_text": 0,
            }
            DOC_TYPE_DICT[code] = rec
        rec["count"] += 1
        proc = (a.get("proceedings") or "").strip()
        if proc and proc not in rec["sample_proceedings"] and len(rec["sample_proceedings"]) < MAX_SAMPLE_PROCEEDINGS:
            rec["sample_proceedings"].append(proc[:200])
        chars = a.get("text_chars")
        if isinstance(chars, int) and chars > 0:
            rec["text_chars_total"] += chars
            rec["text_pages_total"] += int(a.get("text_pages") or 0)
            rec["n_with_text"] += 1
        if rec["exemplar_relpath"] is None and a.get("doc_filename"):
            rec["exemplar_relpath"] = f"{filing_iso}/{case_num}/{a['doc_filename']}"


def _normalize_doctype_code(code: str | None) -> str:
    return (code or "").strip().upper() or "_UNCODED"


def _parse_annotation_value(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in ("true", "yes", "1", "y", "t"):
        return True
    if raw in ("false", "no", "0", "n", "f"):
        return False
    return None


def load_doctype_annotations(path: Path) -> dict[str, bool | None]:
    """Load code -> high_value annotations from dictionary.json or CSV.

    Accepted inputs:
      * dictionary.json shaped as {CODE: {"high_value": true|false|null, ...}}
      * annotations/review CSV with columns code, high_value
    """
    annotations: dict[str, bool | None] = {}
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("doctype annotation JSON must be an object keyed by code")
        for code, rec in data.items():
            if isinstance(rec, dict):
                value = rec.get("high_value")
            else:
                value = rec
            annotations[_normalize_doctype_code(code)] = _parse_annotation_value(value)
        return annotations

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            code = _normalize_doctype_code(row.get("code"))
            annotations[code] = _parse_annotation_value(row.get("high_value"))
    return annotations


def is_high_value_by_code(code: str | None, proceedings: str = "") -> bool:
    """Annotation-driven filter with recall-safe default for new codes."""
    if DOCTYPE_ANNOTATIONS is None:
        return is_high_value(proceedings)
    key = _normalize_doctype_code(code)
    value = DOCTYPE_ANNOTATIONS.get(key)
    if value is True:
        return True
    if value is False:
        return False
    rec = DOCTYPE_REVIEW_NEEDED.setdefault(key, {
        "count": 0,
        "first_seen": utc_now_iso(),
        "sample_proceedings": [],
    })
    rec["count"] += 1
    proc = (proceedings or "").strip()
    if proc and proc not in rec["sample_proceedings"] and len(rec["sample_proceedings"]) < MAX_SAMPLE_PROCEEDINGS:
        rec["sample_proceedings"].append(proc[:200])
    return True


def write_doctype_review_needed() -> None:
    if DOCTYPE_ANNOTATIONS is None or not DOCTYPE_REVIEW_NEEDED:
        return
    path = DATA_ROOT / f"_doc_type_review_needed.{WORKER_TAG}.json"
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(DOCTYPE_REVIEW_NEEDED, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"  doc-type review-needed write failed: {e}")

# Guardrails so a single big-litigation case can't burn through CF's
# "failed-verification" budget. Each click on an a.doc-pdf is a request
# OSCN sees; cap both attempts per case and consecutive gate-fails so
# that one outlier case caps damage to the IP's reputation.
PER_CASE_PDF_CAP = 5
MAX_CONSECUTIVE_GATES = 2
# When True, on the first CF gate inside a case, re-navigate to the
# case-info page to refresh CF cookies and retry the same PDF once.
# Empirically CF allows ~3 PDF requests per case-info-page-clear; this
# trades extra Turnstile cost for more captures per case.
REFRESH_ON_GATE = False
# How many CF-popup fallbacks we'll tolerate per case before bailing.
# Each popup costs ~15s + an extra Turnstile solve. If we're being forced
# down this path on most actions, the IP's CF reputation is degrading
# fast; better to abandon the case than feed the gate.
MAX_POPUP_FALLBACKS_PER_CASE = 8

# --- High-Value Document Filters ---
HIGH_VALUE_BRIEF_RE = re.compile(
    r"\b(MOTION|OPPOSITION|REPLY|DEMURRER|MEMORANDUM|POINTS AND AUTHORITIES|TRIAL BRIEF|BRIEF|EX PARTE|REQUEST FOR ORDER|RFO|STIPULATION|APPLICATION|PETITION)\b",
    re.IGNORECASE,
)
HIGH_VALUE_DECLARATION_RE = re.compile(
    r"\b(DECLARATION|AFFIDAVIT|RESPONSIVE DECLARATION)\b",
    re.IGNORECASE,
)
HIGH_VALUE_PLEADING_RE = re.compile(
    r"\b(ANSWER|COMPLAINT|PETITION|CROSS-COMPLAINT|AMENDED|SUPPLEMENTAL)\b",
    re.IGNORECASE,
)

def is_high_value(text):
    return any([
        HIGH_VALUE_BRIEF_RE.search(text),
        HIGH_VALUE_DECLARATION_RE.search(text),
        HIGH_VALUE_PLEADING_RE.search(text)
    ])

def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def filed_to_iso(filed_str: str) -> str:
    if not filed_str: return ""
    s = filed_str.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try: return datetime.strptime(s, fmt).date().isoformat()
        except ValueError: continue
    return ""


def iso_to_oscn(iso_or_date) -> str:
    """Convert a YYYY-MM-DD ISO string (or date object) to OSCN's MM-DD-YYYY.

    Note: OSCN's Results.aspx accepts dates as `FiledDateL` / `FiledDateH`
    in MM-DD-YYYY (dash) format — NOT MM/DD/YYYY (slash). Slashes are
    silently ignored and the search falls back to "all recent" — which
    is what tripped us up on early runs (returning 500 mixed-type rows
    including 1989/1999/etc archived cases).
    """
    if hasattr(iso_or_date, "strftime"):
        return iso_or_date.strftime("%m-%d-%Y")
    return datetime.strptime(iso_or_date, "%Y-%m-%d").strftime("%m-%d-%Y")


def weekday_dates(start_iso: str, end_iso: str) -> list[date]:
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end = datetime.strptime(end_iso, "%Y-%m-%d").date()
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            out.append(cur)
        cur += timedelta(days=1)
    return out


def split_dates_into_chunks(dates: list[date], n: int) -> list[list[date]]:
    """Split a list of dates into n contiguous chunks, balanced for size."""
    if n <= 1 or not dates:
        return [dates] if dates else []
    chunk = len(dates) // n
    rem = len(dates) % n
    out, cursor = [], 0
    for i in range(n):
        sz = chunk + (1 if i < rem else 0)
        if sz <= 0:
            continue
        out.append(dates[cursor:cursor + sz])
        cursor += sz
    return out


def day_dir(filing_iso: str) -> Path:
    return DATA_ROOT / filing_iso


def case_dir_for(filing_iso: str, case_num: str) -> Path:
    return day_dir(filing_iso) / case_num


def case_is_complete(filing_iso: str, case_num: str) -> bool:
    return (case_dir_for(filing_iso, case_num) / "register_of_actions.json").exists()


def update_day_summary(filing_iso: str, **fields) -> dict:
    """Read-modify-write for data/<filing_iso>/day_summary.json."""
    d = day_dir(filing_iso)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "day_summary.json"
    summary = {}
    if path.exists():
        try:
            summary = json.loads(path.read_text())
        except Exception:
            summary = {}
    summary.update(fields)
    summary.setdefault("filing_date", filing_iso)
    summary["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(summary, indent=2))
    return summary


def write_failed_cases(filing_iso: str, failed: list[dict]) -> None:
    path = day_dir(filing_iso) / "failed_cases.json"
    if not failed:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(failed, indent=2))


def load_failed_cases(filing_iso: str) -> list[dict]:
    path = day_dir(filing_iso) / "failed_cases.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def day_is_complete(filing_iso: str) -> bool:
    """A day is complete if day_summary exists and reports no failures and
    scraped_cases >= total_cases. Used by auto-resume."""
    path = day_dir(filing_iso) / "day_summary.json"
    if not path.exists():
        return False
    try:
        s = json.loads(path.read_text())
    except Exception:
        return False
    total = int(s.get("total_cases", 0) or 0)
    scraped = int(s.get("scraped_cases", 0) or 0)
    failed = int(s.get("failed_cases", 0) or 0)
    return total > 0 and scraped >= total and failed == 0


def find_resume_dates(start_iso: str, end_iso: str) -> list[date]:
    """Return weekdays in [start, end] that aren't yet `day_is_complete`."""
    return [d for d in weekday_dates(start_iso, end_iso) if not day_is_complete(d.isoformat())]


# --- Party / attorney metadata helpers ---


def _normalize_party_key(name: str) -> str:
    """Normalize a party name for fuzzy comparison between the parties list
    and the attorney's represented_parties cell. OSCN inserts non-breaking
    spaces and trailing commas inconsistently."""
    if not name:
        return ""
    s = name.replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(",.").strip()
    return s.upper()


def annotate_pro_se(parties: list[dict], attorneys: list[dict]) -> list[dict]:
    """Return a copy of parties with `pro_se: bool` set per party.

    A party is marked represented if any attorney's represented_parties
    contains a substring match (in either direction) against the party name.
    Substring containment is necessary because OSCN abbreviates: e.g.
    case style has "RICHARDSON, JAMARIO DAMONT" but attorney cell may say
    "RICHARDSON, JAMARIO" or just "RICHARDSON".
    """
    represented_keys: list[str] = []
    for atty in attorneys or []:
        for rep in (atty.get("represented_parties") or []):
            k = _normalize_party_key(rep)
            if k:
                represented_keys.append(k)

    out = []
    for p in parties or []:
        key = _normalize_party_key(p.get("name", ""))
        is_rep = False
        if key:
            for rep in represented_keys:
                if rep and (rep in key or key in rep):
                    is_rep = True
                    break
        out.append({**p, "pro_se": (not is_rep) if key else False})
    return out


def representation_rollups(parties_with_pro_se: list[dict], attorneys: list[dict]) -> dict:
    """Cheap rollups for stratification queries downstream."""
    n_parties = len(parties_with_pro_se)
    pro_se_count = sum(1 for p in parties_with_pro_se if p.get("pro_se"))
    return {
        "n_parties": n_parties,
        "n_attorneys": len(attorneys or []),
        "n_pro_se_parties": pro_se_count,
        "any_pro_se": pro_se_count > 0,
        "all_represented": (n_parties > 0 and pro_se_count == 0),
    }


# --- Search-results parser ---

SEARCH_PARSE_JS = """
() => {
    const rows = Array.from(document.querySelectorAll('tr.resultTableRow'));
    const out = [];
    for (const row of rows) {
        const numA = row.querySelector('td.result_casenumber a');
        if (!numA) continue;
        const caseNum = numA.innerText.trim();
        if (!caseNum) continue;
        const styleEl = row.querySelector('td.result_shortstyle');
        const style = styleEl ? styleEl.innerText.replace(/\\s+/g, ' ').trim() : '';
        const dateEl = row.querySelector('td.result_datefiled');
        const dateText = dateEl ? dateEl.innerText.trim() : '';
        out.push({
            case_number: caseNum,
            url: numA.href,
            style: style,
            filed_text: dateText,
        });
    }
    return out;
}
"""


def search_url(county: str, dcct: str, oscn_date_low: str,
               oscn_date_high: str | None = None) -> str:
    """Build a Results.aspx URL using OSCN's actual parameter names.

    Verified URL shape (per OSCN search form):
        Results.aspx?db=tulsa&FiledDateL=MM-DD-YYYY&FiledDateH=MM-DD-YYYY&dcct=2

    Use the `dcct` numeric ID (from `<select id="dcct">` on OSCN's search
    form) for server-side type filtering — see TYPE_TO_DCCT. Passing an
    empty `dcct` means "All Case Types".

    Date format must be MM-DD-YYYY (dashes); slashes are silently
    ignored and the search falls back to "all recent". If
    `oscn_date_high` is omitted we use a single-day window (low == high).
    """
    if oscn_date_high is None:
        oscn_date_high = oscn_date_low
    qs = {
        "db": county,
        "FiledDateL": oscn_date_low,
        "FiledDateH": oscn_date_high,
    }
    if dcct:
        qs["dcct"] = dcct
    return f"{SEARCH_URL}?{urlencode(qs)}"


async def search_one_day(page, county: str, types: list[str], filing_iso: str) -> tuple[list[dict], dict[str, int]]:
    """One Results.aspx search per type (server-filtered via `dcct`).

    OSCN's `dcct` parameter selects a single case-class on the server,
    so each per-type search has its own 500-row response budget — much
    higher real coverage than a single all-types search would give.

    Returns (filtered_cases, raw_count_by_type) where raw_count_by_type
    maps each requested type to the number of raw rows OSCN returned
    for that type's search (used for watermark monitoring).
    """
    oscn_date = iso_to_oscn(filing_iso)
    seen: set[str] = set()
    out: list[dict] = []
    raw_by_type: dict[str, int] = {}

    for case_type in types:
        case_type_u = case_type.upper()
        dcct = TYPE_TO_DCCT.get(case_type_u)
        if dcct is None:
            print(f"  unknown case type {case_type_u!r}; no dcct mapping. Skipping.")
            continue

        url = search_url(county, dcct, oscn_date)
        try:
            await page.goto(url, wait_until="commit", timeout=60_000)
        except Exception:
            pass

        await wait_for_human_solve(page, target_text="Case Search Results")

        try:
            await page.wait_for_selector("tr.resultTableRow, table.resultsTable", timeout=8_000)
        except Exception:
            # Zero-result day for this type is legitimate.
            pass

        raw = await page.evaluate(SEARCH_PARSE_JS)
        raw_count = len(raw)
        raw_by_type[case_type_u] = raw_count

        if raw_count >= SEARCH_RESULT_WATERMARK:
            dump_dir = day_dir(filing_iso) / "_search_dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            try:
                dump_path = dump_dir / f"{case_type_u}.html"
                dump_path.write_text(await page.content())
                print(f"  WATERMARK: {filing_iso} {case_type_u} returned {raw_count} rows "
                      f"(>= {SEARCH_RESULT_WATERMARK}); raw HTML dumped to {dump_path}")
            except Exception as e:
                print(f"  WATERMARK dump failed for {filing_iso} {case_type_u}: {e}")

        # Client-side prefix verification: dcct should already isolate
        # the right case-class, but defense-in-depth catches any
        # mis-mapping (e.g., dcct=33 returning multiple criminal-misc
        # subtypes).
        kept_for_type = 0
        for row in raw:
            cn = row["case_number"].strip().upper()
            if not cn or cn in seen:
                continue
            prefix = cn.split("-")[0]
            if prefix != case_type_u:
                # Server returned a row whose prefix doesn't match. Skip.
                continue
            style = (row.get("style") or "").lower()
            if "no record" in style:
                continue
            seen.add(cn)
            out.append({
                "case_number": cn,
                "case_type": case_type_u,
                "url": row["url"],
                "style": row.get("style", ""),
                "search_filed_text": row.get("filed_text", ""),
                "county": county,
            })
            kept_for_type += 1
        print(f"  {case_type_u} (dcct={dcct}): raw={raw_count}, kept={kept_for_type}")

    return out, raw_by_type


def launch_chrome():
    """Launch a real Chrome instance with remote debugging."""
    CHROME_PROFILE.mkdir(exist_ok=True)
    try:
        subprocess.check_output(f"lsof -i :{DEBUG_PORT}", shell=True)
        return
    except: pass

    print(f"Launching Google Chrome on port {DEBUG_PORT}...")
    cmd = [
        "open", "-g", "-na", "Google Chrome",
        "--args",
        f"--user-data-dir={CHROME_PROFILE}",
        f"--remote-debugging-port={DEBUG_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    subprocess.Popen(cmd)
    time.sleep(5)

async def _click_visible(scope, selectors, *, timeout_ms=1500, force=False):
    for selector in selectors:
        try:
            locator = scope.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible(timeout=timeout_ms):
                continue
            await locator.click(timeout=timeout_ms, force=force)
            return selector
        except Exception:
            continue
    return None

async def _click_turnstile_checkbox(page):
    scopes = [page, *page.frames]

    iframe_selectors = [
        "iframe[src*='turnstile']",
        "iframe[title*='Widget']",
        "iframe[title*='challenge']",
    ]

    # Best path: drill into the Turnstile iframe and click the actual
    # checkbox/label element. Cross-origin iframe content is reachable
    # via Playwright's frame_locator() in the same browser context.
    for selector in iframe_selectors:
        try:
            iframe = page.locator(selector).first
            if await iframe.count() == 0:
                continue
            frame = page.frame_locator(selector)
            for inner in [
                "input[type='checkbox']",
                "label.ctp-checkbox-label",
                "label[for*='checkbox']",
                "#challenge-stage",
                "[role='checkbox']",
            ]:
                try:
                    el = frame.locator(inner).first
                    if await el.count() and await el.is_visible(timeout=1000):
                        await el.click(timeout=1500)
                        return f"frame:{selector} > {inner}"
                except Exception:
                    continue
        except Exception:
            continue

    # Fallback: fixed-offset click near the LEFT of the Turnstile iframe
    # (Cloudflare's standard widget renders the checkbox ~30px from the
    # left edge, vertically centered). Center-clicks miss it entirely.
    for selector in iframe_selectors:
        try:
            iframe = page.locator(selector).first
            if await iframe.count() == 0:
                continue
            box = await iframe.bounding_box()
            if not box:
                continue
            cx = box["x"] + 30
            cy = box["y"] + box["height"] / 2
            await page.mouse.click(cx, cy)
            return f"iframe-checkbox-offset:{selector}"
        except Exception:
            continue

    selector_hit = await _click_visible(
        page,
        [
            "#challenge-stage",
            "#cf-turnstile-wrapper",
            ".cf-turnstile",
        ],
        force=True,
    )
    if selector_hit:
        return selector_hit

    for scope in scopes:
        selector_hit = await _click_visible(
            scope,
            [
                "input[type='checkbox']",
                "[role='checkbox']",
                "label.ctp-checkbox-label",
                "label[for*='checkbox']",
                "button[type='button']",
            ],
            force=True,
        )
        if selector_hit:
            return selector_hit
        try:
            text_locator = scope.get_by_text(re.compile(r"verify you are human|click to verify", re.I)).first
            text_timeout_ms = 1500
            if await text_locator.count() and await text_locator.is_visible(timeout=text_timeout_ms):
                await text_locator.click(timeout=text_timeout_ms, force=True)
                return "text:verify-you-are-human"
        except Exception:
            continue

    return None

async def _submit_challenge_page(page):
    scopes = [page, *page.frames]
    for scope in scopes:
        selector_hit = await _click_visible(
            scope,
            [
                "#btnSearch",
                "[name='btnSearch']",
                "#btnContinue",
                "[name='btnContinue']",
                "button[type='submit']",
                "input[type='submit']",
                "a.btn-continue",
                "button",
            ],
            timeout_ms=2000,
            force=True,
        )
        if selector_hit:
            return selector_hit
        try:
            button = scope.get_by_role("button", name=re.compile(r"submit|continue|search|view|download", re.I)).first
            if await button.count() and await button.is_visible(timeout=2000):
                await button.click(timeout=2000, force=True)
                return "role:button"
        except Exception:
            pass
        try:
            link = scope.get_by_role("link", name=re.compile(r"submit|continue|search|view|download", re.I)).first
            if await link.count() and await link.is_visible(timeout=2000):
                await link.click(timeout=2000, force=True)
                return "role:link"
        except Exception:
            pass
    return None

async def _page_content_or_blank(page):
    if page.is_closed():
        return ""
    try:
        return await page.content()
    except Exception:
        return ""

async def wait_for_human_solve(
    page,
    target_text="Case Search Results",
    auto_submit=True,
    return_on_submit=False,
    closed_ok=False,
    max_wait_s=90,
    max_submit_reloads=3,
):
    """Pause until the user solves the Cloudflare challenge, with auto-submit and retries.

    `max_wait_s` defaults to 90 to guarantee an upper bound on the loop. Without
    this, an unrecognized page state (e.g. CF "verifying your connection..."
    interstitial that doesn't match our challenge markers OR our success
    markers) leaves the loop with no exit condition. The download-path call
    site overrides this with a tighter 35s.
    """
    print(f"Waiting for human solve (detecting: '{target_text}', auto_submit={auto_submit}, return_on_submit={return_on_submit})...")
    start_wait = time.monotonic()
    submitted_at = 0
    submit_reload_count = 0
    
    while True:
        try:
            elapsed_s = time.monotonic() - start_wait
            if max_wait_s is not None and elapsed_s > max_wait_s:
                raise TimeoutError(f"Timed out after {max_wait_s}s waiting for challenge clear")
            if page.is_closed():
                if closed_ok:
                    print("Challenge page closed; treating as success")
                    return True
                raise Exception("PAGE_CLOSED")
            title = await page.title()
            content = await _page_content_or_blank(page)
            url_lower = page.url.lower()
            
            # Detect IP Restricted
            if "restiction may be caused by the following" in content or \
               "Temporary restiction expires" in content or \
               "Access Denied" in title:
                print("\nFATAL ERROR: YOUR IP ADDRESS IS RESTRICTED BY OSCN.\n")
                raise Exception("IP_RESTRICTED")

            # Detect Case Tracker Redirect (Auth Page)
            if "CASE TRACKER" in content and "Sign in" in content:
                print("\n>>> REDIRECT DETECTED: Landed on Case Tracker sign-in page.")
                print("    This case (likely Criminal) may require authentication or is being gated.")
                raise Exception("AUTH_REQUIRED")

            # Detect "UNABLE TO VERIFY"
            if "UNABLE TO VERIFY" in content:
                print("\n>>> VERIFICATION ERROR: Reloading page..."); await page.reload(); await asyncio.sleep(3)
                submitted_at = 0; submit_reload_count = 0; continue

            # Identify challenge
            is_challenged = ("Turnstile" in title or "Just a moment" in title or "challenge-platform" in content)
            if is_challenged:
                # 1. Check if Turnstile is already solved
                is_solved = await page.evaluate("""() => {
                    const response = document.querySelector('[name="cf-turnstile-response"]');
                    return response && response.value && response.value.length > 10;
                }""")

                # 2. If NOT solved, try to click the checkbox autonomously
                if not is_solved:
                    click_res = await _click_turnstile_checkbox(page)
                    if click_res:
                        print(f">>> Turnstile challenge detected. Clicked {click_res}")
                    else:
                        print(">>> Turnstile challenge detected. Waiting for solve path")

                # 3. If solved and auto_submit is on, click the submit button
                if is_solved and auto_submit:

                    # If it's been more than 15s since last click, try reload or click
                    if submitted_at > 0 and (time.monotonic() - submitted_at > 15):
                        submit_reload_count += 1
                        if max_submit_reloads is not None and submit_reload_count > max_submit_reloads:
                            raise TimeoutError(
                                f"Challenge remained on interstitial after {submit_reload_count} post-submit reloads"
                            )
                        print(">>> Navigation hang detected. Reloading page..."); await page.reload(); await asyncio.sleep(3)
                        submitted_at = time.monotonic(); continue
                    
                    if submitted_at == 0:
                        print("Turnstile solved! Finding submit button..."); await asyncio.sleep(0.8)
                        click_res = await _submit_challenge_page(page)
                        if click_res:
                            print(f">>> Submission triggered via {click_res}."); 
                            if return_on_submit:
                                print(">>> Proceeding immediately (return_on_submit=True)"); return True
                            submitted_at = time.monotonic()
                            submit_reload_count = 0
                
                elapsed = int(elapsed_s)
                if elapsed > 0 and elapsed % 5 == 0:
                    status = "Solved, waiting nav" if is_solved else "Solve in Chrome"
                    print(f"  ... {elapsed}s, {status}, title: {title}")
                await asyncio.sleep(0.5); continue
            
            submitted_at = 0
            submit_reload_count = 0
            content_lower = content.lower()
            on_oscn_known_path = (
                "getcaseinformation.aspx" in url_lower
                or "getdocument.aspx" in url_lower
                or "results.aspx" in url_lower
            )
            no_turnstile_marker = (
                "turnstile" not in content_lower
                and "challenge-platform" not in content_lower
                and "just a moment" not in title.lower()
            )
            success_indicators = [
                target_text in content,
                "docketlist" in content_lower,
                "Case Information" in content,
                # Search-results-page structural markers (class names in
                # the HTML). Robust against title/text variations.
                "resulttablerow" in content_lower,
                "result_casenumber" in content_lower,
                "%PDF-" in content[:100],
                "getdocument.aspx" in url_lower and "turnstile" not in content_lower,
                # Fallback: we navigated to a known OSCN endpoint, the page
                # rendered a non-trivial body, and no challenge markers
                # remain. Catches search results, case info, and document
                # endpoints with layout variations we haven't seen.
                on_oscn_known_path and no_turnstile_marker and len(content) > 5000,
            ]

            if any(success_indicators):
                print(f"Challenge cleared! Detected: {title}")
                return True
                
        except Exception as e:
            if str(e) == "PAGE_CLOSED" and closed_ok:
                print("Challenge page closed; treating as success")
                return True
            if str(e) == "IP_RESTRICTED": raise e
            await asyncio.sleep(0.5); continue
        await asyncio.sleep(0.5)

async def download_pdf(page, action, dest_path, retain_exemplar=False):
    """Download a PDF; return telemetry dict for the caller to record.

    When `retain_exemplar` is True the saved PDF is NOT deleted after a
    successful OCR — used to keep one PDF exemplar per document type.

    Return shape:
        {"ok": bool, "mode": "session"|"click"|None, "elapsed_s": float,
         "error": str|None}

    Primary path: reuse the browser context's authenticated session to
    request the PDF directly with the same cookies and referer as the
    cleared case page. This avoids the transient `_blank` popup that OSCN
    sometimes interposes with a Turnstile page before eventually serving
    the file.

    Fallback path: if the direct fetch comes back as HTML instead of a PDF,
    click the rendered `a.doc-pdf` link, inspect the popup/new-tab challenge
    flow, and retry the in-session request once more after any Turnstile
    solve. We keep retries shallow so a single case cannot burn the IP's
    verification budget.
    """
    started = time.monotonic()

    def _result(ok, mode=None, error=None, ocr_task=None, popup_used=False):
        # ocr_task is an asyncio.Task[dict] when OCR was started, or None
        # otherwise. The case loop awaits it before writing the register
        # so the next download can proceed in parallel with OCR.
        return {
            "ok": ok,
            "mode": mode,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": error,
            "popup_used": popup_used,
            "ocr_task": ocr_task,
        }

    async def _run_ocr_and_finalize() -> dict:
        """OCR a freshly saved PDF, optionally drop the PDF on success.

        Returns a dict ready to merge into the download result. Fields:
            text_filename, text_chars, text_pages, text_letter_frac,
            text_extraction_status, text_extraction_elapsed_s, ocr_engine,
            text_extraction_error (only on failure).
        Mutates dest_path (writes .txt next to it; deletes .pdf on
        successful OCR unless KEEP_PDFS is True).
        """
        if not RUN_OCR:
            return {
                "text_extraction_status": "skipped",
                "text_filename": None,
                "doc_filename_kept": dest_path.name,
            }
        try:
            # Tesseract is CPU-bound and synchronous; offload to a thread
            # so the Playwright event loop stays responsive.
            ocr_res = await asyncio.to_thread(_ocr_pdf, dest_path)
        except Exception as e:
            return {
                "text_extraction_status": "error",
                "text_extraction_error": f"thread error: {str(e)[:200]}",
                "text_extraction_elapsed_s": 0.0,
                "doc_filename_kept": dest_path.name,
            }

        out = {
            "text_chars": ocr_res.get("chars", 0),
            "text_pages": ocr_res.get("pages", 0),
            "text_letter_frac": ocr_res.get("letter_frac", 0.0),
            "text_extraction_status": ocr_res.get("status", "error"),
            "text_extraction_elapsed_s": ocr_res.get("elapsed_s", 0.0),
            "ocr_engine": ocr_res.get("engine", "unknown"),
            "text_filename": None,
            "doc_filename_kept": dest_path.name,
        }
        if ocr_res.get("error"):
            out["text_extraction_error"] = str(ocr_res["error"])[:200]

        text = ocr_res.get("text") or ""
        if text:
            txt_path = dest_path.with_suffix(".txt")
            try:
                txt_path.write_text(text)
                out["text_filename"] = txt_path.name
            except Exception as e:
                out["text_extraction_error"] = f"write failed: {str(e)[:120]}"

        if out["text_extraction_status"] == "ok" and not KEEP_PDFS and not retain_exemplar:
            try:
                dest_path.unlink()
                out["doc_filename_kept"] = None
            except Exception:
                pass

        return out

    async with DOWNLOAD_SEMAPHORE:
        await asyncio.sleep(random.uniform(*PDF_REQUEST_JITTER))

        doc_url = action.get("doc_url") or ""
        doc_id = parse_qs(urlparse(doc_url).query).get("bc", [""])[0]
        if not doc_id:
            print(f"      {dest_path.name}: no bc= in doc_url; skipping")
            return _result(False, error="no_bc_in_url")

        # Prefer the explicit PDF anchor; fall back to any doc anchor.
        matches = page.locator(f'a.doc-pdf[href*="bc={doc_id}"]')
        if await matches.count() == 0:
            matches = page.locator(f'a[href*="bc={doc_id}"]')
        if await matches.count() == 0:
            print(f"      {dest_path.name}: link not found on page; skipping")
            return _result(False, error="link_not_found")
        link = matches.first

        async def attempt_session_request():
            try:
                response = await page.context.request.get(
                    doc_url,
                    headers={"referer": page.url},
                    fail_on_status_code=False,
                )
            except Exception as request_error:
                print(f"      {dest_path.name}: session request failed: {request_error}")
                return None

            content_type = (response.headers.get("content-type") or "").lower()
            if response.status == 200 and "application/pdf" in content_type:
                try:
                    dest_path.write_bytes(await response.body())
                    return True
                except Exception as save_error:
                    print(f"      {dest_path.name}: session save failed: {save_error}")
                    return False

            try:
                preview = (await response.text())[:200].replace("\n", " ")
            except Exception:
                preview = ""
            print(
                f"      {dest_path.name}: session request returned {response.status} "
                f"{content_type or '<no content-type>'}"
                + (f" ({preview})" if preview else "")
            )
            return False

        async def inspect_document_popup(popup):
            with contextlib.suppress(Exception):
                await popup.wait_for_load_state("domcontentloaded", timeout=7_500)
            try:
                await wait_for_human_solve(
                    popup,
                    target_text="PDF",
                    auto_submit=True,
                    return_on_submit=True,
                    closed_ok=True,
                    max_wait_s=15,
                    max_submit_reloads=1,
                )
            except Exception as popup_error:
                print(f"      {dest_path.name}: popup solve failed: {popup_error}")
            finally:
                # Always close the popup so tabs don't accumulate. Each
                # download spawns a fresh _blank popup; leaving them open
                # piles up CF state and slows everything to a crawl.
                # Brief settle so the cf_clearance cookie has time to land
                # on the shared browser context before we drop the tab.
                with contextlib.suppress(Exception):
                    await asyncio.sleep(1.5)
                    if not popup.is_closed():
                        await popup.close()

        async def attempt_click_download():
            download_task = asyncio.create_task(page.context.wait_for_event("download", timeout=90_000))
            popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=12_000))
            try:
                await link.click()
                popup = None
                try:
                    popup = await popup_task
                    print(f"      {dest_path.name}: document opened in popup; inspecting challenge flow")
                except Exception:
                    popup = None

                if popup:
                    await inspect_document_popup(popup)
                    if await attempt_session_request():
                        return ("session", None)
                    if download_task.done():
                        return ("download", download_task.result())
                    raise TimeoutError("Popup challenge did not unlock session PDF request")

                try:
                    return ("download", await download_task)
                except Exception:
                    if await attempt_session_request():
                        return ("session", None)
                    raise
            finally:
                for task in (download_task, popup_task):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

        session_ok = await attempt_session_request()
        if session_ok:
            ocr_task = asyncio.create_task(_run_ocr_and_finalize())
            return _result(True, mode="session", ocr_task=ocr_task)

        if not ENABLE_POPUP_FALLBACK:
            # Empirically the popup-CF "unlock" rarely makes the next
            # session request succeed; each cycle just burns an OSCN
            # verification credit. Record and move on.
            return _result(False, error="cf_gated_skipped")

        try:
            mode, download = await attempt_click_download()
        except Exception as e:
            content = await _page_content_or_blank(page)
            if "challenge-platform" in content or "Turnstile" in content:
                print(f"      {dest_path.name}: Turnstile fired on click; solving and retrying once")
                try:
                    await wait_for_human_solve(page, target_text="PDF", auto_submit=True)
                    mode, download = await attempt_click_download()
                except Exception as e2:
                    print(f"      {dest_path.name}: download failed after CF clear: {e2}")
                    return _result(False, error=f"cf_retry_failed: {str(e2)[:80]}", popup_used=True)
            else:
                print(f"      {dest_path.name}: download failed: {e}")
                return _result(False, error=f"click_download_failed: {str(e)[:80]}", popup_used=True)

        if mode == "session":
            ocr_task = asyncio.create_task(_run_ocr_and_finalize())
            return _result(True, mode="session", ocr_task=ocr_task, popup_used=True)

        try:
            await download.save_as(dest_path)
        except Exception as e:
            print(f"      {dest_path.name}: save failed: {e}")
            return _result(False, mode="click", error=f"save_failed: {str(e)[:80]}", popup_used=True)
        ocr_task = asyncio.create_task(_run_ocr_and_finalize())
        return _result(True, mode="click", ocr_task=ocr_task, popup_used=True)

async def scrape_case_detail(context, page, case_data, hint_filing_iso: str = ""):
    """Scrape one case's info page and write data/<filing_iso>/<case_num>/register_of_actions.json.

    `hint_filing_iso` is the search day we found this case on; used as a
    fallback bucket if the page itself doesn't surface a usable date.
    The case page's first docket entry is the preferred source of truth;
    if both are unavailable we fall back to the year embedded in the
    case number (e.g. 2024 from CJ-2024-1234).
    """
    case_num = case_data.get("case_num") or case_data.get("case_number")
    case_type = case_num.split("-")[0].upper() if case_num and "-" in case_num else ""
    case_started_at = utc_now_iso()
    case_started_perf = time.monotonic()
    print(f"  Scraping {case_num}...")
    if HEARTBEAT is not None:
        HEARTBEAT.update(current_case=case_num, current_action="case-start")
    await asyncio.sleep(random.uniform(*CASE_START_JITTER))
    try: await page.goto(case_data['url'], wait_until="commit", timeout=60000)
    except: pass

    await wait_for_human_solve(page, target_text="Case Information")

    # wait_for_human_solve declares the page cleared when it sees
    # "Case Information" in the document, but that phrase appears in a
    # header/breadcrumb that paints BEFORE table.docketlist renders.
    # We need TWO waits to avoid load-race short-circuits:
    #   1) table.caseStyle — header structure (fast).
    #   2) tr.docketRow.primary-entry OR "No Record." text — confirms
    #      the docket table is either populated OR genuinely empty.
    # Without (2) a busy case can render its caseStyle but have an
    # un-populated docketlist when we evaluate, producing a false
    # empty_docket marker.
    try:
        await page.wait_for_selector("table.caseStyle", timeout=8000)
    except Exception:
        pass
    try:
        await page.wait_for_selector("tr.docketRow.primary-entry", timeout=10000)
    except Exception:
        # Either a genuinely empty / "No Record" case (caseStyle
        # present, docket section blank) or a load that's too slow.
        # The parser below will distinguish: empty_docket short-circuit
        # only fires if caseStyle is present but actions is empty.
        pass

    data = await page.evaluate("""() => {
        // Find an h2.section by leading text (case-insensitive) and return
        // the next <table> sibling. Used to locate per-section tables
        // (Attorneys, Issues, Counts) without relying on table IDs that
        // may differ between case types.
        function tableAfterSection(headingText) {
            const h2s = Array.from(document.querySelectorAll('h2.section'));
            const target = headingText.toLowerCase();
            for (const h of h2s) {
                if (!h.innerText) continue;
                if (h.innerText.trim().toLowerCase().startsWith(target)) {
                    let sib = h.nextElementSibling;
                    while (sib && sib.tagName !== 'TABLE') sib = sib.nextElementSibling;
                    return sib;
                }
            }
            return null;
        }

        function clean(s) {
            return (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
        }

        // ---- Existing fields ----
        const dock = document.querySelector('table.docketlist');
        const styleTbl = document.querySelector('table.caseStyle');
        const docket_table_present = dock !== null;
        const case_style_present = styleTbl !== null;
        const actions = [];
        if (dock) {
            const rows = Array.from(dock.querySelectorAll('tr.docketRow, tr'));
            rows.forEach(row => {
                const tds = Array.from(row.querySelectorAll('td'));
                if (tds.length < 3) return;
                const date = tds[0].innerText.trim();
                if (!/^\\d{2}-\\d{2}-\\d{4}$/.test(date)) return;
                const code = tds[1].innerText.trim();
                const wrapper = tds[2].querySelector('.description-wrapper');
                let desc = wrapper ? wrapper.innerText.split('Document Available')[0].trim() : tds[2].innerText.split('Document Available')[0].trim();
                desc = desc.replace(/\\[(PDF|TIFF)\\]/gi, '').replace(/\\s+/g, ' ').trim();
                const pdfLink = row.querySelector('a.doc-pdf') || row.querySelector('a[href*="fmt=pdf"]');
                const genericLink = row.querySelector('a[href*="GetDocument.aspx"]');
                actions.push({ date, code, proceedings: desc, doc_url: (pdfLink || genericLink) ? (pdfLink || genericLink).href : null });
            });
        }
        let judge = '', style = '', filed = '', closed = '';
        if (styleTbl) {
            const tds = styleTbl.querySelectorAll('td');
            if (tds.length >= 2) {
                const meta = tds[1].innerText || '';
                const mJ = meta.match(/Judge:\\s*([^\\n]+)/i);
                const mF = meta.match(/Filed:\\s*([0-9\\/\\-]+)/i);
                const mC = meta.match(/Closed:\\s*([0-9\\/\\-]+)/i);
                judge = mJ ? mJ[1].trim() : '';
                filed = mF ? mF[1].trim() : '';
                closed = mC ? mC[1].trim() : '';
            }
            style = styleTbl.innerText.split('\\n')[0].trim();
        }

        // ---- Parties ----
        // Each party rendered as <span class="parties_party"> containing
        // <span class="parties_partyname">NAME</span> and
        // <span class="parties_type">Plaintiff/Defendant/etc</span>.
        const parties = Array.from(document.querySelectorAll('span.parties_party')).map(p => {
            const nm = p.querySelector('.parties_partyname');
            const tp = p.querySelector('.parties_type');
            return {
                name: nm ? clean(nm.innerText) : '',
                type: tp ? clean(tp.innerText) : '',
            };
        }).filter(p => p.name);

        // ---- Attorneys ----
        // Section header is <h2 class="section attorneys">; the next table
        // has a 2-column layout: left cell is "NAME(Bar #N)<br>...address...",
        // right cell is comma-separated represented parties.
        const attorneys = [];
        const attyTable = tableAfterSection('Attorney');
        if (attyTable) {
            const rows = Array.from(attyTable.querySelectorAll('tbody tr'));
            for (const tr of rows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length < 2) continue;
                const left = cells[0].innerText || '';
                const right = cells[1].innerText || '';
                const lines = left.split(/\\r?\\n+/).map(s => clean(s)).filter(Boolean);
                if (lines.length === 0) continue;
                const barMatch = left.match(/\\(Bar\\s*#\\s*(\\d+)\\)/i);
                const name = (lines[0] || '').replace(/\\(Bar\\s*#\\s*\\d+\\)/i, '').trim().replace(/[,\\s]+$/, '');
                // Address = the remaining lines (skip the firm-display line that's
                // often a duplicate of the name without (Bar #...))
                const address_lines = lines.slice(1).filter(l => l && l !== name);
                const reps = right.split(/[,\\n]+/).map(s => clean(s)).filter(Boolean);
                attorneys.push({
                    name: name,
                    bar_number: barMatch ? barMatch[1] : null,
                    address: address_lines.join(', '),
                    represented_parties: reps,
                });
            }
        }

        // ---- Issues (civil) / Counts (criminal) ----
        // Both render under the same h2.section.issues / .counts pattern.
        // We do a defensive cell-capture so downstream analysis can refine
        // without us needing per-type parsers in here.
        function extractRowList(headingText) {
            const tbl = tableAfterSection(headingText);
            if (!tbl) return [];
            const rows = Array.from(tbl.querySelectorAll('tr.docketRow, tr.primary-entry, tr'));
            const out = [];
            for (const tr of rows) {
                const cells = Array.from(tr.querySelectorAll('td')).map(td => clean(td.innerText));
                // Skip header rows or rows with nothing useful
                if (cells.every(c => !c)) continue;
                if (tr.querySelector('th')) continue;
                // Issue/count number from .count_issue if present
                const numEl = tr.querySelector('.count_issue');
                const number = numEl ? clean(numEl.innerText).replace(/\\s+/g, '') : '';
                const partyEl = tr.querySelector('.partyname, .countpartyname, .parties_partyname');
                const party = partyEl ? clean(partyEl.innerText) : '';
                out.push({ number, party, cells });
            }
            return out;
        }
        const issues = extractRowList('Issues');
        const counts = extractRowList('Counts');

        return {
            actions, judge, style, filed, closed,
            docket_table_present, case_style_present,
            parties, attorneys, issues, counts,
        };
    }""")

    # Sanity check: if neither structural marker rendered, the page
    # didn't finish loading (or hit some unrecognized layout). Don't
    # write a register record — leaving the case directory absent lets
    # auto-resume pick the case back up on the next run instead of
    # baking in a false "empty" marker.
    if not data.get('docket_table_present') and not data.get('case_style_present'):
        print(f"  {case_num}: case-info page never rendered; not writing register (will retry next run)")
        return None

    # Derive the filing_iso to bucket this case under. Priority:
    #   1. caseStyle's "Filed: MM/DD/YYYY" — the page's own canonical
    #      filing date. Use this ALWAYS when available; the first
    #      docket-action date is unreliable for older cases that have
    #      pre-1990 archived events.
    #   2. The search-day hint (the day we found this case on).
    #   3. First docket-action date as a last resort for cases whose
    #      header parse was incomplete.
    #   4. The year embedded in the case number.
    parsed_filed_iso = filed_to_iso(data.get('filed', '') or '')
    filing_iso = parsed_filed_iso or hint_filing_iso or ""
    if not filing_iso and data['actions']:
        filing_iso = filed_to_iso(data['actions'][0]['date'])
    if not filing_iso:
        m = re.search(r"\b(\d{4})\b", case_num or "")
        filing_iso = f"{m.group(1)}-01-01" if m else "0000-00-00"

    case_dir = case_dir_for(filing_iso, case_num)
    case_dir.mkdir(parents=True, exist_ok=True)

    # Party / attorney metadata is captured even on empty cases — a
    # "No Record." sealed case still has a parties block in some layouts,
    # and the rollups are cheap to compute either way.
    raw_parties = data.get("parties") or []
    raw_attorneys = data.get("attorneys") or []
    parties_annotated = annotate_pro_se(raw_parties, raw_attorneys)
    rollups = representation_rollups(parties_annotated, raw_attorneys)

    # Short-circuit: a real empty/no-record case shows the case-style
    # block (so we know the page rendered) but no rows in the docket
    # table, OR a "No Record." marker in the case style. Distinguishing
    # this from a load-race is what `docket_table_present` /
    # `case_style_present` are for above.
    style_lower = (data.get('style') or '').lower()
    is_no_record = "no record" in style_lower
    empty_case = (not data['actions']) or is_no_record
    if empty_case:
        reason = "no_record" if is_no_record else "empty_docket"
        print(f"  {case_num}: {reason}; skipping PDF loop")
        finished_at = utc_now_iso()
        elapsed_s = round(time.monotonic() - case_started_perf, 3)
        result = {
            "metadata": {
                "case_number": case_num,
                "case_type": case_type,
                "case_title": data.get('style', ''),
                "filing_date": filing_iso,
                "county": case_data.get("county", ""),
                "empty_case": True,
                "empty_reason": reason,
                "parties": parties_annotated,
                "attorneys": raw_attorneys,
                "issues": data.get("issues") or [],
                "counts": data.get("counts") or [],
                **rollups,
                "timing": {
                    "started_at": case_started_at,
                    "finished_at": finished_at,
                    "elapsed_seconds": elapsed_s,
                    "scraped_at": finished_at,
                    "downloaded_docs": 0,
                },
            },
            "actions": [],
        }
        with open(case_dir / "register_of_actions.json", "w") as f:
            json.dump(result, f, indent=2)
        return result

    final_actions = []
    downloaded = 0
    attempts = 0
    consecutive_gates = 0
    popup_fallbacks = 0
    case_info_refreshes = 0
    case_info_refresh_elapsed = 0.0
    capped = False
    download_telemetry = []  # per-attempt {mode, elapsed_s, error}
    # Pipelined-OCR queue: [(per_action, ocr_task), ...]. We start the OCR
    # task as soon as the download lands and let the next download proceed
    # in parallel; we await the tasks at end-of-case and merge their
    # results into the per_action records before writing the register.
    pending_ocr: list[tuple[dict, asyncio.Task]] = []
    for action in data['actions']:
        doc_filename = None
        per_action = {
            "date": action['date'],
            "code": action.get('code', ''),
            "proceedings": action['proceedings'],
            "fee": "",
            "doc_url": action['doc_url'],
            "doc_filename": None,
        }
        keep_by_filter = DISABLE_FILTER or is_high_value_by_code(
            action.get('code'), action.get('proceedings', '')
        )
        if action['doc_url'] and keep_by_filter and not capped:
            doc_id = parse_qs(urlparse(action['doc_url']).query).get('bc', ['doc'])[0]
            doc_filename = f"{action['date']}_{doc_id}.pdf"
            dest = case_dir / doc_filename
            txt_dest = dest.with_suffix(".txt")
            # Cache hit: either the PDF or its OCR'd .txt is already on
            # disk. The .txt alone is the typical post-migration state
            # (PDFs are deleted after successful OCR).
            if dest.exists() or (txt_dest.exists() and txt_dest.stat().st_size > 0):
                downloaded += 1
                per_action["doc_filename"] = doc_filename if dest.exists() else None
                if txt_dest.exists():
                    per_action["text_filename"] = txt_dest.name
                per_action["download_mode"] = "cached"
            elif not DISABLE_CAP and attempts >= PER_CASE_PDF_CAP:
                # Per-case cap reached. Defer remaining high-value docs.
                capped = True
                print(f"    Cap hit ({PER_CASE_PDF_CAP} PDFs); deferring remaining to retry pass")
            else:
                attempts += 1
                cap_label = "∞" if DISABLE_CAP else str(PER_CASE_PDF_CAP)
                print(f"    Target found ({attempts}/{cap_label}): {action['proceedings'][:60]}...")
                _code = (action.get('code') or '').strip()
                needs_exemplar = DOC_TYPE_SAMPLING and bool(_code) and _code not in EXEMPLAR_CLAIMED
                if needs_exemplar:
                    EXEMPLAR_CLAIMED.add(_code)
                result_dict = await download_pdf(page, action, dest, retain_exemplar=needs_exemplar)
                download_telemetry.append({
                    "doc_id": doc_id,
                    "mode": result_dict.get("mode"),
                    "elapsed_s": result_dict.get("elapsed_s"),
                    "ok": result_dict.get("ok"),
                    "error": result_dict.get("error"),
                })
                per_action["download_mode"] = result_dict.get("mode")
                per_action["download_elapsed_s"] = result_dict.get("elapsed_s")
                if result_dict.get("popup_used"):
                    popup_fallbacks += 1
                # If the first gate of this case fires and REFRESH_ON_GATE
                # is on, re-navigate to case-info to refresh CF cookies
                # and retry the same PDF once. CF empirically allows ~3
                # PDF requests per case-info-clear before gating.
                if (
                    REFRESH_ON_GATE
                    and not result_dict.get("ok")
                    and result_dict.get("error") == "cf_gated_skipped"
                    and consecutive_gates == 0
                ):
                    refresh_started = time.monotonic()
                    print(f"    Gate detected; refreshing case-info to renew CF and retrying once")
                    try:
                        await page.goto(case_data['url'], wait_until="commit", timeout=60000)
                    except Exception:
                        pass
                    try:
                        await wait_for_human_solve(page, target_text="Case Information")
                    except Exception as e:
                        print(f"    Case-info refresh failed: {e}; treating as gate")
                    refresh_elapsed = round(time.monotonic() - refresh_started, 2)
                    case_info_refreshes += 1
                    case_info_refresh_elapsed += refresh_elapsed
                    # Retry the download once.
                    result_dict = await download_pdf(page, action, dest, retain_exemplar=needs_exemplar)
                    download_telemetry.append({
                        "doc_id": doc_id,
                        "mode": result_dict.get("mode"),
                        "elapsed_s": result_dict.get("elapsed_s"),
                        "ok": result_dict.get("ok"),
                        "error": result_dict.get("error"),
                        "post_refresh": True,
                    })
                    per_action["download_mode"] = result_dict.get("mode")
                    per_action["download_elapsed_s"] = result_dict.get("elapsed_s")
                if result_dict.get("ok"):
                    downloaded += 1
                    if HEARTBEAT is not None:
                        HEARTBEAT.increment("session_docs_collected")
                    # Provisional: assume PDF was kept on disk. The OCR
                    # finalize pass below replaces this if it was deleted
                    # after successful OCR.
                    per_action["doc_filename"] = doc_filename
                    consecutive_gates = 0
                    ocr_task = result_dict.get("ocr_task")
                    if ocr_task is not None:
                        pending_ocr.append((per_action, ocr_task))
                else:
                    per_action["download_error"] = result_dict.get("error")
                    consecutive_gates += 1
                    if needs_exemplar:
                        EXEMPLAR_CLAIMED.discard(_code)
                if popup_fallbacks >= MAX_POPUP_FALLBACKS_PER_CASE and not capped:
                    capped = True
                    print(f"    Popup-fallback cap hit ({popup_fallbacks}); CF reputation "
                          "degrading on this case — deferring remaining to retry pass")
                if consecutive_gates >= MAX_CONSECUTIVE_GATES and not capped:
                    capped = True
                    print(f"    Circuit breaker: {consecutive_gates} consecutive failures; "
                          f"deferring remaining to retry pass")
        final_actions.append(per_action)

    # Drain pipelined OCR tasks and merge their results into the per-action
    # dicts before we serialize the register. asyncio.gather lets the
    # remaining OCR threads finish in parallel; the case-end wait is then
    # bounded by the slowest OCR rather than the sum.
    if pending_ocr:
        ocr_results = await asyncio.gather(
            *(t for _, t in pending_ocr), return_exceptions=True
        )
        for (per_action, _), ocr_result in zip(pending_ocr, ocr_results):
            if isinstance(ocr_result, BaseException):
                ocr_result = {
                    "text_extraction_status": "error",
                    "text_extraction_error": f"task failure: {str(ocr_result)[:160]}",
                    "text_extraction_elapsed_s": 0.0,
                }
            for k in ("text_filename", "text_chars", "text_pages",
                      "text_letter_frac", "text_extraction_status",
                      "text_extraction_elapsed_s", "ocr_engine",
                      "text_extraction_error"):
                if k in ocr_result:
                    per_action[k] = ocr_result[k]
            # Finalize doc_filename based on whether OCR kept/deleted the PDF.
            kept_pdf_name = ocr_result.get("doc_filename_kept")
            if "doc_filename_kept" in ocr_result and kept_pdf_name is None:
                per_action["doc_filename"] = None

    if DOC_TYPE_SAMPLING:
        update_doc_type_dict(final_actions, filing_iso, case_num)
        write_doc_type_dict()
    write_doctype_review_needed()

    finished_at = utc_now_iso()
    elapsed_s = round(time.monotonic() - case_started_perf, 3)
    # Roll up per-mode timing for quick scanning in stats
    mode_summary = {}
    for t in download_telemetry:
        m = t.get("mode") or "fail"
        if m not in mode_summary:
            mode_summary[m] = {"count": 0, "ok": 0, "total_s": 0.0}
        mode_summary[m]["count"] += 1
        mode_summary[m]["ok"] += 1 if t.get("ok") else 0
        mode_summary[m]["total_s"] += t.get("elapsed_s") or 0.0

    result = {
        "metadata": {
            "case_number": case_num,
            "case_type": case_type,
            "case_title": data['style'],
            "filing_date": filing_iso,
            "judge": data.get('judge', ''),
            "filed": data.get('filed', ''),
            "closed": data.get('closed', ''),
            "county": case_data.get("county", ""),
            "empty_case": False,
            "parties": parties_annotated,
            "attorneys": raw_attorneys,
            "issues": data.get("issues") or [],
            "counts": data.get("counts") or [],
            **rollups,
            "timing": {
                "started_at": case_started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed_s,
                "scraped_at": finished_at,
                "downloaded_docs": downloaded,
                "attempts": attempts,
                "capped": capped,
                "consecutive_gates_at_end": consecutive_gates,
                "download_mode_summary": mode_summary,
                "case_info_refreshes": case_info_refreshes,
                "case_info_refresh_elapsed_s": round(case_info_refresh_elapsed, 2),
            },
        },
        "actions": final_actions,
    }
    with open(case_dir / "register_of_actions.json", "w") as f:
        json.dump(result, f, indent=2)
    if HEARTBEAT is not None:
        HEARTBEAT.increment("session_cases_scraped")
    return result

async def scrape_one_day(context, page, county: str, types: list[str],
                         filing_iso: str) -> None:
    """Search OSCN for one filing day across all requested types and scrape each case.

    Writes day_summary.json + failed_cases.json under data/<filing_iso>/.
    Per-case scrape goes through scrape_case_detail which buckets each
    case under its OWN page-asserted filing date — this means a case
    discovered by the search but actually filed on a different date
    will land in that other day's folder (and is reported here as a
    cross-day case).
    """
    started_at = utc_now_iso()
    started_perf = time.monotonic()
    print(f"\nProcessing {filing_iso}  county={county}  types={','.join(types)}")

    # --- Search phase ---
    # OSCN's `dcct` param filters server-side, so we issue one search
    # per type and combine. Each per-type search has its own 500-row
    # response budget, sidestepping the cap that an all-types search
    # would saturate on busy Tulsa days.
    manifest: list[dict] = []
    raw_by_type: dict[str, int] = {}
    try:
        manifest, raw_by_type = await search_one_day(page, county, types, filing_iso)
    except Exception as e:
        err_text = str(e)
        print(f"  search failed: {err_text[:200]}")
        if err_text == "IP_RESTRICTED":
            raise

    per_type_kept = {t.upper(): sum(1 for c in manifest if c["case_type"] == t.upper()) for t in types}
    total_raw = sum(raw_by_type.values())
    print(f"  total: raw={total_raw}, kept={len(manifest)}  per_type_kept={per_type_kept}")

    update_day_summary(
        filing_iso,
        total_cases=len(manifest),
        raw_by_type=raw_by_type,
        per_type_kept=per_type_kept,
    )

    if not manifest:
        update_day_summary(
            filing_iso,
            scraped_cases=0,
            failed_cases=0,
            run_metadata={
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "elapsed_seconds": round(time.monotonic() - started_perf, 2),
                "no_cases_found": True,
                "county": county,
                "case_types": types,
            },
        )
        print(f"  {filing_iso}: no cases after filter; recorded zero-case day")
        return

    # --- Scrape phase ---
    pending = [c for c in manifest if not case_is_complete(filing_iso, c["case_number"])]
    print(f"  scraping {len(pending)} of {len(manifest)} cases (rest already complete)")

    failures: list[dict] = []
    for case in pending:
        case_data = {
            "case_num": case["case_number"],
            "url": case["url"],
            "county": county,
        }
        try:
            await scrape_case_detail(context, page, case_data, hint_filing_iso=filing_iso)
        except Exception as e:
            err_text = str(e)
            print(f"  {case['case_number']}: {err_text[:200]}")
            failures.append({**case, "error": err_text[:300]})
            if err_text == "IP_RESTRICTED":
                # Persist what we've got and re-raise so the outer loop bails.
                write_failed_cases(filing_iso, failures)
                raise

    write_failed_cases(filing_iso, failures)

    # Cross-day audit: count cases whose actual filing_date matched the
    # searched day vs. those that were bucketed elsewhere.
    in_day = sum(1 for c in manifest if case_is_complete(filing_iso, c["case_number"]))
    cross_day = len(manifest) - in_day - len(failures)

    update_day_summary(
        filing_iso,
        total_cases=len(manifest),
        scraped_cases=in_day,
        cross_day_cases=cross_day,
        failed_cases=len(failures),
        run_metadata={
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "elapsed_seconds": round(time.monotonic() - started_perf, 2),
            "county": county,
            "case_types": types,
        },
    )
    if cross_day:
        print(f"  note: {cross_day} cases had Filed dates outside {filing_iso}; "
              f"written to their actual filing-day folders")


async def run_scraper_loop(args, context, page, dates: list[date]):
    """Iterate the assigned weekdays and scrape each."""
    types = [t.strip().upper() for t in args.type.split(",") if t.strip()]
    for d in dates:
        iso = d.isoformat()
        if HEARTBEAT is not None:
            HEARTBEAT.update(current_day=iso, current_case=None,
                             current_action="day-start")
        if day_is_complete(iso):
            print(f"  {iso}: already complete, skipping (use --force to re-scrape)")
            continue
        try:
            await scrape_one_day(context, page, args.county, types, iso)
        except Exception as e:
            if str(e) == "IP_RESTRICTED":
                print("Stopping run: IP needs to clear before resuming.")
                if HEARTBEAT is not None:
                    HEARTBEAT.update(current_action="ip_restricted",
                                     last_error="IP_RESTRICTED")
                return
            print(f"  {iso}: aborted: {str(e)[:200]}")


async def main():
    import argparse, sys
    parser = argparse.ArgumentParser(description=
        "OSCN docket scraper. Iterates weekdays in [--start-date, --end-date], "
        "searches each by case-type, and saves per-case data under "
        "data/YYYY-MM-DD/<CASE-NUMBER>/.")
    parser.add_argument("--start-date", required=True,
                        help="Inclusive filing-date start (YYYY-MM-DD). Weekdays only.")
    parser.add_argument("--end-date", required=True,
                        help="Inclusive filing-date end (YYYY-MM-DD).")
    parser.add_argument("--county", default="tulsa",
                        choices=("tulsa", "oklahoma"),
                        help="OSCN db parameter (county).")
    parser.add_argument("--type", default=",".join(DEFAULT_TYPES),
                        help="Comma-separated case-type prefixes to scrape per day, e.g. CJ,CV,CF,CM.")
    parser.add_argument("--chrome", action="store_true",
                        help="Fall back to attaching to system Chrome via CDP (default: Camoufox).")
    parser.add_argument("--workers", type=int, default=1,
                        help="Spawn N parallel scraper processes (each with its own Camoufox). "
                             "Each child gets a contiguous slice of the date range.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore day_is_complete; re-scrape even completed days.")
    parser.add_argument("--keep-pdfs", action="store_true",
                        help="Retain PDFs after successful OCR (default: delete to save space).")
    parser.add_argument("--no-ocr", action="store_true",
                        help="Skip the inline OCR pass; keep PDFs as-is. Useful for debugging.")
    parser.add_argument("--no-filter", action="store_true",
                        help="Disable the high-value filter; attempt every doc-bearing action "
                             "(still bounded by PER_CASE_PDF_CAP). Useful for surveying what OSCN serves.")
    parser.add_argument("--no-cap", action="store_true",
                        help=f"Disable the per-case PDF cap (default {PER_CASE_PDF_CAP}). "
                             "MAX_CONSECUTIVE_GATES still aborts mid-case on CF cascades. "
                             "Risk: mega-cases drive up download volume and CF gate exposure.")
    parser.add_argument("--enable-popup-fallback", action="store_true",
                        help="Re-enable the click-popup fallback when a session request returns "
                             "a CF gate. Default off — empirically the popup unlock rarely "
                             "yields the doc and burns OSCN's verification budget.")
    parser.add_argument("--data-root", default=None,
                        help="Override the output root (default: ok_scraper/data). "
                             "Useful for writing to an external drive.")
    parser.add_argument("--refresh-on-gate", action="store_true",
                        help="On the first CF gate in a case, re-navigate to the case-info "
                             "page to renew CF cookies and retry the same PDF once. Trades "
                             "an extra Turnstile solve for more PDFs per case.")
    parser.add_argument("--worker-id", type=int, default=None,
                        help="Internal: identifies this child when run under --workers N. "
                             "Used to name distinct heartbeat files in the monitor.")
    parser.add_argument("--doc-type-samples", action="store_true",
                        help="Build a per-worker document-type dictionary keyed by OSCN docket "
                             "code (<data_root>/_doc_type_dictionary.<worker>.json) and retain one "
                             "PDF exemplar per code (skips post-OCR delete for the first doc of each "
                             "code). For bootstrapping a doc-type filter; pair with --no-filter --no-cap.")
    parser.add_argument("--doctype-annotations", type=Path, default=None,
                        help="Use an annotated document-type dictionary/CSV to decide which docket "
                             "codes to download. Unknown/unannotated codes are still downloaded and "
                             f"flagged in <data_root>/_doc_type_review_needed.<worker>.json.")
    parser.add_argument("--pdf-request-jitter", default="0.05,0.2",
                        help="Seconds to sleep before each PDF request as min,max. Default: 0.05,0.2.")
    parser.add_argument("--case-start-jitter", default="0.1,0.3",
                        help="Seconds to sleep before each case-info navigation as min,max. Default: 0.1,0.3.")
    args = parser.parse_args()

    # Propagate runtime toggles to module-level state read by download_pdf.
    global RUN_OCR, KEEP_PDFS, DISABLE_FILTER, DISABLE_CAP, ENABLE_POPUP_FALLBACK, DATA_ROOT, REFRESH_ON_GATE
    global DOCTYPE_ANNOTATIONS
    global DOC_TYPE_SAMPLING, WORKER_TAG, PDF_REQUEST_JITTER, CASE_START_JITTER
    RUN_OCR = not args.no_ocr
    KEEP_PDFS = args.keep_pdfs or args.no_ocr
    DISABLE_FILTER = args.no_filter
    DISABLE_CAP = args.no_cap
    ENABLE_POPUP_FALLBACK = args.enable_popup_fallback
    REFRESH_ON_GATE = args.refresh_on_gate
    DOC_TYPE_SAMPLING = args.doc_type_samples
    WORKER_TAG = f"worker{args.worker_id}" if args.worker_id is not None else "main"
    try:
        PDF_REQUEST_JITTER = tuple(float(x) for x in args.pdf_request_jitter.split(",", 1))
        CASE_START_JITTER = tuple(float(x) for x in args.case_start_jitter.split(",", 1))
        if len(PDF_REQUEST_JITTER) != 2 or len(CASE_START_JITTER) != 2:
            raise ValueError
        if (
            PDF_REQUEST_JITTER[0] < 0
            or CASE_START_JITTER[0] < 0
            or PDF_REQUEST_JITTER[0] > PDF_REQUEST_JITTER[1]
            or CASE_START_JITTER[0] > CASE_START_JITTER[1]
        ):
            raise ValueError
    except Exception:
        raise SystemExit("--pdf-request-jitter and --case-start-jitter must be nonnegative min,max seconds")
    if args.data_root:
        DATA_ROOT = Path(args.data_root).expanduser().resolve()
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        print(f"Using data root: {DATA_ROOT}")
    if DOC_TYPE_SAMPLING:
        load_doc_type_dict()
        print(f"Doc-type sampling ON -> {_doc_type_dict_path()} "
              f"({len(DOC_TYPE_DICT)} codes already known)")
    if args.doctype_annotations:
        ann_path = args.doctype_annotations.expanduser().resolve()
        DOCTYPE_ANNOTATIONS = load_doctype_annotations(ann_path)
        annotated = sum(1 for v in DOCTYPE_ANNOTATIONS.values() if v is not None)
        high = sum(1 for v in DOCTYPE_ANNOTATIONS.values() if v is True)
        print(f"Doc-type code filter ON -> {ann_path} "
              f"({annotated}/{len(DOCTYPE_ANNOTATIONS)} annotated, {high} high-value)")

    # Resolve the weekday set
    all_weekdays = weekday_dates(args.start_date, args.end_date)
    if not all_weekdays:
        print(f"No weekdays in [{args.start_date}, {args.end_date}]; nothing to do.")
        return

    if args.force:
        dates_to_scrape = all_weekdays
    else:
        dates_to_scrape = find_resume_dates(args.start_date, args.end_date)
        skipped = len(all_weekdays) - len(dates_to_scrape)
        if skipped:
            print(f"Auto-resume: {skipped} of {len(all_weekdays)} weekdays already complete; "
                  f"will scrape {len(dates_to_scrape)} remaining")

    if not dates_to_scrape:
        print("Nothing to do (all requested weekdays are already complete; pass --force to override).")
        return

    # --- Multi-worker dispatcher ---
    if args.workers > 1:
        if args.chrome:
            print("WARNING: --chrome shares one debug port; running multiple --workers with "
                  "--chrome will collide. Set --workers 1 with --chrome.")
            return
        log_dir = DATA_ROOT / "_worker_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        chunks = split_dates_into_chunks(dates_to_scrape, args.workers)
        children = []
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            chunk_start = chunk[0].isoformat()
            chunk_end = chunk[-1].isoformat()
            log_path = log_dir / f"worker_{i}_{chunk_start}_to_{chunk_end}.log"
            cmd = [
                sys.executable, "-u",
                sys.argv[0],
                "--start-date", chunk_start,
                "--end-date", chunk_end,
                "--county", args.county,
                "--type", args.type,
            ]
            if args.force:
                cmd.append("--force")
            if args.keep_pdfs:
                cmd.append("--keep-pdfs")
            if args.no_ocr:
                cmd.append("--no-ocr")
            if args.no_filter:
                cmd.append("--no-filter")
            if args.no_cap:
                cmd.append("--no-cap")
            if args.enable_popup_fallback:
                cmd.append("--enable-popup-fallback")
            if args.data_root:
                cmd.extend(["--data-root", str(DATA_ROOT)])
            if args.refresh_on_gate:
                cmd.append("--refresh-on-gate")
            if args.doc_type_samples:
                cmd.append("--doc-type-samples")
            if args.doctype_annotations:
                cmd.extend(["--doctype-annotations", str(args.doctype_annotations)])
            cmd.extend(["--pdf-request-jitter", args.pdf_request_jitter])
            cmd.extend(["--case-start-jitter", args.case_start_jitter])
            cmd.extend(["--worker-id", str(i)])
            log_f = open(log_path, "w")
            proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
            children.append((i, proc, log_path, log_f, chunk_start, chunk_end))
            print(f"  worker {i}: {chunk_start} .. {chunk_end} "
                  f"({len(chunk)} weekdays) -> pid {proc.pid}, log {log_path}")
        if not children:
            print("Nothing to dispatch.")
            return
        print(f"Dispatched {len(children)} workers. Waiting for completion...")
        for i, proc, log_path, log_f, s, e in children:
            rc = proc.wait()
            log_f.close()
            print(f"  worker {i} ({s}..{e}): exit {rc}  (see {log_path})")
        print("All workers finished.")
        return

    global HEARTBEAT
    HEARTBEAT = Heartbeat(
        DATA_ROOT, scraper="ok",
        args=sys.argv[1:],
        worker_id=args.worker_id,
    )
    HEARTBEAT.update(
        # Run intent — static for the run's lifetime, so the monitor can
        # show "OK Tulsa CJ,CV,CF,CM 2025-01-01..2025-12-31, no-filter,
        # no-cap, refresh-on-gate" at a glance.
        start_date=args.start_date, end_date=args.end_date,
        county=args.county, types=args.type,
        dates_to_scrape=len(dates_to_scrape),
        no_filter=DISABLE_FILTER, no_cap=DISABLE_CAP,
        refresh_on_gate=REFRESH_ON_GATE,
        popup_fallback=ENABLE_POPUP_FALLBACK,
        rotation_managed=rotation_managed(),
        current_ip=probe_public_ip(),
        # Session counters (cumulative across days in this process; the
        # monitor renders them next to current_day/case).
        session_cases_scraped=0,
        session_docs_collected=0,
    )
    HEARTBEAT.start()
    try:
        if args.chrome:
            # CDP-attached system Chrome (debugging fallback). Expect more CF gates.
            try:
                pids = subprocess.check_output(f"lsof -i :{DEBUG_PORT} -t", shell=True).decode().split()
                for pid in pids: os.kill(int(pid), 15); time.sleep(2)
            except: pass
            launch_chrome()
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                await run_scraper_loop(args, context, page, dates_to_scrape)
                await browser.close()
            HEARTBEAT.close(status="exited", finished_reason="chrome-path-completed")
            return

        # Default: Camoufox (anti-fingerprint Playwright Firefox build).
        if not CAMOUFOX_AVAILABLE:
            print("Error: Camoufox not installed in this venv.")
            print("Install with: pip install 'camoufox[geoip]'")
            print("Or use --chrome to fall back to system Chrome via CDP (degraded gate-clearance).")
            HEARTBEAT.close(status="crashed", finished_reason="camoufox-missing")
            return
        print(f"Launching Camoufox; will process {len(dates_to_scrape)} weekday(s) "
              f"({dates_to_scrape[0]} .. {dates_to_scrape[-1]})")
        async with AsyncCamoufox(
            headless=False,
            os="macos",
            humanize=True,
        ) as browser:
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()
            await run_scraper_loop(args, context, page, dates_to_scrape)
        HEARTBEAT.close(status="exited", finished_reason="completed")
    except Exception as exc:
        HEARTBEAT.close(status="crashed", finished_reason=str(exc)[:200])
        raise


if __name__ == "__main__":
    asyncio.run(main())
