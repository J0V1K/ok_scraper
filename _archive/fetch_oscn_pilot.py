import oscn
import json
import os

def pilot_oscn():
    print("Starting OSCN Pilot...")
    
    county = 'tulsa'
    year = '2024'
    number = 1
    
    print(f"Requesting case CJ-{year}-{number} in {county}...")
    
    try:
        case = oscn.request.Case(county=county, year=year, number=number, type='CJ')
        
        print(f"Valid: {case.valid}")
        print(f"Style: {case.style}")
        print(f"Body length: {len(case.body)}")
        print(f"Body content: {case.body}")
        if not case.valid:
            return

        print(f"Processing case: {case.oscn_number}")
        print(f"Judge: {case.judge}")
        print(f"Filed: {case.filed}")
        
        # Look for PDF links in the docket
        pdf_links = []
        for entry in case.docket:
            links = entry.get('links', [])
            if links:
                pdf_links.extend(links)
        
        print(f"Found {len(pdf_links)} potential PDFs.")
        for link in pdf_links:
            print(f"  - {link}")

    except Exception as e:
        print(f"Error requesting case: {e}")

if __name__ == "__main__":
    pilot_oscn()
