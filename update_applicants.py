#!/usr/bin/env python3
"""
Update applicant counts for all jobs by scraping LinkedIn's public job pages.

No authentication required — uses the public HTML page which includes applicant counts.
Updates dashboard_state.json directly so counts show on next dashboard refresh.

Usage:
    python update_applicants.py

Requires the dashboard server files to be in place (reads all_jobs_metadata.json,
writes to dashboard/dashboard_state.json).
"""

import json
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
METADATA_FILE = os.path.join(BASE_DIR, "resumes", "all_jobs_metadata.json")
STATE_FILE = os.path.join(BASE_DIR, "dashboard", "dashboard_state.json")

BATCH_SIZE = 5
DELAY_BETWEEN_BATCHES = 1.0  # seconds


def extract_linkedin_job_id(url):
    m = re.search(r'/view/(\d+)', url)
    return m.group(1) if m else None


def fetch_applicants(job_id):
    """Fetch applicant count from public LinkedIn job page via curl."""
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15
        )
        html = result.stdout
        # Look for patterns like "162 applicants", "Over 200 applicants", "1,234 applicants"
        m = re.search(r'((?:Over\s+)?[\d,]+\s+applicants?)', html, re.IGNORECASE)
        if m:
            return m.group(1)
        # Also check for "Be among the first X applicants"
        m = re.search(r'Be among the first\s+(\d+)\s+applicants?', html, re.IGNORECASE)
        if m:
            return m.group(0)
        return None
    except Exception:
        return None


def main():
    # Load jobs
    with open(METADATA_FILE) as f:
        jobs = json.load(f)

    # Load existing state
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

    # Filter to jobs with LinkedIn links
    job_pairs = []
    for j in jobs:
        link = j.get('job_link', '')
        lid = extract_linkedin_job_id(link)
        if lid and j.get('id'):
            job_pairs.append((j['id'], lid))

    total = len(job_pairs)
    print(f"Updating applicant counts for {total} jobs...\n")

    updated = 0
    failed = 0
    skipped = 0

    for i in range(0, total, BATCH_SIZE):
        batch = job_pairs[i:i + BATCH_SIZE]

        for job_id, linkedin_id in batch:
            applicants = fetch_applicants(linkedin_id)
            if applicants:
                if job_id not in state:
                    state[job_id] = {}
                state[job_id]["applicants"] = applicants
                updated += 1
            else:
                failed += 1

        processed = min(i + BATCH_SIZE, total)
        print(f"Progress: {processed}/{total} ({updated} updated, {failed} failed)")

        # Save intermediate results every 50 jobs
        if updated > 0 and processed % 50 == 0:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
            print(f"  Saved checkpoint.")

        # Delay between batches to avoid rate limiting
        if i + BATCH_SIZE < total:
            time.sleep(DELAY_BETWEEN_BATCHES)

    # Final save
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

    print(f"\nDone! {updated} updated, {failed} failed out of {total} total.")
    print("Refresh your dashboard to see the updated counts.")


if __name__ == "__main__":
    main()
