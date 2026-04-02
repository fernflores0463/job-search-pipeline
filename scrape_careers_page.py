#!/usr/bin/env python3
"""
scrape_careers_page.py — Scrape jobs from a company careers page

Fetches a company careers page URL, extracts job listings, and feeds them
into the same scoring/tiering pipeline used for LinkedIn imports.

Usage:
    python scrape_careers_page.py <careers_page_url> [--company "Company Name"]

Examples:
    python scrape_careers_page.py "https://careers.example.com/jobs" --company "Example Corp"
    python scrape_careers_page.py "https://boards.greenhouse.io/company"

The script will:
  1. Fetch and parse the careers page
  2. Extract job listings (title, link, location, description)
  3. Follow each job link to grab the full description
  4. Score/tier each job using your existing tech-match criteria
  5. Generate resume packages and rebuild the dashboard

Supports common patterns: Lever, Greenhouse, Workday, Ashby, custom HTML pages.
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Import shared pipeline functions from process_new_postings.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from process_new_postings import (  # noqa: E402
    is_excluded_company, is_excluded_title, is_excluded_role, is_swe_role,
    calc_tech_score, calc_level_bonus, calc_company_bonus, assign_tier,
    make_job_id, generate_resume_files, rebuild_dashboard,
    METADATA_FILE,
)
from datetime import date  # noqa: E402

RESUMES_DIR = os.path.join(BASE_DIR, "resumes")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


# ─────────────────────────────────────────────────────────
# FETCHING
# ─────────────────────────────────────────────────────────

def fetch_page(url, timeout=15):
    """Fetch a page and return BeautifulSoup object."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, 'html.parser'), resp.url


def fetch_description(url, timeout=15):
    """Fetch a job detail page and extract the description text."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Try common description containers
        desc_el = None
        for selector in [
            # Greenhouse
            '#content', '.content-wrapper',
            # Lever
            '.posting-page', '.content',
            # Ashby
            '[data-testid="job-description"]', '.ashby-job-posting-description',
            # Workday
            '[data-automation-id="jobPostingDescription"]',
            # Generic
            '.job-description', '.description', '#job-description',
            '[class*="description"]', '[class*="job-detail"]',
            'article', '.posting-requirements',
            'main',
        ]:
            desc_el = soup.select_one(selector)
            if desc_el and len(desc_el.get_text(strip=True)) > 100:
                break

        if desc_el:
            return desc_el.get_text(separator=' ', strip=True)

        # Fallback: grab body text
        body = soup.find('body')
        if body:
            text = body.get_text(separator=' ', strip=True)
            # Return up to 5000 chars
            return text[:5000] if len(text) > 5000 else text
        return ''
    except Exception as e:
        print(f"  Warning: Could not fetch description from {url}: {e}")
        return ''


# ─────────────────────────────────────────────────────────
# ATS-SPECIFIC EXTRACTORS
# ─────────────────────────────────────────────────────────

def detect_platform(url, soup):
    """Detect which ATS platform the page is on."""
    domain = urlparse(url).hostname or ''
    page_text = str(soup)

    if 'greenhouse.io' in domain or 'boards.greenhouse' in domain:
        return 'greenhouse'
    if 'lever.co' in domain or 'jobs.lever' in domain:
        return 'lever'
    if 'myworkday' in domain or 'workday' in domain:
        return 'workday'
    if 'ashbyhq.com' in domain:
        return 'ashby'
    if 'smartrecruiters' in domain:
        return 'smartrecruiters'
    if 'jobvite' in domain:
        return 'jobvite'
    if 'icims' in domain:
        return 'icims'

    # Heuristic checks on page content
    if 'greenhouse' in page_text.lower()[:2000]:
        return 'greenhouse'
    if 'lever' in page_text.lower()[:2000]:
        return 'lever'

    return 'generic'


def extract_greenhouse(soup, base_url):
    """Extract jobs from a Greenhouse board page."""
    jobs = []
    # Greenhouse boards: sections with department names, each containing job links
    for link in soup.select('a[href*="/jobs/"]'):
        title = link.get_text(strip=True)
        href = urljoin(base_url, link.get('href', ''))
        if title and '/jobs/' in href and len(title) > 3:
            location = ''
            # Location is often a sibling or parent's sibling
            parent = link.parent
            loc_el = parent.find('span', class_=lambda c: c and 'location' in c.lower()) if parent else None
            if not loc_el and parent:
                loc_el = parent.find_next_sibling()
            if loc_el:
                location = loc_el.get_text(strip=True)
            jobs.append({'title': title, 'url': href, 'location': location})
    return jobs


def extract_lever(soup, base_url):
    """Extract jobs from a Lever postings page."""
    jobs = []
    for posting in soup.select('.posting'):
        title_el = posting.select_one('.posting-title h5, a.posting-title')
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link_el = posting.select_one('a.posting-title, a[href*="/jobs/"]') or posting.find('a')
        href = urljoin(base_url, link_el.get('href', '')) if link_el else ''

        location = ''
        loc_el = posting.select_one('.posting-categories .location, .workplaceTypes')
        if loc_el:
            location = loc_el.get_text(strip=True)

        if title and href:
            jobs.append({'title': title, 'url': href, 'location': location})
    return jobs


def extract_ashby(soup, base_url):
    """Extract jobs from an Ashby job board."""
    jobs = []
    for link in soup.select('a[href*="/jobs/"], a[href*="/posting/"]'):
        title = link.get_text(strip=True)
        href = urljoin(base_url, link.get('href', ''))
        if title and len(title) > 3:
            location = ''
            parent = link.parent
            if parent:
                loc_el = parent.find(string=re.compile(r'remote|hybrid|onsite|office', re.I))
                if loc_el:
                    location = loc_el.strip()
            jobs.append({'title': title, 'url': href, 'location': location})
    return jobs


def extract_generic(soup, base_url):
    """Generic extraction: find all links that look like job postings."""
    jobs = []
    seen_urls = set()

    # Strategy 1: Look for repeated structural patterns (list items, cards, table rows)
    # that contain links and look like job listings
    job_containers = []
    for selector in [
        'li a[href]', '.job a[href]', '.position a[href]',
        'tr a[href]', '.card a[href]', '.listing a[href]',
        '[class*="job"] a[href]', '[class*="position"] a[href]',
        '[class*="career"] a[href]', '[class*="opening"] a[href]',
        '[class*="posting"] a[href]', '[class*="vacancy"] a[href]',
    ]:
        found = soup.select(selector)
        if len(found) >= 3:  # At least 3 listings to consider it a pattern
            job_containers.extend(found)

    for link in job_containers:
        href = urljoin(base_url, link.get('href', ''))
        title = link.get_text(strip=True)

        if href in seen_urls or not title or len(title) < 5 or len(title) > 200:
            continue

        # Skip obvious non-job links
        lower_title = title.lower()
        if any(skip in lower_title for skip in [
            'sign in', 'log in', 'about', 'contact', 'privacy', 'terms',
            'cookie', 'home', 'back to', 'see all', 'load more', 'menu',
        ]):
            continue

        seen_urls.add(href)

        # Try to find location near the link
        location = ''
        parent = link.parent
        if parent:
            loc_el = parent.find(
                string=re.compile(r'remote|hybrid|on-?site|(?:san|new|los|seattle|austin|chicago)', re.I)
            )
            if loc_el:
                # Get the full text of the element containing the match
                location = loc_el.strip()[:100]

        jobs.append({'title': title, 'url': href, 'location': location})

    # Strategy 2: If strategy 1 found very few, try all links with job-like URL patterns
    if len(jobs) < 3:
        for link in soup.find_all('a', href=True):
            href = urljoin(base_url, link['href'])
            if href in seen_urls:
                continue

            # URL patterns that suggest job listings
            if not re.search(r'/jobs?/|/careers?/|/positions?/|/openings?/|/posting|/role|/apply', href, re.I):
                continue

            title = link.get_text(strip=True)
            if not title or len(title) < 5 or len(title) > 200:
                continue

            lower_title = title.lower()
            if any(skip in lower_title for skip in [
                'sign in', 'log in', 'about', 'contact', 'privacy',
                'terms', 'cookie', 'home', 'back to',
            ]):
                continue

            seen_urls.add(href)
            jobs.append({'title': title, 'url': href, 'location': ''})

    return jobs


# ─────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────

def scrape_careers_page(url, company_override=None):
    """Scrape a careers page and return structured job data."""
    print(f"Fetching: {url}")
    soup, final_url = fetch_page(url)

    # Detect platform
    platform = detect_platform(final_url, soup)
    print(f"Detected platform: {platform}")

    # Try to detect company name from page
    company = company_override
    if not company:
        # Try <title>, og:site_name, or domain
        title_tag = soup.find('title')
        og_site = soup.find('meta', property='og:site_name')
        if og_site and og_site.get('content'):
            company = og_site['content'].strip()
        elif title_tag:
            # Usually "Jobs at Company" or "Company Careers"
            t = title_tag.get_text(strip=True)
            for pattern in [r'(?:jobs?\s+at|careers?\s+at)\s+(.+?)(?:\s*[-|]|$)',
                            r'^(.+?)\s+(?:careers?|jobs?|openings?)',
                            r'^(.+?)\s*[-|]']:
                m = re.search(pattern, t, re.I)
                if m:
                    company = m.group(1).strip()
                    break
            if not company:
                company = urlparse(final_url).hostname.split('.')[0].title()
        else:
            company = urlparse(final_url).hostname.split('.')[0].title()

    print(f"Company: {company}")

    # Extract job listings based on platform
    extractors = {
        'greenhouse': extract_greenhouse,
        'lever': extract_lever,
        'ashby': extract_ashby,
    }
    extractor = extractors.get(platform, extract_generic)
    raw_jobs = extractor(soup, final_url)

    print(f"Found {len(raw_jobs)} job links on page")

    if not raw_jobs:
        print("\nNo jobs found. The page may use JavaScript rendering.")
        print("Try opening the page in Chrome, then use the browser console to")
        print("copy the page HTML after it loads, and save it as a local file.")
        return []

    # Deduplicate by URL
    seen = set()
    unique_jobs = []
    for j in raw_jobs:
        if j['url'] not in seen:
            seen.add(j['url'])
            unique_jobs.append(j)
    raw_jobs = unique_jobs
    print(f"After dedup: {len(raw_jobs)} unique positions")

    # Filter for SWE roles using title
    swe_jobs = []
    for j in raw_jobs:
        tl = j['title'].lower()
        if is_swe_role(tl) and not is_excluded_title(tl) and not is_excluded_role(tl):
            swe_jobs.append(j)

    print(f"After SWE filter: {len(swe_jobs)} matching roles")

    if not swe_jobs:
        print("\nNo SWE roles found after filtering. Showing all extracted titles:")
        for j in raw_jobs[:20]:
            print(f"  - {j['title']}")
        resp = input("\nWould you like to include ALL roles anyway? (y/n): ").strip().lower()
        if resp == 'y':
            swe_jobs = raw_jobs
        else:
            return []

    # Fetch full descriptions for each job
    print(f"\nFetching descriptions for {len(swe_jobs)} jobs...")
    processed_jobs = []
    for i, j in enumerate(swe_jobs):
        print(f"  [{i + 1}/{len(swe_jobs)}] {j['title']}", end='', flush=True)
        desc = fetch_description(j['url'])
        if desc:
            print(f" — {len(desc)} chars")
        else:
            print(" — no description found")

        # Score the job
        dl = desc.lower()
        tl = j['title'].lower()
        tech_score = calc_tech_score(dl)
        level_bonus = calc_level_bonus(tl)
        company_bonus = calc_company_bonus(company)
        total = tech_score + level_bonus + company_bonus
        tier = assign_tier(total)

        processed_jobs.append({
            'id': make_job_id(company, j['title'], j['url']),
            'company': company,
            'title': j['title'],
            'salary': 'N/A',
            'description': desc,
            'apply_link': j['url'],
            'job_link': j['url'],
            'posted_date': '',
            'location': j.get('location', ''),
            'work_type': '',
            'applicants': '',
            'score': total,
            'tier': tier,
            'import_date': date.today().isoformat(),
        })

        # Be polite — small delay between requests
        if i < len(swe_jobs) - 1:
            time.sleep(0.5)

    return processed_jobs


def main():
    parser = argparse.ArgumentParser(
        description='Scrape jobs from a company careers page and import to dashboard'
    )
    parser.add_argument('url', help='URL of the company careers/jobs page')
    parser.add_argument('--company', '-c', help='Override company name (auto-detected if omitted)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be imported without making changes')
    args = parser.parse_args()

    # Validate URL
    parsed = urlparse(args.url)
    if not parsed.scheme:
        args.url = 'https://' + args.url

    # Check if company is excluded
    if args.company and is_excluded_company(args.company):
        print(f"Warning: '{args.company}' matches an excluded company pattern.")
        resp = input("Continue anyway? (y/n): ").strip().lower()
        if resp != 'y':
            sys.exit(0)

    # Scrape
    jobs = scrape_careers_page(args.url, company_override=args.company)
    if not jobs:
        print("No jobs to import.")
        sys.exit(0)

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"SCRAPE RESULTS: {len(jobs)} positions from {jobs[0]['company']}")
    print(f"{'=' * 60}")
    tiers = {}
    for j in jobs:
        tiers.setdefault(j['tier'], []).append(j)
    for t in ["Strong Match", "Match", "Weak Match"]:
        count = len(tiers.get(t, []))
        if count:
            print(f"  {t}: {count}")
    print()

    # Show top matches
    top = sorted(jobs, key=lambda x: -x['score'])[:10]
    print("Top matches:")
    for j in top:
        print(f"  [{j['tier']}, Score:{j['score']}] {j['title']}")
        if j['location']:
            print(f"    Location: {j['location']}")
    print()

    if args.dry_run:
        print("Dry run — no changes made.")
        sys.exit(0)

    # Confirm import
    resp = input(f"Import {len(jobs)} jobs into the dashboard? (y/n): ").strip().lower()
    if resp != 'y':
        print("Cancelled.")
        sys.exit(0)

    # Load existing metadata and merge
    existing = []
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE) as f:
            existing = json.load(f)
        print(f"\nExisting jobs: {len(existing)}")

    existing_keys = set()
    for j in existing:
        if j.get('job_link'):
            existing_keys.add(j['job_link'])
        else:
            existing_keys.add(j.get('id', ''))

    added = [j for j in jobs if (j.get('job_link') or j.get('id', '')) not in existing_keys]
    all_jobs = existing + added
    print(f"New jobs to add: {len(added)}")
    print(f"Total after merge: {len(all_jobs)}")

    if not added:
        print("All jobs already exist in the dashboard. Nothing to add.")
        sys.exit(0)

    # Generate resume files
    created = generate_resume_files(added)
    print(f"Resume packages created: {created}")

    # Save metadata
    with open(METADATA_FILE, 'w') as f:
        json.dump(all_jobs, f, indent=2)
    print(f"Metadata saved: {METADATA_FILE}")

    # Rebuild dashboard
    rebuild_dashboard(all_jobs)
    print("Dashboard rebuilt!")
    print()
    print("Done! Restart the dashboard server to see the new jobs.")
    print("  cd 'Job Dashboard' && ./start_dashboard.sh")


if __name__ == '__main__':
    main()
