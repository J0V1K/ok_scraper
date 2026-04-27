import asyncio
from playwright.async_api import async_playwright
import subprocess
import time
import os
from pathlib import Path

# Use a specific port for Oklahoma to avoid conflict with SF scraper
DEBUG_PORT = 9223 
CHROME_PROFILE = Path.home() / ".ok_manual_profile"

def launch_chrome():
    CHROME_PROFILE.mkdir(exist_ok=True)
    
    # Check if already running
    try:
        subprocess.check_output(f"lsof -i :{DEBUG_PORT}", shell=True)
        print(f"Chrome already running on port {DEBUG_PORT}")
        return
    except:
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

async def pilot_hitl():
    launch_chrome()
    
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = await context.new_page()
            
            url = "https://www.oscn.net/dockets/GetCaseInformation.aspx?db=tulsa&number=CJ-2024-1"
            print(f"Navigating to {url}...")
            await page.goto(url)
            
            print("\n" + "="*60)
            print("MANUAL STEP REQUIRED:")
            print("Please switch to the Google Chrome window and solve the Turnstile/Cloudflare challenge.")
            print("The script will wait until it detects the case style on the page.")
            print("="*60 + "\n")
            
            # Poll for success
            max_wait = 120
            start_time = time.time()
            success = False
            
            while time.time() - start_time < max_wait:
                # Check for an element that exists on a successful case page
                # .caseStyle is used in SF, let's see what OSCN uses. 
                # Based on previous research, let's look for "Case Information" or similar
                content = await page.content()
                if "Case Information" in content and "CJ-2024-1" in content and "Turnstile" not in content:
                    print("SUCCESS: Challenge solved and case data detected!")
                    success = True
                    break
                
                if "Why am I seeing this?" in content:
                    # Still challenged
                    pass
                
                await asyncio.sleep(2)
            
            if success:
                # Extract some data to prove it works
                title = await page.title()
                print(f"Final Page Title: {title}")
                # Save for verification
                with open("ok_scraper/hitl_sample.html", "w") as f:
                    f.write(await page.content())
                print("Saved sample to ok_scraper/hitl_sample.html")
            else:
                print("Timed out waiting for manual solve.")
                
        except Exception as e:
            print(f"Error: {e}")
        finally:
            # We don't necessarily want to close the browser if we want to reuse the session
            pass

if __name__ == "__main__":
    asyncio.run(pilot_hitl())
