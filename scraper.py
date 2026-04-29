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
DATA_ROOT = Path("ok_scraper/data")

# --- Globals ---
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)

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

async def wait_for_human_solve(page, target_text="Case Search Results", auto_submit=True, return_on_submit=False):
    """Pause until the user solves the Cloudflare challenge, with auto-submit and retries."""
    print(f"Waiting for human solve (detecting: '{target_text}', auto_submit={auto_submit}, return_on_submit={return_on_submit})...")
    start_wait = time.monotonic()
    submitted_at = 0
    
    while True:
        try:
            title = await page.title()
            content = await page.content()
            url_lower = page.url.lower()
            
            # Detect IP Restricted
            if "restiction may be caused by the following" in content or "Temporary restiction expires" in content:
                print("\nFATAL ERROR: YOUR IP ADDRESS IS RESTRICTED BY OSCN.\n")
                raise Exception("IP_RESTRICTED")

            # Detect "UNABLE TO VERIFY"
            if "UNABLE TO VERIFY" in content:
                print("\n>>> VERIFICATION ERROR: Reloading page..."); await page.reload(); await asyncio.sleep(3)
                submitted_at = 0; continue

            # Identify challenge
            is_challenged = ("Turnstile" in title or "Just a moment" in title or "challenge-platform" in content)
            
            if is_challenged:
                is_solved = await page.evaluate("""() => {
                    const response = document.querySelector('[name="cf-turnstile-response"]');
                    return response && response.value && response.value.length > 10;
                }""")
                
                if is_solved and auto_submit:
                    # If it's been more than 15s since last click, try reload or click
                    if submitted_at > 0 and (time.monotonic() - submitted_at > 15):
                        print(">>> Navigation hang detected. Reloading page..."); await page.reload(); await asyncio.sleep(3)
                        submitted_at = time.monotonic(); continue
                    
                    if submitted_at == 0:
                        print("Turnstile solved! Finding submit button..."); await asyncio.sleep(2)
                        click_res = await page.evaluate("""() => {
                            const findBtn = (root) => {
                                const selectors = ['#btnSearch', '[name="btnSearch"]', '#btnContinue', '[name="btnContinue"]', 'input[type="submit"]', 'button[type="submit"]', 'a.btn-continue'];
                                for (const s of selectors) {
                                    const b = root.querySelector(s);
                                    if (b && b.offsetParent !== null) {
                                        b.click();
                                        b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                                        return s;
                                    }
                                }
                                return null;
                            };
                            let s = findBtn(document);
                            if (!s) { for (const f of Array.from(window.frames)) { try { s = findBtn(f.document); if(s) break; } catch(e) {} } }
                            if (!s) { const f = document.querySelector('form'); if(f) { f.submit(); s='form-submit'; } }
                            return s;
                        }""")
                        if click_res:
                            print(f">>> Submission triggered via {click_res}."); 
                            if return_on_submit:
                                print(">>> Proceeding immediately (return_on_submit=True)"); return True
                            submitted_at = time.monotonic()
                
                elapsed = int(time.monotonic() - start_wait)
                if elapsed > 0 and elapsed % 5 == 0:
                    status = "Solved, waiting nav" if is_solved else "Solve in Chrome"
                    print(f"  ... {elapsed}s, {status}, title: {title}")
                await asyncio.sleep(1); continue
            
            submitted_at = 0
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
            if str(e) == "IP_RESTRICTED": raise e
            await asyncio.sleep(1); continue
        await asyncio.sleep(1)

async def download_pdf(context, url, dest_path):
    """Download a PDF using a real page tab to handle challenges."""
    async with DOWNLOAD_SEMAPHORE:
        page = await context.new_page()
        try:
            await asyncio.sleep(random.uniform(2.0, 4.0))
            await page.bring_to_front()
            try:
                async with page.expect_download(timeout=10000) as download_info:
                    await page.goto(url, wait_until="commit")
                download = await download_info.value
                await download.save_as(dest_path); return True
            except Exception as e:
                if "Download is starting" in str(e): return True
                if page.url == "about:blank":
                    await page.goto(url, wait_until="commit")

            # Use return_on_submit=True to avoid the Turnstile reload loop for downloads
            await wait_for_human_solve(page, target_text="PDF", auto_submit=True, return_on_submit=True)
            
            print("      Capturing download...")
            async with page.expect_download(timeout=60000) as download_info:
                # Re-navigating or just waiting for the event after submit
                await asyncio.sleep(2)
                if page.url == "about:blank" or "oscn.net" in page.url:
                    await page.goto(url)
            
            download = await download_info.value
            await download.save_as(dest_path); return True
        except Exception as e:
            if dest_path.exists() and dest_path.stat().st_size > 0: return True
            print(f"      Download failed: {e}"); return False
        finally: await page.close()

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
    for action in data['actions']:
        doc_filename = None
        if action['doc_url'] and is_high_value(action['proceedings']):
            doc_id = parse_qs(urlparse(action['doc_url']).query).get('bc', ['doc'])[0]
            doc_filename = f"{action['date']}_{doc_id}.pdf"
            dest = case_dir / doc_filename
            if not dest.exists():
                print(f"    Target found: {action['proceedings'][:60]}...")
                if await download_pdf(context, action['doc_url'], dest): downloaded += 1
                else: doc_filename = None
            else: downloaded += 1
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
    parser.add_argument("--camoufox", action="store_true", help="Use Camoufox hardened browser (autonomous mode)")
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

    if args.camoufox:
        if not CAMOUFOX_AVAILABLE:
            print("Error: Camoufox not installed. Run 'pip install camoufox'")
            return
        print("Launching Camoufox hardened browser (autonomous mode)...")
        # Use AsyncCamoufox with hardened spoofing
        async with AsyncCamoufox(
            headless=False,
            os="mac",
            humanize=True # Adds natural delays and mouse movements
        ) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            await run_scraper_loop(args, context, page, start_num)
    else:
        # Standard Chrome / CDP logic
        try:
            pids = subprocess.check_output(f"lsof -i :{DEBUG_PORT} -t", shell=True).decode().split()
            for pid in pids: os.kill(int(pid), 15); time.sleep(2)
        except: pass
        launch_chrome()
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            context = browser.contexts[0]; page = context.pages[0] if context.pages else await context.new_page()
            await run_scraper_loop(args, context, page, start_num)
            await browser.close()

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
