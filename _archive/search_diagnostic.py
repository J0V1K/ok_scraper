import asyncio
from playwright.async_api import async_playwright
import subprocess
import time
from pathlib import Path

DEBUG_PORT = 9223

async def diagnostic():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = await context.new_page()
            
            # Target the known active period
            url = "https://www.oscn.net/dockets/Results.aspx?db=tulsa&type=CJ&filedstart=01/02/2024&filedend=01/03/2024"
            print(f"Navigating to {url}...")
            await page.goto(url, wait_until="commit")
            
            print("Please solve Turnstile if it appears...")
            # Simple wait for the table or challenge
            for _ in range(30):
                content = await page.content()
                if "CJ-2024" in content:
                    print("Found CJ-2024 in content!")
                    break
                await asyncio.sleep(2)
            
            content = await page.content()
            with open("ok_scraper/search_diagnostic.html", "w") as f:
                f.write(content)
            print("Saved diagnostic HTML to ok_scraper/search_diagnostic.html")
            
            # Check the table structure
            rows_found = await page.evaluate("""() => {
                const rows = document.querySelectorAll('tr');
                return Array.from(rows).map(r => r.innerText.substring(0, 50));
            }""")
            print(f"Total table rows found: {len(rows_found)}")
            print("First few rows text:")
            for r in rows_found[:5]:
                print(f"  - {r}")

        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(diagnostic())
