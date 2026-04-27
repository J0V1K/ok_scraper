import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from playwright.async_api import async_playwright
from tqdm import tqdm

# --- Configuration ---
DEBUG_PORT = 9223
CHROME_PROFILE = Path.home() / ".ok_manual_profile"
BASE_URL = "https://www.oscn.net/dockets"
SEARCH_URL = f"{BASE_URL}/Results.aspx"
CASE_URL = f"{BASE_URL}/GetCaseInformation.aspx"
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
    ]
    subprocess.Popen(cmd)
    time.sleep(3)

async def wait_for_human_solve(page, target_text="Case Search Results"):
    """Pause until the user solves the Cloudflare challenge, then auto-submit if possible."""
    print(f"Waiting for human solve (detecting: '{target_text}')...")
    start_wait = time.time()
    while True:
        try:
            title = await page.title()
            content = await page.content()
            
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
                
                if is_solved:
                    print("Turnstile solved! Attempting automatic submission...")
                    await page.evaluate("""() => {
                        const btn = document.querySelector('input[type="submit"], button[type="submit"]');
                        if (btn) btn.click();
                        else {
                            const form = document.querySelector('form');
                            if (form) form.submit();
                        }
                    }""")
                    await asyncio.sleep(2) # Wait for navigation to start
                    continue

                if int(time.time() - start_wait) % 15 == 0:
                    print(f"\\n>>> CHALLENGE DETECTED: Please solve Turnstile in Chrome. (Title: {title})")
                await asyncio.sleep(2)
                continue
            
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
                
        except Exception:
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
    
    # Extract case links from the page
    cases = await page.evaluate("""(allowedTypes) => {
        // Find the main results table if possible, otherwise look at all links
        const table = document.querySelector('table.docketlist') || document;
        const links = Array.from(table.querySelectorAll('a[href*="GetCaseInformation.aspx"]'));
        
        return links.map(link => {
            const row = link.closest('tr');
            const caseNum = link.innerText.trim();
            const prefix = caseNum.split('-')[0].toUpperCase();
            
            if (!allowedTypes.includes(prefix)) return null;
            
            return {
                case_num: caseNum,
                url: link.href,
                style: row ? row.innerText.replace(/\\s+/g, ' ').trim() : ''
            };
        }).filter(c => c !== null);
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

async def download_pdf(context, url, dest_path):
    """Download a PDF using a separate page to avoid interrupting the main flow."""
    page = await context.new_page()
    try:
        async with page.expect_download() as download_info:
            await page.goto(url)
        download = await download_info.value
        await download.save_as(dest_path)
        return True
    except Exception as e:
        print(f"      Download failed: {e}")
        return False
    finally:
        await page.close()

async def scrape_case_detail(context, page, case_data):
    """Scrape case and download high-value PDFs."""
    print(f"  Scraping {case_data['case_num']}...")
    await page.goto(case_data['url'])
    await wait_for_human_solve(page, target_text="Case Information")
    
    # Identify links and their descriptions from the docket table
    documents = await page.evaluate("""() => {
        const docLinks = [];
        const rows = Array.from(document.querySelectorAll('table.dockettext tr'));
        rows.forEach(row => {
            // OSCN has explicit classes for PDF links
            const link = row.querySelector('a.doc-pdf');
            if (link) {
                // The description is usually in the first <p> of the .description-wrapper or the cell text itself
                const wrapper = row.querySelector('.description-wrapper');
                let description = '';
                if (wrapper) {
                    description = wrapper.innerText.split('Document Available')[0].trim();
                } else {
                    description = row.innerText.split('Document Available')[0].trim();
                }
                
                docLinks.push({
                    description: description.replace(/\\n/g, ' '),
                    url: link.href
                });
            }
        });
        return docLinks;
    }""")

    # Extract all docket text entries for debugging/analysis
    docket_entries = await page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('table.dockettext tr'));
        return rows.map(row => row.innerText.trim()).filter(t => t.length > 0);
    }""")
    
    case_dir = DATA_ROOT / case_data['case_num'].replace('-', '_')
    case_dir.mkdir(parents=True, exist_ok=True)
    
    high_value_docs = []
    for doc in documents:
        if is_high_value(doc['description']):
            # Create a safe filename
            safe_desc = re.sub(r'[^a-zA-Z0-9 ]', '', doc['description'])[:50].strip().replace(' ', '_')
            dest = case_dir / f"{safe_desc}.pdf"
            
            print(f"    Target found: {doc['description'][:60]}...")
            success = await download_pdf(context, doc['url'], dest)
            if success:
                high_value_docs.append({"description": doc['description'], "local_path": str(dest)})

    return {
        "metadata": case_data,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "docket_entries": docket_entries,
        "high_value_documents": high_value_docs,
        "all_documents_found": documents
    }

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--county", default="tulsa")
    parser.add_argument("--type", default="CJ", help="Case type prefix (e.g. CJ, CV)")
    parser.add_argument("--year", type=int, help="Target year for sequential batching (skips search)")
    parser.add_argument("--start-num", type=int, default=1, help="Start number for sequential batching")
    parser.add_argument("--count", type=int, default=10, help="Number of cases to scrape in batching mode")
    parser.add_argument("--start", default="01/01/2024", help="Search start date (if not using --year)")
    parser.add_argument("--end", default="01/05/2024", help="Search end date (if not using --year)")
    args = parser.parse_args()

    launch_chrome()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0]
        page = await context.new_page()

        if args.year:
            # Sequential Batching Mode
            print(f"Sequential Batching Mode: {args.type}-{args.year} starting at {args.start_num}")
            cases = []
            for i in range(args.start_num, args.start_num + args.count):
                case_id = f"{args.type}-{args.year}-{i}"
                cases.append({
                    "case_num": case_id,
                    "url": f"{CASE_URL}?db={args.county}&number={case_id}"
                })
        else:
            # Search Results Mode
            cases = await scrape_search_results(page, args.county, args.type, args.start, args.end)
        
        for case in tqdm(cases, desc="Processing Cases"):
            try:
                result = await scrape_case_detail(context, page, case)
                with open(DATA_ROOT / f"{case['case_num'].replace('-', '_')}_meta.json", "w") as f:
                    json.dump(result, f, indent=2)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"Error on {case['case_num']}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
