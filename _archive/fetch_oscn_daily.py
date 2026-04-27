import asyncio
from playwright.async_api import async_playwright

async def fetch_oscn_daily():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Try Results search
        url = "https://www.oscn.net/dockets/Results.aspx?db=tulsa&type=CJ&filedstart=04/01/2026&filedend=04/20/2026"
        
        print(f"Navigating to {url}...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            title = await page.title()
            print(f"Page title: {title}")
            
            content = await page.content()
            print(f"Content length: {len(content)}")
            
            if "Turnstile" in title or "Why am I seeing this?" in content:
                print("BLOCKED on Results too.")
            else:
                print("SUCCESSfully reached Results.")
                # Save for inspection
                with open("ok_scraper/results_sample.html", "w") as f:
                    f.write(content)
        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(fetch_oscn_daily())
