import undetected_chromedriver as uc
import time
import os

def fetch_with_uc():
    print("Starting undetected-chromedriver pilot...")
    
    options = uc.ChromeOptions()
    # options.add_argument('--headless') # UC works better in headful, but let's try headless first
    
    # Use a specific user data dir to maintain some persistence if needed
    # options.add_argument('--user-data-dir=ok_scraper/uc_profile')

    try:
        driver = uc.Chrome(options=options, version_main=123) # Match playwright's version or let it auto-detect
        
        county = 'tulsa'
        year = '2024'
        number = '1'
        case_type = 'CJ'
        url = f"https://www.oscn.net/dockets/GetCaseInformation.aspx?db={county}&number={case_type}-{year}-{number}"
        
        print(f"Navigating to {url}...")
        driver.get(url)
        
        # Wait for the challenge to potentially resolve
        print("Waiting 15 seconds for potential Cloudflare challenge...")
        time.sleep(15)
        
        print(f"Page title: {driver.title}")
        content = driver.page_source
        print(f"Content length: {len(content)}")
        
        if "Why am I seeing this?" in content or "Turnstile" in driver.title:
            print("STILL BLOCKED by Cloudflare.")
        else:
            print("SUCCESSfully reached the case page!")
            with open("ok_scraper/uc_sample.html", "w") as f:
                f.write(content)
            driver.save_screenshot("ok_scraper/uc_success.png")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if 'driver' in locals():
            driver.quit()

if __name__ == "__main__":
    fetch_with_uc()
