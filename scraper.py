import asyncio
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

# --- Configuration ---
DEBUG_PORT = 9223
CHROME_PROFILE = Path.home() / ".ok_manual_profile"
BASE_URL = "https://www.oscn.net/dockets"
CASE_URL = f"{BASE_URL}/GetCaseInformation.aspx"
DOC_URL = f"{BASE_URL}/GetDocument.aspx"
DATA_ROOT = Path(__file__).resolve().parent / "data"

# --- Globals ---
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)

# Guardrails so a single big-litigation case can't burn through CF's
# "failed-verification" budget. Each click on an a.doc-pdf is a request
# OSCN sees; cap both attempts per case and consecutive gate-fails so
# that one outlier case caps damage to the IP's reputation.
PER_CASE_PDF_CAP = 5
MAX_CONSECUTIVE_GATES = 2

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

    # Prefer clicking inside the Turnstile iframe via a real Playwright click.
    iframe_hit = await _click_visible(
        page,
        [
            "iframe[src*='turnstile']",
            "iframe[title*='Widget']",
            "iframe[title*='challenge']",
        ],
        force=True,
    )
    if iframe_hit:
        try:
            iframe = page.locator(iframe_hit).first
            box = await iframe.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return f"iframe-center:{iframe_hit}"
        except Exception:
            pass

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
    max_wait_s=None,
    max_submit_reloads=3,
):
    """Pause until the user solves the Cloudflare challenge, with auto-submit and retries."""
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
                        print("Turnstile solved! Finding submit button..."); await asyncio.sleep(2)
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
                await asyncio.sleep(1); continue
            
            submitted_at = 0
            submit_reload_count = 0
            success_indicators = [
                target_text in content, 
                "docketlist" in content.lower(), 
                "Case Information" in content, 
                "%PDF-" in content[:100], 
                "getdocument.aspx" in url_lower and "turnstile" not in content.lower()
            ]
            
            if any(success_indicators):
                print(f"Challenge cleared! Detected: {title}")
                return True
                
        except Exception as e:
            if str(e) == "PAGE_CLOSED" and closed_ok:
                print("Challenge page closed; treating as success")
                return True
            if str(e) == "IP_RESTRICTED": raise e
            await asyncio.sleep(1); continue
        await asyncio.sleep(1)

async def download_pdf(page, action, dest_path):
    """Download a PDF by clicking the a.doc-pdf link from the case page.

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
    async with DOWNLOAD_SEMAPHORE:
        await asyncio.sleep(random.uniform(5.0, 12.0))

        doc_url = action.get("doc_url") or ""
        doc_id = parse_qs(urlparse(doc_url).query).get("bc", [""])[0]
        if not doc_id:
            print(f"      {dest_path.name}: no bc= in doc_url; skipping")
            return False

        # Prefer the explicit PDF anchor; fall back to any doc anchor.
        matches = page.locator(f'a.doc-pdf[href*="bc={doc_id}"]')
        if await matches.count() == 0:
            matches = page.locator(f'a[href*="bc={doc_id}"]')
        if await matches.count() == 0:
            print(f"      {dest_path.name}: link not found on page; skipping")
            return False
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
                    max_wait_s=35,
                    max_submit_reloads=1,
                )
            except Exception as popup_error:
                print(f"      {dest_path.name}: popup solve failed: {popup_error}")

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
            return True

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
                    return False
            else:
                print(f"      {dest_path.name}: download failed: {e}")
                return False

        if mode == "session":
            return True

        try:
            await download.save_as(dest_path)
            return True
        except Exception as e:
            print(f"      {dest_path.name}: save failed: {e}")
            return False

async def scrape_case_detail(context, page, case_data):
    """Scrape case and output SF-compatible register_of_actions.json."""
    case_num = case_data["case_num"]
    print(f"  Scraping {case_num}...")
    await asyncio.sleep(random.uniform(2.0, 4.0))
    try: await page.goto(case_data['url'], wait_until="commit", timeout=60000)
    except: pass
    
    await wait_for_human_solve(page, target_text="Case Information")
    
    data = await page.evaluate("""() => {
        const table = document.querySelector('table.docketlist');
        if (!table) return { actions: [], judge: '', style: '' };
        const rows = Array.from(table.querySelectorAll('tr.docketRow, tr'));
        const actions = [];
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
        const styleTbl = document.querySelector('table.caseStyle');
        let judge = '', style = '';
        if (styleTbl) {
            const tds = styleTbl.querySelectorAll('td');
            if (tds.length >= 2) {
                const m = tds[1].innerText.match(/Judge:\\s*([^\\n]+)/i);
                judge = m ? m[1].trim() : '';
            }
            style = styleTbl.innerText.split('\\n')[0].trim();
        }
        return { actions, judge, style };
    }""")

    case_dir = DATA_ROOT / case_num.replace('-', '_')
    case_dir.mkdir(parents=True, exist_ok=True)
    
    final_actions = []
    downloaded = 0
    attempts = 0
    consecutive_gates = 0
    capped = False
    for action in data['actions']:
        doc_filename = None
        if action['doc_url'] and is_high_value(action['proceedings']) and not capped:
            doc_id = parse_qs(urlparse(action['doc_url']).query).get('bc', ['doc'])[0]
            doc_filename = f"{action['date']}_{doc_id}.pdf"
            dest = case_dir / doc_filename
            if dest.exists():
                downloaded += 1
            elif attempts >= PER_CASE_PDF_CAP:
                # Per-case cap reached. Mark remaining high-value docs as
                # not downloaded so a --failed-only pass can pick them up
                # later under different (cooler) session conditions.
                doc_filename = None
                capped = True
                print(f"    Cap hit ({PER_CASE_PDF_CAP} PDFs); deferring remaining to retry pass")
            else:
                attempts += 1
                print(f"    Target found ({attempts}/{PER_CASE_PDF_CAP}): {action['proceedings'][:60]}...")
                ok = await download_pdf(page, action, dest)
                if ok:
                    downloaded += 1
                    consecutive_gates = 0
                else:
                    doc_filename = None
                    consecutive_gates += 1
                    if consecutive_gates >= MAX_CONSECUTIVE_GATES:
                        capped = True
                        print(f"    Circuit breaker: {consecutive_gates} consecutive failures; "
                              f"deferring remaining to retry pass")
        final_actions.append({ "date": action['date'], "proceedings": action['proceedings'], "fee": "", "doc_url": action['doc_url'], "doc_filename": doc_filename })

    result = {
        "metadata": { "case_number": case_num, "case_title": data['style'], "filing_date": filed_to_iso(data['actions'][0]['date']) if data['actions'] else "", "timing": { "scraped_at": utc_now_iso(), "downloaded_docs": downloaded } },
        "actions": final_actions
    }
    with open(case_dir / "register_of_actions.json", "w") as f: json.dump(result, f, indent=2)
    return result

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--type", default="CJ")
    parser.add_argument("--start", type=int, help="Sequence number to start at (defaults to auto-resume)")
    parser.add_argument("--chrome", action="store_true",
                        help="Fall back to attaching to system Chrome via CDP (default is Camoufox)")
    args = parser.parse_args()

    # --- Auto-Resume Logic ---
    start_num = args.start
    if start_num is None:
        print(f"Checking existing data in {DATA_ROOT} for auto-resume...")
        existing_nums = []
        prefix = f"{args.type}_{args.year}_"
        if DATA_ROOT.exists():
            for item in DATA_ROOT.iterdir():
                if item.is_dir() and item.name.startswith(prefix):
                    try:
                        num = int(item.name.replace(prefix, ""))
                        existing_nums.append(num)
                    except ValueError: continue
        if existing_nums:
            start_num = max(existing_nums) + 1
            print(f"Auto-resume: Found {len(existing_nums)} cases. Starting at #{start_num}")
        else:
            start_num = 1
            print(f"No existing data found for {args.type}-{args.year}. Starting at #1")

    if args.chrome:
        # CDP-attached system Chrome (debugging fallback). Cloudflare can
        # fingerprint the resulting browser more easily; expect more gates.
        try:
            pids = subprocess.check_output(f"lsof -i :{DEBUG_PORT} -t", shell=True).decode().split()
            for pid in pids: os.kill(int(pid), 15); time.sleep(2)
        except: pass
        launch_chrome()
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            await run_scraper_loop(args, context, page, start_num)
            await browser.close()
        return

    # Default: Camoufox (Playwright Firefox build with anti-fingerprint
    # hardening). Required for click-driven downloads to clear CF gates.
    if not CAMOUFOX_AVAILABLE:
        print("Error: Camoufox not installed in this venv.")
        print("Install with: pip install 'camoufox[geoip]'")
        print("Or use --chrome to fall back to system Chrome via CDP (degraded gate-clearance).")
        return
    print("Launching Camoufox hardened browser...")
    async with AsyncCamoufox(
        headless=False,
        os="macos",
        humanize=True,  # natural delays + mouse movements
    ) as browser:
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        await run_scraper_loop(args, context, page, start_num)

async def run_scraper_loop(args, context, page, start_num):
    for i in range(start_num, start_num + args.count):
        case_id = f"CJ-{args.year}-{i}"
        try: 
            await scrape_case_detail(context, page, {"case_num": case_id, "url": f"{CASE_URL}?db=tulsa&number={case_id}"})
        except Exception as e:
            if str(e) == "IP_RESTRICTED": break
            print(f"Error on {case_id}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
