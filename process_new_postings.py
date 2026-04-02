#!/usr/bin/env python3
"""
process_new_postings.py — Job Search Pipeline

Processes LinkedIn CSV exports and produces:
  1. Tailored resume.txt + info.txt for each qualifying position
  2. A rebuilt dashboard.html with all jobs (old + new) embedded

Usage:
    python process_new_postings.py <path_to_csv>

Example:
    python process_new_postings.py job_postings_dump/csv_imports/linkedin_jobs_2026-03-15_filtered.csv

See README.md for how scoring, filtering, and tiers work.
"""

import csv
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import date

# ─────────────────────────────────────────────────────────
# CONFIGURATION — edit these paths if your folder structure changes
# ─────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESUMES_DIR = os.path.join(BASE_DIR, "resumes")
DASHBOARD_DIR = os.path.join(BASE_DIR, "dashboard")
DASHBOARD_HTML = os.path.join(DASHBOARD_DIR, "dashboard.html")
METADATA_FILE = os.path.join(RESUMES_DIR, "all_jobs_metadata.json")


def load_config():
    """Load config.json from the project root. Exit with helpful message if missing."""
    config_path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(config_path):
        print("Error: config.json not found.")
        print("Please copy config.example.json to config.json and fill in your details:")
        print("  cp config.example.json config.json")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


# Load config and populate all settings from it
_config = load_config()

EXCLUDE_COMPANIES = _config["filters"]["exclude_companies_containing"]
EXCLUDE_DESCRIPTION_PATTERNS = _config["filters"].get("exclude_description_patterns", [])
EXCLUDE_TITLE_KEYWORDS = _config["filters"]["exclude_title_keywords"]
EXCLUDE_ROLE_KEYWORDS = _config["filters"]["exclude_role_keywords"]

TECH_SCORES = _config["scoring"]["tech_keywords"]

# These require both keywords present in description
TECH_COMBO_SCORES = {
    ('rest', 'api'): 2,             # RESTful APIs
}

# Role-type bonuses (from description text)
ROLE_SCORES = {
    r'backend|back-end':                    1,
    r'full-stack|fullstack|full stack':     1,
}

TOP_COMPANIES = _config["scoring"]["top_companies"]

_tier_thresholds = _config["scoring"]["tier_thresholds"]
TIER_STRONG = _tier_thresholds["strong"]
TIER_MATCH  = _tier_thresholds["match"]

# Build BULLETS dict from config experience entries
BULLETS = {
    key: entry["bullets"]
    for key, entry in _config["experience"].items()
}

BULLET_LIMITS = {
    key: entry["bullet_limit"]
    for key, entry in _config["experience"].items()
}

SKILLS_DEFAULT = _config["skills_template"]

BULLET_KEYWORD_PAIRS = [
    (pair[0], pair[1]) for pair in _config["bullet_keyword_pairs"]
]


# ═════════════════════════════════════════════════════════
# FUNCTIONS
# ═════════════════════════════════════════════════════════

def is_excluded_company(company):
    cl = company.lower().strip()
    return any(ex in cl for ex in EXCLUDE_COMPANIES)


def is_excluded_description(desc_lower):
    """Return True if the description contains staffing-related phrases."""
    return any(pat in desc_lower for pat in EXCLUDE_DESCRIPTION_PATTERNS)


def is_excluded_title(title_lower):
    return any(kw in title_lower for kw in EXCLUDE_TITLE_KEYWORDS)


def is_excluded_role(title_lower):
    return any(kw in title_lower for kw in EXCLUDE_ROLE_KEYWORDS)


def is_swe_role(title_lower):
    return bool(re.search(r'software|sde|backend|back.?end|full.?stack|engineer|forward deployed|fde', title_lower))


def calc_level_bonus(title_lower):
    """SWE II/III/Senior get +2 points."""
    if re.search(r'(ii|iii|2|3)\b', title_lower):
        return 2
    if 'senior' in title_lower or 'sr.' in title_lower:
        return 2
    return 0


def calc_tech_score(desc_lower):
    score = 0
    for pattern, points in TECH_SCORES.items():
        if re.search(pattern, desc_lower):
            score += points
    for (kw1, kw2), points in TECH_COMBO_SCORES.items():
        if kw1 in desc_lower and kw2 in desc_lower:
            score += points
    for pattern, points in ROLE_SCORES.items():
        if re.search(pattern, desc_lower):
            score += points
    return score


def calc_company_bonus(company):
    cl = company.lower()
    return 2 if any(tc in cl for tc in TOP_COMPANIES) else 0


def assign_tier(total_score):
    if total_score >= TIER_STRONG:
        return "Strong Match"
    elif total_score >= TIER_MATCH:
        return "Match"
    return "Weak Match"


def sanitize_dirname(s):
    s = re.sub(r'[^\w\s\-]', '', s.strip())
    return re.sub(r'\s+', '_', s).strip('_')


def make_job_id(company, title, job_link=''):
    # Use job_link for uniqueness when available, fall back to company|title
    if job_link:
        return hashlib.md5(job_link.encode()).hexdigest()[:12]
    return hashlib.md5(f"{company}|{title}".encode()).hexdigest()[:12]


def pick_bullets(desc, title):
    dl = desc.lower()
    tl = title.lower()
    scored = []
    for company, company_bullets in BULLETS.items():
        for bullet in company_bullets:
            bl = bullet.lower()
            score = 0
            for desc_kw, bullet_kw in BULLET_KEYWORD_PAIRS:
                if re.search(desc_kw, dl) and bullet_kw in bl:
                    score += 1
            if re.search(r'full-stack|fullstack|full stack', dl):
                if any(w in bl for w in ['frontend', 'react', 'angular', 'full-stack']):
                    score += 2
            if 'backend' in tl or 'back-end' in tl:
                if any(w in bl for w in ['backend', 'service', 'api']):
                    score += 1
            scored.append((score, company, bullet))
    scored.sort(key=lambda x: -x[0])

    selected = {c: [] for c in BULLETS}
    used = set()
    for score, company, bullet in scored:
        if bullet not in used and len(selected[company]) < BULLET_LIMITS[company]:
            selected[company].append(bullet)
            used.add(bullet)
    for company in selected:
        if len(selected[company]) < 2:
            for b in BULLETS[company]:
                if b not in used and len(selected[company]) < BULLET_LIMITS[company]:
                    selected[company].append(b)
                    used.add(b)
    return selected


def customize_skills(desc):
    dl = desc.lower()
    langs = SKILLS_DEFAULT["languages"]
    frameworks = SKILLS_DEFAULT["frameworks"]
    misc = SKILLS_DEFAULT["misc"]
    if 'go ' in dl or 'golang' in dl:
        langs = SKILLS_DEFAULT["languages"]
    elif 'java' in dl:
        # Put Java first if Java-focused role
        parts = [p.strip() for p in SKILLS_DEFAULT["languages"].split(',')]
        if 'Java' in parts and 'Go' in parts:
            parts.remove('Java')
            parts.insert(0, 'Java')
        langs = ', '.join(parts)
    if 'grpc' in dl or 'protobuf' in dl:
        fw_parts = [p.strip() for p in SKILLS_DEFAULT["frameworks"].split(',')]
        if 'gRPC' not in fw_parts:
            fw_parts.insert(1, 'gRPC')
        frameworks = ', '.join(fw_parts)
    if 'kafka' in dl:
        misc_parts = [p.strip() for p in SKILLS_DEFAULT["misc"].split(',')]
        if 'Kafka' not in misc_parts:
            misc_parts.append('Kafka')
        misc = ', '.join(misc_parts)
    return langs, frameworks, misc


def generate_resume_txt(job, selected_bullets, langs, frameworks, misc):
    company_order = list(BULLETS.keys())
    lines = [
        f"Position: {job['company']} - {job['title']}",
        "=" * 66, "",
        "SKILLS", "-" * 66,
        f"Programming Languages:  {langs}",
        f"Frameworks:             {frameworks}",
        f"Miscellaneous:          {misc}", "",
    ]
    for cn in company_order:
        cb = selected_bullets.get(cn, [])
        if cb:
            lines.append(cn)
            lines.append("-" * 66)
            for b in cb:
                lines.extend([f"* {b}", ""])
    return "\n".join(lines)


def generate_info_txt(job):
    lines = [
        f"Job Title: {job['title']}",
        f"Company: {job['company']}",
        f"Salary Range: {job['salary']}",
        f"Match Tier: {job['tier']}",
        f"Match Score: {job['score']}",
        f"Apply Link: {job['apply_link']}",
    ]
    if job.get('job_link'):
        lines.append(f"Job Link: {job['job_link']}")
    return "\n".join(lines)


def process_csv(csv_path, location=None):
    """Read CSV, filter, score, and return list of job dicts."""
    batch_label = date.today().isoformat()
    jobs = []
    seen = set()
    with open(csv_path) as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    # Auto-detect location from CSV's "Search Location" column if not provided via --location
    if not location and rows:
        csv_location = (rows[0].get('Search Location') or '').strip()
        if csv_location:
            location = csv_location
            print(f"📍 Auto-detected location from CSV: {location}")

    if location:
        batch_label += f' — {location}'

    for r in rows:
        company = r['Company'].strip()
        title = r['Job Title'].strip()
        job_link = (r.get('Job Link') or '').strip()
        # Dedup by job_link (unique per listing), fall back to company+title if no link
        key = job_link if job_link else (company, title)
        if key in seen:
            continue
        seen.add(key)

        if is_excluded_company(company):
            continue

        tl = title.lower()
        if is_excluded_title(tl) or is_excluded_role(tl):
            continue
        if not is_swe_role(tl):
            continue

        desc = r.get('Description', '') or ''
        dl = desc.lower()

        if is_excluded_description(dl):
            continue

        tech_score = calc_tech_score(dl)
        level_bonus = calc_level_bonus(tl)
        company_bonus = calc_company_bonus(company)
        total = tech_score + level_bonus + company_bonus
        tier = assign_tier(total)

        jobs.append({
            'id': make_job_id(company, title, job_link),
            'company': company,
            'title': title,
            'salary': r.get('Salary', '') or 'N/A',
            'description': desc,
            'apply_link': r.get('Application Link', '') or '',
            'job_link': r.get('Job Link', '') or '',
            'posted_date': r.get('Posted Date', ''),
            'location': r.get('Location', ''),
            'work_type': r.get('Work Type', ''),
            'applicants': r.get('Applicants', ''),
            'score': total,
            'tier': tier,
            'import_date': batch_label,
        })
    return jobs


def generate_resume_files(jobs):
    """Create resume.txt + info.txt for each job under resumes/."""
    created = 0
    for job in jobs:
        company_dir = sanitize_dirname(job['company'])
        title_dir = sanitize_dirname(job['title'])
        base_dir = os.path.join(RESUMES_DIR, company_dir, title_dir)

        # If directory already has a resume, append the job ID to make it unique
        out_dir = base_dir
        if os.path.exists(os.path.join(out_dir, "resume.txt")):
            out_dir = base_dir + "_" + job['id']

        os.makedirs(out_dir, exist_ok=True)
        bullets = pick_bullets(job['description'], job['title'])
        langs, frameworks, misc = customize_skills(job['description'])

        with open(os.path.join(out_dir, "resume.txt"), 'w') as f:
            f.write(generate_resume_txt(job, bullets, langs, frameworks, misc))
        with open(os.path.join(out_dir, "info.txt"), 'w') as f:
            f.write(generate_info_txt(job))
        created += 1
    return created


def rebuild_dashboard(all_jobs):
    """Regenerate dashboard.html with all jobs embedded."""
    # Strip description from embedded data (too large, not needed in dashboard)
    dashboard_jobs = []
    for j in all_jobs:
        # Ensure every job has an id
        if 'id' not in j or not j['id']:
            j['id'] = make_job_id(j['company'], j['title'], j.get('job_link', ''))
        dj = {k: v for k, v in j.items() if k != 'description'}
        company_dir = sanitize_dirname(j['company'])
        title_dir = sanitize_dirname(j['title'])
        # Check if resume lives in the base dir or the id-suffixed dir
        base_path = os.path.join(RESUMES_DIR, company_dir, title_dir)
        id_path = base_path + "_" + j['id']
        if os.path.isfile(os.path.join(id_path, "resume.txt")):
            resume_rel = f"resumes/{company_dir}/{title_dir}_{j['id']}"
            dj['resume_path'] = resume_rel + "/resume.txt"
        else:
            resume_rel = f"resumes/{company_dir}/{title_dir}"
            dj['resume_path'] = resume_rel + "/resume.txt"
        # Auto-detect uploaded PDF resume
        pdf_abs = os.path.join(BASE_DIR, resume_rel, "resume.pdf")
        if os.path.isfile(pdf_abs):
            dj['pdf_path'] = resume_rel + "/resume.pdf"
        dashboard_jobs.append(dj)

    dashboard_jobs.sort(key=lambda x: -x['score'])
    jobs_json = json.dumps(dashboard_jobs, ensure_ascii=True)
    jobs_json = jobs_json.replace('</script>', '<\\/script>')

    # Compute batch stats grouped by import_date
    batch_map = {}
    for j in dashboard_jobs:
        d = j.get('import_date', 'Unknown')
        if d not in batch_map:
            batch_map[d] = {'date': d, 'total': 0, 'strong': 0, 'match': 0, 'weak': 0}
        batch_map[d]['total'] += 1
        t = j.get('tier', '')
        if t == 'Strong Match':
            batch_map[d]['strong'] += 1
        elif t == 'Match':
            batch_map[d]['match'] += 1
        else:
            batch_map[d]['weak'] += 1
    batch_stats = sorted(batch_map.values(), key=lambda x: x['date'])
    batch_stats_json = json.dumps(batch_stats, ensure_ascii=True)

    # Read current dashboard as template
    with open(DASHBOARD_HTML, 'r') as f:
        html = f.read()

    # Replace the JOBS_DATA constant
    # Use re.search + string slicing instead of re.sub to avoid
    # unicode escape sequences in JSON being misinterpreted as backreferences
    pattern = r'const JOBS_DATA = \[.*?\];'
    match = re.search(pattern, html, flags=re.DOTALL)
    if match:
        replacement = f'const JOBS_DATA = {jobs_json};'
        html = html[:match.start()] + replacement + html[match.end():]
    else:
        print("Warning: Could not find JOBS_DATA in dashboard.html")

    # Replace the BATCH_STATS constant
    pattern2 = r'const BATCH_STATS = \[.*?\];'
    match2 = re.search(pattern2, html, flags=re.DOTALL)
    if match2:
        replacement2 = f'const BATCH_STATS = {batch_stats_json};'
        html = html[:match2.start()] + replacement2 + html[match2.end():]
    else:
        print("Warning: Could not find BATCH_STATS in dashboard.html")

    with open(DASHBOARD_HTML, 'w') as f:
        f.write(html)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Process LinkedIn job CSV into the pipeline.')
    parser.add_argument('csv', help='Path to the CSV file')
    parser.add_argument('--location', '-l', default=None,
                        help='Metro area tag for this batch (e.g., "Seattle", "NYC"). '
                             'Appears in Batch History as "2026-03-27 — Seattle". '
                             'Auto-detected from CSV "Search Location" column if omitted.')
    args = parser.parse_args()

    csv_path = args.csv
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(BASE_DIR, csv_path)

    if not os.path.exists(csv_path):
        print(f"Error: File not found: {csv_path}")
        sys.exit(1)

    print(f"Processing: {csv_path}")
    print()

    # Step 1: Process new CSV
    new_jobs = process_csv(csv_path, location=args.location)
    print(f"New CSV: {len(new_jobs)} qualifying positions found")
    tiers = {}
    for j in new_jobs:
        tiers.setdefault(j['tier'], []).append(j)
    for t in ["Strong Match", "Match", "Weak Match"]:
        print(f"  {t}: {len(tiers.get(t, []))}")
    print()

    # Step 2: Load existing metadata and merge (dedup by job_link, fall back to id)
    existing = []
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE) as f:
            existing = json.load(f)
        print(f"Existing jobs: {len(existing)}")

    existing_keys = set()
    for j in existing:
        if j.get('job_link'):
            existing_keys.add(j['job_link'])
        else:
            existing_keys.add(j.get('id', ''))
    added = [j for j in new_jobs if (j.get('job_link') or j.get('id', '')) not in existing_keys]
    all_jobs = existing + added
    print(f"New jobs to add: {len(added)}")
    print(f"Total after merge: {len(all_jobs)}")
    print()

    # Step 3: Generate resume files for new jobs
    created = generate_resume_files(added)
    print(f"Resume packages created: {created}")

    # Step 4: Save updated metadata
    with open(METADATA_FILE, 'w') as f:
        json.dump(all_jobs, f, indent=2)
    print(f"Metadata saved: {METADATA_FILE}")

    # Step 5: Rebuild dashboard
    rebuild_dashboard(all_jobs)
    print(f"Dashboard rebuilt: {DASHBOARD_HTML}")
    print()
    print("Done! Your dashboard_state.json (notes/statuses) is untouched.")
    print("Start the server with: cd dashboard && python3 server.py")


if __name__ == "__main__":
    main()
