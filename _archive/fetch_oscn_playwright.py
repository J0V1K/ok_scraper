import asyncio
from playwright.async_api import async_playwright
import sys

async def fetch_oscn_case():
    async with async_playwright() as p:
        # We need a browser. Using chromium since it's already installed.
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        county = 'tulsa'
        year = '2024'
        number = '1'
        case_type = 'CJ'
        
        url = f"https://www.oscn.net/dockets/GetCaseInformation.aspx?db={county}&number={case_type}-{year}-{number}"
        
        print(f"Navigating to {url}...")
        try:
            # Wait for a bit to avoid immediate detection
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for a specific element that should be on the case page
            try:
                await page.wait_for_selector(".caseStyle", timeout=10000)
            except:
                print("Warning: .caseStyle not found. Might be challenged.")
            
            # Check if we are challenged
            title = await page.title()
            print(f"Page title: {title}")
            
            content = await page.content()
            print(f"Content length: {len(content)}")
            
            if "Why am I seeing this?" in content:
                print("STILL BLOCKED by human verification challenge.")
                await page.screenshot(path="ok_scraper/blocked.png")
            else:
                print("SUCCESSfully reached the case page (presumably).")
                await page.screenshot(path="ok_scraper/success.png")
                # print(f"First 500 chars: {content[:500]}")
                
                # Save for inspection
                with open("ok_scraper/case_sample.html", "w") as f:
                    f.write(content)
                    
        except Exception as e:
            print(f"Error: {e}")
            await page.screenshot(path="ok_scraper/error.png")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(fetch_oscn_case())
