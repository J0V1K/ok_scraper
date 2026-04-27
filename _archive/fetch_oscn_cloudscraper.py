import cloudscraper
import os

def fetch_with_cloudscraper():
    print("Starting cloudscraper pilot...")
    
    # Create a scraper instance
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'darwin',
            'desktop': True
        }
    )
    
    county = 'tulsa'
    year = '2024'
    number = '1'
    case_type = 'CJ'
    url = f"https://www.oscn.net/dockets/GetCaseInformation.aspx?db={county}&number={case_type}-{year}-{number}"
    
    print(f"Navigating to {url}...")
    try:
        response = scraper.get(url)
        print(f"Status code: {response.status_code}")
        
        content = response.text
        print(f"Content length: {len(content)}")
        
        if "Why am I seeing this?" in content or "Turnstile" in content:
            print("STILL BLOCKED by Cloudflare (Turnstile detected).")
        else:
            print("SUCCESSfully reached the case page!")
            with open("ok_scraper/cloudscraper_sample.html", "w") as f:
                f.write(content)
            print(f"First 200 chars: {content[:200]}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    fetch_with_cloudscraper()
