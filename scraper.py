import asyncio
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

# --- Configuration ---
DEBUG_PORT = 9223
CHROME_PROFILE = Path.home() / ".ok_manual_profile"
BASE_URL = "https://www.oscn.net/dockets"
SEARCH_URL = f"{BASE_URL}/Results.aspx"
CASE_URL = f"{BASE_URL}/GetCaseInformation.aspx"
DOC_URL = f"{BASE_URL}/GetDocument.aspx"
DATA_ROOT = Path("ok_scraper/data")

# --- High-Value Document Filters (Mirrored from SF Scraper + OK Specifics) ---
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


# --- Date / path helpers ---


def utc_now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def filed_to_iso(filed_str: str) -> str:
    """Convert OSCN's 'MM/DD/YYYY' or 'MM-DD-YYYY' to 'YYYY-MM-DD'. Empty on failure."""
    if not filed_str:
        return ""
    s = filed_str.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def iso_to_oscn(iso_or_date) -> str:
    if isinstance(iso_or_date, date):
        return iso_or_date.strftime("%m/%d/%Y")
    return datetime.strptime(iso_or_date, "%Y-%m-%d").strftime("%m/%d/%Y")


def weekday_dates(start_iso: str, end_iso: str):
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end = datetime.strptime(end_iso, "%Y-%m-%d").date()
    cur = start
    out = []
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            out.append(cur)
        cur += timedelta(days=1)
    return out


def safe_case_dirname(case_num: str) -> str:
    return case_num.replace("/", "_").replace(":", "_")


def day_dir(filing_iso: str) -> Path:
    return DATA_ROOT / filing_iso


def case_dir_for(filing_iso: str, case_num: str) -> Path:
    return day_dir(filing_iso) / safe_case_dirname(case_num)


def case_is_complete(filing_iso: str, case_num: str) -> bool:
    return (case_dir_for(filing_iso, case_num) / "register_of_actions.json").exists()


def doc_id_from_url(url: str) -> str:
    if not url:
        return ""
    qs = parse_qs(urlparse(url).query)
    for key in ("bc", "BC", "docid", "DocID"):
        if key in qs and qs[key]:
            return qs[key][0]
    return ""


def safe_filename(text: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "", text or "")
    cleaned = re.sub(r"\s+", "_", cleaned).strip("_")
    return cleaned[:max_len] or "doc"


# --- Per-day state ---


def update_day_summary(filing_iso: str, **fields) -> dict:
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


def write_failed_cases(filing_iso: str, failed: list) -> None:
    path = day_dir(filing_iso) / "failed_cases.json"
    if not failed:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(failed, indent=2))


def load_failed_cases(filing_iso: str) -> list:
    path = day_dir(filing_iso) / "failed_cases.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def launch_chrome():
    """Launch a real Chrome instance with remote debugging."""
    CHROME_PROFILE.mkdir(exist_ok=True)
    try:
        subprocess.check_output(f"lsof -i :{DEBUG_PORT}", shell=True)
        return
    except subprocess.CalledProcessError:
        pass

    print(f"Launching Google Chrome on port {DEBUG_PORT}...")
    cmd = [
        "open", "-g", "-na", "Google Chrome",
        "--args",
        f"--user-data-dir={CHROME_PROFILE}",
        f"--remote-debugging-port={DEBUG_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=DownloadBubble,DownloadBubbleV2",
    ]
    subprocess.Popen(cmd)
    time.sleep(3)

async def wait_for_human_solve(page, target_text="Case Search Results"):
    """Pause until the user solves the Cloudflare challenge, then auto-submit if possible."""
    print(f"Waiting for human solve (detecting: '{target_text}')...")
    start_wait = time.time()
    submitted = False
    
    while True:
        try:
            title = await page.title()
            content = await page.content()
            
            # CRITICAL: Detect if we are actually IP Restricted
            if "restiction may be caused by the following" in content or "Temporary restiction expires" in content:
                print("\n" + "!"*60)
                print("FATAL ERROR: YOUR IP ADDRESS IS RESTRICTED BY OSCN.")
                print("The scraper cannot proceed. Please stop the script and wait for the")
                print("timer in your browser to expire before trying again.")
                print("!"*60 + "\n")
                raise Exception("IP_RESTRICTED")

            # Identify if we are still on the challenge page
            is_challenged = (
                "Turnstile" in title 
                or "Why am I seeing this?" in content 
                or "challenge-platform" in content
            )
            
            if is_challenged:
                # Check if Turnstile has been solved but is waiting for a click
                is_solved = await page.evaluate("""() => {
                    const response = document.querySelector('[name="cf-turnstile-response"]');
                    return response && response.value && response.value.length > 0;
                }""")
                
                if is_solved and not submitted:
                    print("Turnstile solution detected! Attempting automatic submission...")
                    await page.evaluate("""() => {
                        const response = document.querySelector('[name="cf-turnstile-response"]');
                        const form = response ? response.closest('form') : null;
                        if (form) {
                            form.submit();
                            return true;
                        }
                        const btn = document.querySelector('input[type="submit"], button[type="submit"], #btnSearch');
                        if (btn) {
                            btn.click();
                            return true;
                        }
                        return false;
                    }""")
                    submitted = True
                    print("Submission command sent. Waiting for navigation...")
                    await asyncio.sleep(5)
                    continue

                if int(time.time() - start_wait) % 15 == 0:
                    print(f"\\n>>> CHALLENGE DETECTED: Please solve Turnstile in Chrome. (Title: {title})")
                await asyncio.sleep(2)
                continue
            
            # Reset submitted flag if we are no longer on a challenge page
            submitted = False
            
            # Detection criteria for a successful page
            success_indicators = [
                target_text in content,
                "Case Search" in title,
                "docketlist" in content.lower(),
                "caselist" in content.lower(),
                "Style" in content and "Date" in content,
                "Case Information" in content
            ]
            
            if any(success_indicators):
                print(f"Challenge cleared! Detected: {title}")
                return True
                
        except Exception as e:
            if str(e) == "IP_RESTRICTED":
                raise e
            await asyncio.sleep(1)
            continue
        
        await asyncio.sleep(2)

async def scrape_search_results(page, county, case_types, start_date, end_date):
    # case_types can be a comma-separated string like "CJ,CV,CS"
    type_list = [t.strip().upper() for t in case_types.split(",")]
    # OSCN URL usually takes one primary type, we'll use the first but filter for all in the results
    primary_type = type_list[0]
    
    url = f"{SEARCH_URL}?db={county}&type={primary_type}&filedstart={start_date}&filedend={end_date}"
    print(f"Navigating to Search: {url}")
    
    try:
        await page.goto(url, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"Initial navigation warning: {e}")
        
    await wait_for_human_solve(page, target_text="Case Search Results")
    
    # Extract case links from the structured results table.
    # OSCN renders search hits as <tr class="resultTableRow {even,odd}Row">
    # with td.result_casenumber, td.result_shortstyle, td.result_info.
    # Cases that come back with style "No Record." are sealed/expunged
    # and have empty case-info pages; skip them at search time.
    cases = await page.evaluate("""(allowedTypes) => {
        const rows = Array.from(document.querySelectorAll('tr.resultTableRow'));
        const out = [];
        for (const row of rows) {
            const numA = row.querySelector('td.result_casenumber a');
            if (!numA) continue;
            const caseNum = numA.innerText.trim();
            if (!caseNum) continue;
            const prefix = caseNum.split('-')[0].toUpperCase();
            if (!allowedTypes.includes(prefix)) continue;

            const styleEl = row.querySelector('td.result_shortstyle');
            const style = styleEl ? styleEl.innerText.replace(/\\s+/g, ' ').trim() : '';
            if (/no record\\.?/i.test(style)) continue;  // sealed/expunged

            const dateEl = row.querySelector('td.result_datefiled');
            const dateText = dateEl ? dateEl.innerText.trim() : '';

            out.push({
                case_num: caseNum,
                url: numA.href,
                style: style,
                date_text: dateText,
            });
        }
        return out;
    }""", type_list)
    
    # De-duplicate
    seen = set()
    unique_cases = []
    for c in cases:
        if c['case_num'] not in seen:
            unique_cases.append(c)
            seen.add(c['case_num'])
            
    if not unique_cases:
        print(f"WARNING: Found 0 cases matching {type_list} for these dates.")
        print("Try a wider date range or adding more types (e.g. --type CJ,CV,CS)")
    else:
        print(f"Found {len(unique_cases)} relevant cases matching {type_list}.")
        
    return unique_cases

# --- Globals ---
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)  # Sequential downloads to stay quiet

async def download_pdf(context, page, url, dest_path):
    """Download a PDF using the browser's context with challenge detection."""
    async with DOWNLOAD_SEMAPHORE:
        for attempt in range(2):
            try:
                # Add a small random sleep to look more human
                await asyncio.sleep(random.uniform(1.5, 3.5))
                
                # Perform the fetch
                response = await context.request.get(url)
                
                # Check for Cloudflare challenge (OSCN returns 201 or 403 for challenges in requests)
                if response.status in [201, 403, 503]:
                    print(f"      Cloudflare challenge detected during download (Status {response.status}).")
                    print("      Switching to browser tab to refresh clearance...")
                    
                    # Open the URL in the main page to trigger the human challenge
                    await page.goto(url)
                    await wait_for_human_solve(page, target_text="PDF")
                    
                    # If we got through, the next iteration will retry the request with new cookies
                    continue

                if response.status == 200:
                    content = await response.body()
                    if content.startswith(b"%PDF-"):
                        with open(dest_path, "wb") as f:
                            f.write(content)
                        return True
                    else:
                        print(f"      Invalid PDF content from {url} (Challenge page?)")
                else:
                    print(f"      Download failed (HTTP {response.status}): {url}")
                    
            except Exception as e:
                print(f"      Request failed: {e}")
                
        return False

CASE_DETAIL_PARSE_JS = """
() => {
    // Authoritative case metadata from OSCN's embedded JSON block.
    let jsonMeta = {};
    const jsonStyleEl = document.getElementById('json_style');
    if (jsonStyleEl) {
        try { jsonMeta = JSON.parse(jsonStyleEl.textContent.trim()); } catch (_) {}
    }

    // Filed/Closed/Judge from the second TD of <table class="caseStyle">.
    let filed = '', closed = '', judge = '', meta_text = '';
    const styleTbl = document.querySelector('table.caseStyle');
    if (styleTbl) {
        const tds = styleTbl.querySelectorAll('td');
        if (tds.length >= 2) {
            meta_text = tds[1].innerText.replace(/\\s+/g, ' ').trim();
            const m1 = meta_text.match(/Filed:\\s*([0-9\\/\\-]+)/i);
            const m2 = meta_text.match(/Closed:\\s*([0-9\\/\\-]+)/i);
            const m3 = meta_text.match(/Judge:\\s*([^\\n]+?)(?:\\s*$)/i);
            filed = m1 ? m1[1].trim() : '';
            closed = m2 ? m2[1].trim() : '';
            judge = m3 ? m3[1].trim() : '';
        }
    }

    // Docket rows. tr.docketRow.primary-entry isolates real events
    // and excludes non-event rows that sit inside the same table.
    const rows = Array.from(document.querySelectorAll('tr.docketRow.primary-entry'));
    const dateRe = /^\\d{2}-\\d{2}-\\d{4}$/;
    const entries = rows.map((tr) => {
        const tds = Array.from(tr.querySelectorAll('td'));
        const date = (tds[0] ? tds[0].innerText : '').replace(/\\u00a0/g, '').trim();
        const codeEl = tds[1] ? tds[1].querySelector('.docket_code') : null;
        const code = codeEl ? codeEl.innerText.trim() : (tds[1] ? tds[1].innerText.trim() : '');
        let description = '';
        const descWrapper = tds[2] ? tds[2].querySelector('.description-wrapper') : null;
        if (descWrapper) {
            const firstP = descWrapper.querySelector('p');
            description = firstP ? firstP.innerText.replace(/\\s+/g, ' ').trim() : '';
            if (!description) {
                description = descWrapper.innerText
                    .split('Document Available')[0]
                    .replace(/\\s+/g, ' ').trim();
            }
        } else if (tds[2]) {
            description = tds[2].innerText
                .split('Document Available')[0]
                .replace(/\\s+/g, ' ').trim();
        }
        const pdfLink = tr.querySelector('a.doc-pdf');
        const tifLink = tr.querySelector('a.doc-tif');
        const docHref = pdfLink ? pdfLink.href :
                        tifLink ? tifLink.href : null;
        return {
            date: date,
            code: code,
            description: description,
            doc_url: docHref,
            has_pdf: !!pdfLink,
        };
    }).filter((e) => dateRe.test(e.date));

    return {
        case_number: jsonMeta.casenumber || '',
        case_style: jsonMeta.style || '',
        cmid: jsonMeta.cmid || '',
        court: jsonMeta.court || '',
        judge: judge,
        filed: filed,
        closed: closed,
        docket_entries: entries,
    };
}
"""


def normalize_action(entry: dict) -> dict:
    """Map a parsed docket row into the SF-compatible action shape."""
    doc_url = entry.get("doc_url") or None
    doc_id = doc_id_from_url(doc_url) if doc_url else ""
    action_date = entry.get("date") or ""
    doc_filename = None
    if doc_url:
        doc_filename = f"{safe_filename(action_date)}_{doc_id or 'doc'}.pdf"
    description = entry.get("description") or ""
    return {
        "date": action_date,
        "code": entry.get("code") or "",
        "proceedings": description,
        "doc_url": doc_url,
        "doc_id": doc_id,
        "doc_filename": doc_filename,
    }


async def scrape_case_detail(context, page, case_data, hint_filing_iso: str = ""):
    """Open one case page, parse docket, write register_of_actions.json + PDFs.

    `hint_filing_iso` is used to bucket the case if the case page itself
    fails to surface a Filed: date. For search-by-day mode, the hint is
    the day we searched.
    """
    case_num = case_data["case_num"]
    print(f"  Scraping {case_num}...")
    await asyncio.sleep(random.uniform(2.0, 4.0))

    try:
        await page.goto(case_data["url"])
    except Exception as e:
        print(f"    Navigation failed: {e}")
        return None

    await wait_for_human_solve(page, target_text="Case Information")

    parsed = await page.evaluate(CASE_DETAIL_PARSE_JS)

    # Determine which filing-day folder this case belongs to.
    filing_iso = filed_to_iso(parsed.get("filed", "")) or hint_filing_iso
    if not filing_iso:
        # Last-ditch: try to pull a year from the case number, default day-1.
        m = re.search(r"\d{4}", case_num)
        if m:
            filing_iso = f"{m.group(0)}-01-01"
        else:
            filing_iso = "0000-00-00"  # quarantine bucket

    case_dir = case_dir_for(filing_iso, case_num)
    case_dir.mkdir(parents=True, exist_ok=True)

    # Build action records.
    actions = [normalize_action(e) for e in parsed.get("docket_entries", [])]

    # Filter + download high-value PDFs.
    downloaded = 0
    for action in actions:
        if not action["doc_url"]:
            continue
        if not is_high_value(action["proceedings"]):
            continue
        dest = case_dir / (action["doc_filename"] or f"doc_{action['doc_id'] or 'unknown'}.pdf")
        if dest.exists() and dest.stat().st_size > 0:
            downloaded += 1
            continue
        print(f"    Target: [{action['code']}] {action['proceedings'][:60]}...")
        ok = await download_pdf(context, page, action["doc_url"], dest)
        if ok:
            downloaded += 1

    record = {
        "metadata": {
            "case_number": parsed.get("case_number") or case_num,
            "case_type": case_num.split("-")[0].upper() if "-" in case_num else "",
            "county": case_data.get("county", ""),
            "filing_date": filing_iso,
            "case_style": parsed.get("case_style", ""),
            "judge": parsed.get("judge", ""),
            "filed": parsed.get("filed", ""),
            "closed": parsed.get("closed", ""),
            "cmid": parsed.get("cmid", ""),
            "court": parsed.get("court", ""),
            "scraped_at": utc_now_iso(),
            "source_url": case_data["url"],
            "storage": "local",
            "roa_source": "browser",
            "documents_downloaded": downloaded,
        },
        "actions": actions,
    }
    (case_dir / "register_of_actions.json").write_text(json.dumps(record, indent=2))
    return record

async def scrape_one_day(context, page, county: str, case_types: str,
                         filing_iso: str) -> None:
    """Search one filing day, scrape cases, write per-day state."""
    started_at = utc_now_iso()
    started_perf = time.perf_counter()
    oscn_date = iso_to_oscn(filing_iso)

    try:
        cases = await scrape_search_results(page, county, case_types, oscn_date, oscn_date)
    except Exception as e:
        print(f"  Search failed for {filing_iso}: {e}")
        update_day_summary(filing_iso, total_cases=0, scraped_cases=0,
                           run_metadata={"started_at": started_at,
                                         "finished_at": utc_now_iso(),
                                         "search_failed": True})
        return

    # Stamp county for downstream code, dedupe, and skip already-complete cases.
    for c in cases:
        c.setdefault("county", county)
    update_day_summary(filing_iso, total_cases=len(cases))

    pending = [c for c in cases if not case_is_complete(filing_iso, c["case_num"])]
    print(f"  {len(pending)} pending of {len(cases)} total")

    failures = []
    pbar = tqdm(total=len(pending), desc=filing_iso, unit="case")
    for case in pending:
        try:
            await scrape_case_detail(context, page, case, hint_filing_iso=filing_iso)
        except Exception as e:
            err_text = str(e)
            print(f"  {case['case_num']}: {err_text[:200]}")
            failures.append({**case, "error": err_text[:300]})
            if err_text == "IP_RESTRICTED":
                pbar.close()
                write_failed_cases(filing_iso, failures)
                raise
        finally:
            pbar.update(1)
    pbar.close()

    write_failed_cases(filing_iso, failures)

    # Some searches return cases that aren't actually filed on the searched
    # day (OSCN's filter has caveats). Count only cases whose Filed date
    # matches `filing_iso` against the day folder. Cases written to other
    # day folders are still on disk (under their actual Filed date) but
    # don't count toward this day's total.
    in_day_count = sum(
        1 for c in cases if case_is_complete(filing_iso, c["case_num"])
    )
    cross_day_count = sum(
        1 for c in cases
        if not case_is_complete(filing_iso, c["case_num"])
        and c["case_num"] not in {f["case_num"] for f in failures}
    )

    update_day_summary(
        filing_iso,
        total_cases=len(cases),
        scraped_cases=in_day_count,
        cross_day_cases=cross_day_count,
        failed_cases=len(failures),
        run_metadata={
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "elapsed_seconds": round(time.perf_counter() - started_perf, 2),
            "case_types": case_types,
            "county": county,
        },
    )
    if cross_day_count:
        print(
            f"  Note: {cross_day_count} cases from this search had Filed dates"
            f" outside {filing_iso}; written to their actual day folders."
        )


async def retry_failed_day(context, page, filing_iso: str) -> None:
    failed = load_failed_cases(filing_iso)
    if not failed:
        return
    print(f"\nRetrying {len(failed)} failed cases for {filing_iso}")
    new_failures = []
    pbar = tqdm(total=len(failed), desc=f"{filing_iso} retry", unit="case")
    for case in failed:
        try:
            await scrape_case_detail(context, page, case, hint_filing_iso=filing_iso)
        except Exception as e:
            err_text = str(e)
            new_failures.append({**case, "error": err_text[:300]})
            if err_text == "IP_RESTRICTED":
                pbar.close()
                write_failed_cases(filing_iso, new_failures)
                raise
        finally:
            pbar.update(1)
    pbar.close()
    write_failed_cases(filing_iso, new_failures)


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="OSCN docket scraper")
    parser.add_argument("--county", default="tulsa", choices=("tulsa", "oklahoma"))
    parser.add_argument("--type", default="CJ,CV",
                        help="Comma-separated case-type prefixes, e.g. CJ,CV")
    # Day-by-day search mode (preferred):
    parser.add_argument("--start-date", help="Filing-date start (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Filing-date end (YYYY-MM-DD)")
    # Sequential batching mode (when search is unreliable):
    parser.add_argument("--year", type=int,
                        help="Target year for sequential case-number batching (skips search)")
    parser.add_argument("--start-num", type=int, default=1)
    parser.add_argument("--count", type=int, default=10)
    # Modes:
    parser.add_argument("--failed-only", action="store_true",
                        help="Retry only cases listed in each day's failed_cases.json")
    args = parser.parse_args()

    launch_chrome()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        # --- Sequential batching mode ---
        if args.year:
            type_first = args.type.split(",")[0].strip().upper()
            print(f"Sequential batching: {type_first}-{args.year} starting at {args.start_num}")
            cases = []
            for i in range(args.start_num, args.start_num + args.count):
                case_id = f"{type_first}-{args.year}-{i}"
                cases.append({
                    "case_num": case_id,
                    "url": f"{CASE_URL}?db={args.county}&number={case_id}",
                    "county": args.county,
                })
            for case in tqdm(cases, desc=f"{type_first}-{args.year}", unit="case"):
                try:
                    await scrape_case_detail(context, page, case, hint_filing_iso="")
                except Exception as e:
                    print(f"  {case['case_num']}: {str(e)[:200]}")
                    if str(e) == "IP_RESTRICTED":
                        return
            return

        # --- Day-by-day search mode ---
        if not args.start_date or not args.end_date:
            raise SystemExit(
                "Provide --start-date and --end-date (YYYY-MM-DD) for search mode, "
                "or --year + --start-num + --count for sequential batching."
            )

        dates = weekday_dates(args.start_date, args.end_date)
        print(f"Filing days to process: {len(dates)} (weekdays only)")

        for day in dates:
            iso = day.isoformat()
            print(f"\nProcessing {iso} ({args.county}, types={args.type})")
            try:
                if args.failed_only:
                    await retry_failed_day(context, page, iso)
                else:
                    await scrape_one_day(context, page, args.county, args.type, iso)
            except Exception as e:
                if str(e) == "IP_RESTRICTED":
                    print("Stopping run; IP needs to clear before resuming.")
                    return
                print(f"Day {iso} aborted: {e}")


if __name__ == "__main__":
    asyncio.run(main())
