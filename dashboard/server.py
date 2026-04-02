#!/usr/bin/env python3
"""
Job Search Dashboard Server
Serves the dashboard and auto-saves state to a JSON file on disk.

Usage:
    python server.py
    Then open http://localhost:8080 in your browser.
"""

import http.server
import html as html_module
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
import urllib.parse

PORT = 8080
DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(DIR)
STATE_FILE = os.path.join(DIR, "dashboard_state.json")
PLANS_FILE = os.path.join(DIR, "application_plans.json")
METADATA_FILE = os.path.join(PARENT_DIR, "resumes", "all_jobs_metadata.json")

# ─────────────────────────────────────────────────────────
# CONFIG LOADING
# ─────────────────────────────────────────────────────────

def _load_config():
    config_path = os.path.join(PARENT_DIR, "config.json")
    if not os.path.exists(config_path):
        print("Error: config.json not found in project root.")
        print("Please copy config.example.json to config.json and fill in your details:")
        print("  cp config.example.json config.json")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)

_config = _load_config()

# Applicant update state
_update_lock = threading.Lock()
_update_status = {"running": False, "progress": 0, "total": 0, "updated": 0, "failed": 0, "message": ""}

# Scrape careers state
_scrape_lock = threading.Lock()
_scrape_status = {"running": False, "progress": 0, "total": 0, "added": 0, "message": "", "jobs_found": []}

# Live check state
_live_check_lock = threading.Lock()
_live_check_status = {"running": False, "progress": 0, "total": 0, "closed": 0, "live": 0, "inconclusive": 0, "auto_updated": 0, "message": ""}

# Company info cache (in-memory, persists for server lifetime)
_company_cache = {}
_company_cache_lock = threading.Lock()
COMPANY_CACHE_FILE = os.path.join(DIR, "company_cache.json")

def _load_company_cache():
    global _company_cache
    if os.path.exists(COMPANY_CACHE_FILE):
        try:
            with open(COMPANY_CACHE_FILE) as f:
                _company_cache = json.load(f)
        except Exception:
            _company_cache = {}

def _save_company_cache():
    try:
        with open(COMPANY_CACHE_FILE, "w") as f:
            json.dump(_company_cache, f, indent=2)
    except Exception:
        pass

_load_company_cache()


def _fetch_company_info(company_name):
    """Fetch company summary from Wikipedia and logo from Clearbit."""
    key = company_name.strip().lower()
    with _company_cache_lock:
        if key in _company_cache:
            return _company_cache[key]

    result = {"name": company_name, "summary": "", "logo_url": "", "known_for": "", "industry": "", "website": ""}

    # --- Wikipedia summary ---
    try:
        search_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query",
            "list": "search",
            "srsearch": company_name + " company",
            "srlimit": 1,
            "format": "json"
        })
        req = urllib.request.Request(search_url, headers={"User-Agent": "JobDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        results = data.get("query", {}).get("search", [])
        if results:
            title = results[0]["title"]
            summary_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
                "action": "query",
                "prop": "extracts",
                "exintro": True,
                "explaintext": True,
                "exsentences": 4,
                "titles": title,
                "format": "json"
            })
            req2 = urllib.request.Request(summary_url, headers={"User-Agent": "JobDashboard/1.0"})
            with urllib.request.urlopen(req2, timeout=8) as resp2:
                data2 = json.loads(resp2.read())
            pages = data2.get("query", {}).get("pages", {})
            for page in pages.values():
                extract = page.get("extract", "")
                if extract:
                    result["summary"] = extract.strip()
                    break
    except Exception:
        pass

    # --- Derive "known for" and industry from the summary ---
    if result["summary"]:
        sentences = result["summary"].split(". ")
        # First sentence usually says what the company does
        if sentences:
            result["known_for"] = sentences[0].rstrip(".") + "."
        # Try to detect industry keywords
        text_lower = result["summary"].lower()
        industries = []
        industry_kw = {
            "technology": ["technology", "software", "tech", "computing", "saas", "cloud"],
            "finance": ["financial", "banking", "finance", "investment", "insurance"],
            "healthcare": ["health", "medical", "pharma", "biotech", "hospital"],
            "e-commerce": ["e-commerce", "ecommerce", "retail", "marketplace", "shopping"],
            "social media": ["social media", "social network"],
            "automotive": ["automotive", "automobile", "car", "vehicle", "ev"],
            "aerospace": ["aerospace", "space", "aviation", "defense"],
            "consulting": ["consulting", "advisory", "professional services"],
            "entertainment": ["entertainment", "media", "streaming", "gaming"],
            "telecommunications": ["telecom", "telecommunications", "wireless"],
            "energy": ["energy", "oil", "gas", "renewable", "solar"],
            "food & beverage": ["food", "beverage", "restaurant"],
        }
        for industry, keywords in industry_kw.items():
            if any(kw in text_lower for kw in keywords):
                industries.append(industry)
        result["industry"] = ", ".join(industries[:2]) if industries else ""

    # --- Website guess for logo ---
    # Try common patterns
    clean = re.sub(r'[^a-zA-Z0-9]', '', company_name.lower().strip())
    result["website"] = clean + ".com"

    # Clearbit logo API (free, no auth)
    result["logo_url"] = f"https://logo.clearbit.com/{result['website']}"

    with _company_cache_lock:
        _company_cache[key] = result
    _save_company_cache()
    return result


def _fetch_applicants_curl(linkedin_job_id):
    """Fetch applicant count from public LinkedIn job page."""
    url = f"https://www.linkedin.com/jobs/view/{linkedin_job_id}/"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15
        )
        m = re.search(r'((?:Over\s+)?[\d,]+\s+applicants?)', result.stdout, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r'(Be among the first\s+\d+\s+applicants?)', result.stdout, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _run_applicant_update():
    """Background thread: curl each job's public page for applicant counts."""
    global _update_status
    import time
    from concurrent.futures import ThreadPoolExecutor

    with open(METADATA_FILE) as f:
        jobs = json.load(f)

    state = load_state()

    def _parse_applicant_count(s):
        """Extract numeric count from strings like '142 applicants', 'Over 200 applicants'."""
        if not s:
            return 0
        m2 = re.search(r'([\d,]+)', str(s))
        return int(m2.group(1).replace(',', '')) if m2 else 0

    # Only update jobs with < 500 applicants or no count at all
    pairs = []
    skipped = 0
    for j in jobs:
        m = re.search(r'/view/(\d+)', j.get('job_link', ''))
        if not m or not j.get('id'):
            continue
        job_id = j['id']
        # Check current count from state first, then from metadata
        current = state.get(job_id, {}).get('applicants') or j.get('applicants', '')
        count = _parse_applicant_count(current)
        if count >= 500:
            skipped += 1
            continue
        pairs.append((job_id, m.group(1)))

    with _update_lock:
        _update_status = {"running": True, "progress": 0, "total": len(pairs), "updated": 0, "failed": 0, "message": f"Running... ({skipped} skipped with 500+ applicants)"}

    batch_size = 15
    delay = 0.5

    for i in range(0, len(pairs), batch_size):
        if not _update_status["running"]:
            break  # cancelled

        batch = pairs[i:i + batch_size]

        # Run curl requests in parallel within the batch
        def fetch_one(pair):
            job_id, linkedin_id = pair
            applicants = _fetch_applicants_curl(linkedin_id)
            return job_id, applicants

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            results = list(executor.map(fetch_one, batch))

        for job_id, applicants in results:
            with _update_lock:
                _update_status["progress"] += 1
                if applicants:
                    if job_id not in state:
                        state[job_id] = {}
                    state[job_id]["applicants"] = applicants
                    _update_status["updated"] += 1
                else:
                    _update_status["failed"] += 1

        # Save after every batch so dashboard can pick up changes in real time
        save_state(state)

        if i + batch_size < len(pairs):
            time.sleep(delay)

    save_state(state)
    with _update_lock:
        _update_status["running"] = False
        _update_status["message"] = f"Done! {_update_status['updated']} updated, {_update_status['failed']} failed."


def _call_ollama(messages, model="qwen2.5:14b"):
    """Call Ollama's local API for chat completions."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result.get("message", {}).get("content", "")


_BULLET_POOL = {
    key: entry["bullets"]
    for key, entry in _config["experience"].items()
}

_CANDIDATE_SKILLS = _config["candidate"]["skills"]


def _build_job_context(job_id):
    """Build context string for a job: metadata + description + resume + bullet pool."""
    try:
        with open(METADATA_FILE, "r") as f:
            meta = json.load(f)
        job = next((j for j in meta if j.get("id") == job_id), None)
        if not job:
            return None, None

        parts = []
        parts.append(f"Company: {job.get('company', 'Unknown')}")
        parts.append(f"Title: {job.get('title', 'Unknown')}")
        parts.append(f"Location: {job.get('location', 'Unknown')}")
        parts.append(f"Work Type: {job.get('work_type', 'Unknown')}")
        parts.append(f"Salary: {job.get('salary', 'N/A')}")
        parts.append(f"Score: {job.get('score', 0)} | Tier: {job.get('tier', 'Unknown')}")
        desc = job.get("description", "")
        if desc:
            parts.append(f"\nJOB DESCRIPTION:\n{desc}")

        # Load tailored resume for this specific job
        resume_text = None
        resume_path = job.get("resume_path")
        if resume_path:
            full_path = os.path.join(PARENT_DIR, resume_path)
            if os.path.isfile(full_path):
                try:
                    with open(full_path, "r") as f:
                        resume_text = f.read()
                except Exception:
                    pass

        return "\n".join(parts), resume_text
    except Exception:
        return None, None


def _format_bullet_pool():
    """Format the full bullet pool for the AI system prompt."""
    lines = []
    for company, bullets in _BULLET_POOL.items():
        lines.append(f"\n{company}:")
        for i, b in enumerate(bullets, 1):
            lines.append(f"  {i}. {b}")
    return "\n".join(lines)


def _check_job_live_linkedin(job_link):
    """Check if a LinkedIn job posting is still live. Returns 'closed', 'live', or 'inconclusive'.

    LinkedIn returns inconsistent responses to unauthenticated curl (tiny JS redirects vs full pages).
    We only trust 'closed' when we get a full page (>10KB) with explicit closed language.
    Short/empty responses are always 'inconclusive'.
    """
    m = re.search(r'/view/(\d+)', job_link)
    if not m:
        return "inconclusive"
    url = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15
        )
        body = result.stdout
        # LinkedIn often returns tiny JS redirect stubs (<5KB) — can't trust those
        if not body or len(body) < 10000:
            return "inconclusive"
        body_lower = body.lower()
        # Only mark closed with explicit text on a full page
        if "no longer accepting applications" in body_lower or "this job is no longer available" in body_lower:
            return "closed"
        if re.search(r'\d+\s+applicants?', body, re.IGNORECASE) or "Easy Apply" in body:
            return "live"
        if "qualifications" in body_lower or "responsibilities" in body_lower:
            return "live"
        return "inconclusive"
    except Exception:
        return "inconclusive"


def _check_apply_link(apply_link):
    """Check if a company apply link is still live. Returns 'closed', 'live', or 'inconclusive'.

    Only marks 'closed' on definitive HTTP 404/410 responses.
    HTTP 200 = live. Everything else = inconclusive.
    """
    if not apply_link:
        return "inconclusive"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", apply_link],
            capture_output=True, text=True, timeout=15
        )
        http_code = result.stdout.strip()
        if http_code in ("404", "410"):
            return "closed"
        if http_code == "200":
            return "live"
        return "inconclusive"
    except Exception:
        return "inconclusive"


INTERVIEW_STAGES = {"1st Interview", "2nd Interview", "Final", "Verbal Offer", "Offer", "Accepted"}

def _run_live_check(batch_filter=None):
    """Background thread: check if jobs are still live on LinkedIn and company portals."""
    global _live_check_status
    import time
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor

    with open(METADATA_FILE) as f:
        jobs = json.load(f)

    state = load_state()

    # Sort by import_date ascending (oldest first)
    jobs.sort(key=lambda j: j.get("import_date", ""))

    if batch_filter:
        jobs = [j for j in jobs if j.get("import_date") == batch_filter]

    pairs = []
    for j in jobs:
        job_id = j.get("id")
        job_link = j.get("job_link", "")
        apply_link = j.get("apply_link", "")
        if not job_id or not job_link:
            continue
        current_status = state.get(job_id, {}).get("status")
        pairs.append((job_id, job_link, apply_link, current_status, state.get(job_id, {}).get("timestamps", {})))

    with _live_check_lock:
        _live_check_status = {
            "running": True, "progress": 0, "total": len(pairs),
            "closed": 0, "live": 0, "inconclusive": 0, "auto_updated": 0,
            "message": f"Checking {len(pairs)} jobs..."
        }

    batch_size = 10
    delay = 1.5
    now_iso = datetime.now(timezone.utc).isoformat()

    for i in range(0, len(pairs), batch_size):
        if not _live_check_status["running"]:
            break

        batch = pairs[i:i + batch_size]

        def check_one(pair):
            job_id, job_link, apply_link, current_status, timestamps = pair
            is_linkedin = "/linkedin.com/" in job_link
            if is_linkedin:
                result = _check_job_live_linkedin(job_link)
            else:
                # Non-LinkedIn job link: check it as an apply link directly
                result = _check_apply_link(job_link)
            if result == "inconclusive" and apply_link and apply_link != job_link:
                apply_result = _check_apply_link(apply_link)
                if apply_result == "closed":
                    result = "closed"
                elif apply_result == "live":
                    result = "live"
            return job_id, result, current_status, timestamps

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            results = list(executor.map(check_one, batch))

        for job_id, result, current_status, timestamps in results:
            if job_id not in state:
                state[job_id] = {}
            state[job_id]["live_status"] = result
            state[job_id]["live_status_checked"] = now_iso

            auto_updated = False
            if result == "closed":
                has_interviews = any(k in INTERVIEW_STAGES for k in timestamps)
                if current_status == "Applied" and not has_interviews:
                    state[job_id]["status"] = "Applied, Closed, No Update"
                    auto_updated = True
                elif current_status in (None, "Not Applied", ""):
                    state[job_id]["status"] = "Position Closed, No Application Submitted"
                    auto_updated = True

            with _live_check_lock:
                _live_check_status["progress"] += 1
                if result == "closed":
                    _live_check_status["closed"] += 1
                elif result == "live":
                    _live_check_status["live"] += 1
                else:
                    _live_check_status["inconclusive"] += 1
                if auto_updated:
                    _live_check_status["auto_updated"] += 1

        save_state(state)

        if i + batch_size < len(pairs):
            time.sleep(delay)

    save_state(state)
    with _live_check_lock:
        _live_check_status["running"] = False
        s = _live_check_status
        _live_check_status["message"] = f"Done! {s['closed']} closed, {s['live']} live, {s['inconclusive']} inconclusive. {s['auto_updated']} auto-updated."


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_plans():
    if os.path.exists(PLANS_FILE):
        with open(PLANS_FILE, "r") as f:
            return json.load(f)
    return []


def save_plans(plans):
    with open(PLANS_FILE, "w") as f:
        json.dump(plans, f, indent=2)


def _run_scrape_careers(url, company_override=None):
    """Background thread: scrape a careers page and import jobs."""
    global _scrape_status
    import time as _time

    # Import pipeline functions
    sys.path.insert(0, PARENT_DIR)
    from process_new_postings import (
        is_excluded_title, is_excluded_role, is_swe_role,
        calc_tech_score, calc_level_bonus, calc_company_bonus, assign_tier,
        make_job_id, generate_resume_files, rebuild_dashboard,
    )
    from scrape_careers_page import (
        fetch_page, detect_platform, extract_greenhouse, extract_lever,
        extract_ashby, extract_generic, fetch_description,
    )
    from datetime import date as _date
    from urllib.parse import urlparse as _urlparse

    try:
        with _scrape_lock:
            _scrape_status = {"running": True, "progress": 0, "total": 0, "added": 0,
                              "message": "Fetching careers page...", "jobs_found": []}

        soup, final_url = fetch_page(url)
        platform = detect_platform(final_url, soup)

        # Detect company name
        company = company_override
        if not company:
            og_site = soup.find('meta', property='og:site_name')
            title_tag = soup.find('title')
            if og_site and og_site.get('content'):
                company = og_site['content'].strip()
            elif title_tag:
                import re as _re
                t = title_tag.get_text(strip=True)
                for pat in [r'(?:jobs?\s+at|careers?\s+at)\s+(.+?)(?:\s*[-|]|$)',
                            r'^(.+?)\s+(?:careers?|jobs?|openings?)',
                            r'^(.+?)\s*[-|]']:
                    m = _re.search(pat, t, _re.I)
                    if m:
                        company = m.group(1).strip()
                        break
            if not company:
                company = _urlparse(final_url).hostname.split('.')[0].title()

        with _scrape_lock:
            _scrape_status["message"] = f"Detected {platform} — extracting jobs from {company}..."

        extractors = {
            'greenhouse': extract_greenhouse,
            'lever': extract_lever,
            'ashby': extract_ashby,
        }
        extractor = extractors.get(platform, extract_generic)
        raw_jobs = extractor(soup, final_url)

        # Deduplicate
        seen = set()
        unique = []
        for j in raw_jobs:
            if j['url'] not in seen:
                seen.add(j['url'])
                unique.append(j)
        raw_jobs = unique

        # Filter for SWE roles
        swe_jobs = []
        for j in raw_jobs:
            tl = j['title'].lower()
            if is_swe_role(tl) and not is_excluded_title(tl) and not is_excluded_role(tl):
                swe_jobs.append(j)

        if not swe_jobs:
            # If no SWE roles, include all
            swe_jobs = raw_jobs

        with _scrape_lock:
            _scrape_status["total"] = len(swe_jobs)
            _scrape_status["message"] = f"Found {len(swe_jobs)} positions at {company}. Fetching descriptions..."

        if not swe_jobs:
            with _scrape_lock:
                _scrape_status["running"] = False
                _scrape_status["message"] = f"No jobs found. The page may use JavaScript rendering. Try the console script instead."
            return

        # Fetch descriptions and score
        processed_jobs = []
        for i, j in enumerate(swe_jobs):
            desc = fetch_description(j['url'])
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
                'import_date': _date.today().isoformat(),
            })

            with _scrape_lock:
                _scrape_status["progress"] = i + 1
                _scrape_status["message"] = f"Scoring [{i+1}/{len(swe_jobs)}]: {j['title'][:50]}"

            if i < len(swe_jobs) - 1:
                _time.sleep(0.3)

        # Merge with existing metadata
        existing = []
        if os.path.exists(METADATA_FILE):
            with open(METADATA_FILE) as f:
                existing = json.load(f)

        existing_keys = set()
        for j in existing:
            if j.get('job_link'):
                existing_keys.add(j['job_link'])
            else:
                existing_keys.add(j.get('id', ''))

        added = [j for j in processed_jobs if (j.get('job_link') or j.get('id', '')) not in existing_keys]
        all_jobs = existing + added

        if added:
            # Generate resumes
            generate_resume_files(added)

            # Save metadata
            with open(METADATA_FILE, 'w') as f:
                json.dump(all_jobs, f, indent=2)

            # Rebuild dashboard
            rebuild_dashboard(all_jobs)

        tiers = {}
        for j in processed_jobs:
            tiers.setdefault(j['tier'], []).append(j['title'])

        tier_summary = ", ".join(f"{t}: {len(v)}" for t, v in sorted(tiers.items()))

        with _scrape_lock:
            _scrape_status["running"] = False
            _scrape_status["added"] = len(added)
            _scrape_status["jobs_found"] = [
                {"title": j["title"], "tier": j["tier"], "score": j["score"], "location": j.get("location", "")}
                for j in sorted(processed_jobs, key=lambda x: -x["score"])[:20]
            ]
            _scrape_status["message"] = (
                f"Done! {len(processed_jobs)} positions found, {len(added)} new added. {tier_summary}. "
                f"Reload the page to see them."
            )

    except Exception as e:
        with _scrape_lock:
            _scrape_status["running"] = False
            _scrape_status["message"] = f"Error: {str(e)}"


def _extract_relevant_description(description, max_chars=800):
    """Extract the most relevant parts of a job description, skipping company boilerplate.

    Keeps: responsibilities, requirements, qualifications, skills, team info, role details.
    Skips: company overview, benefits, EEO statements, salary disclaimers, legal boilerplate.
    """
    if not description:
        return ''

    # Patterns that mark the START of relevant content
    relevant_pattern = re.compile(
        r'(what you\'?ll do|what you\'?ll work on|what you will do|your role|the role'
        r'|responsibilities|key responsibilities|primary responsibilities|core responsibilities'
        r'|requirements|key requirements|minimum requirements'
        r'|qualifications|required qualifications|minimum qualifications|basic qualifications'
        r'|skills|required skills|technical skills|tech stack'
        r'|who you are|about the role|about the team|about this team|team intro'
        r'|position description|position summary|job summary'
        r'|what we\'?re looking for|what you\'?ll need|what you bring'
        r'|in this role|as a .{5,30} you will'
        r'|day in the life|a day in the life'
        r'|nice to have|preferred qualifications)',
        re.IGNORECASE
    )

    # Patterns that mark the END / irrelevant sections
    skip_pattern = re.compile(
        r'(about us|about the company|who we are|our mission'
        r'|benefits|total rewards|compensation|salary range|pay range|pay transparency'
        r'|equal opportunity|eeo |diversity|accommodation'
        r'|privacy notice|employee applicant|disclaimer|legal notice'
        r'|how to apply|next steps|interview process'
        r'|please note that|note:)',
        re.IGNORECASE
    )

    # Find the first relevant section marker
    match = relevant_pattern.search(description)
    if match:
        # Start from this marker
        start = match.start()
        relevant_text = description[start:]

        # Now find where irrelevant content begins and cut it off
        skip_match = skip_pattern.search(relevant_text)
        if skip_match:
            # Only cut if the skip marker is after some content
            if skip_match.start() > 100:
                relevant_text = relevant_text[:skip_match.start()]
    else:
        # No clear section headers found - skip first ~30% as company overview
        total = len(description)
        start = min(total // 3, 500)
        # Try to start at a sentence boundary
        boundary = description.find('.  ', start)
        if boundary > 0 and boundary < start + 200:
            relevant_text = description[boundary + 3:]
        else:
            relevant_text = description[start:]

    # Clean up whitespace
    relevant_text = re.sub(r'\s{2,}', '  ', relevant_text).strip()

    # Truncate to max_chars at a word boundary
    if len(relevant_text) > max_chars:
        relevant_text = relevant_text[:max_chars].rsplit(' ', 1)[0] + '...'

    return relevant_text


def generate_plan_pdf(plan, jobs_lookup):
    """Generate a color-coded PDF for an application plan."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )

    pdf_path = os.path.join(DIR, f"plan_{plan['id']}.pdf")

    doc = SimpleDocTemplate(
        pdf_path, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        'PlanTitle', parent=styles['Title'],
        fontSize=22, spaceAfter=4, textColor=colors.HexColor('#1e3a5f'),
    ))
    styles.add(ParagraphStyle(
        'PlanSubtitle', parent=styles['Normal'],
        fontSize=11, textColor=colors.HexColor('#64748b'), spaceAfter=16,
    ))
    styles.add(ParagraphStyle(
        'JobTitle', parent=styles['Normal'],
        fontSize=11, textColor=colors.HexColor('#1e293b'), leading=14,
    ))
    styles.add(ParagraphStyle(
        'JobDetail', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#475569'), leading=12,
    ))
    styles.add(ParagraphStyle(
        'Notes', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#6b21a8'), leading=12,
        fontName='Helvetica-Oblique',
    ))
    styles.add(ParagraphStyle(
        'JobID', parent=styles['Normal'],
        fontSize=8, textColor=colors.HexColor('#94a3b8'), leading=10,
    ))
    styles.add(ParagraphStyle(
        'Description', parent=styles['Normal'],
        fontSize=8, textColor=colors.HexColor('#334155'), leading=11,
    ))

    TIER_COLORS = {
        'Strong Match': colors.HexColor('#16a34a'),
        'Match': colors.HexColor('#2563eb'),
        'Weak Match': colors.HexColor('#d97706'),
    }
    TIER_BG = {
        'Strong Match': colors.HexColor('#f0fdf4'),
        'Match': colors.HexColor('#eff6ff'),
        'Weak Match': colors.HexColor('#fffbeb'),
    }

    story = []

    # Title
    story.append(Paragraph(f"Application Plan", styles['PlanTitle']))
    job_count = len(plan.get('jobs', []))
    story.append(Paragraph(
        f"Date: <b>{plan['date']}</b>  &bull;  {job_count} position{'s' if job_count != 1 else ''}",
        styles['PlanSubtitle'],
    ))
    if plan.get('title'):
        story.append(Paragraph(plan['title'], styles['Heading2']))
        story.append(Spacer(1, 6))

    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 12))

    # Job cards
    for idx, plan_job in enumerate(plan.get('jobs', [])):
        job_id = plan_job.get('id', '')
        job = jobs_lookup.get(job_id, {})
        tier = job.get('tier', 'Weak Match')
        tier_color = TIER_COLORS.get(tier, colors.gray)
        tier_bg = TIER_BG.get(tier, colors.HexColor('#f8fafc'))

        # Checkbox + number
        num = f"<b>{idx + 1}.</b>"

        company = job.get('company', plan_job.get('company', 'Unknown'))
        title = job.get('title', plan_job.get('title', 'Unknown'))
        score = job.get('score', 0)
        salary = job.get('salary', 'N/A')
        location = job.get('location', '')
        work_type = job.get('work_type', '')
        apply_link = job.get('apply_link', '')
        job_link = job.get('job_link', '')
        resume_path = job.get('resume_path', '')
        notes = plan_job.get('notes', '')

        # Build table rows for this job card
        card_data = []

        # Row 1: Number, Company + Title, Tier badge
        header_text = f"{num} <b>{company}</b> - {title}"
        tier_text = f"<font color='white'><b> {tier} (Score: {score}) </b></font>"

        card_data.append([
            Paragraph(header_text, styles['JobTitle']),
            Paragraph(tier_text, ParagraphStyle('tier_badge', parent=styles['Normal'],
                fontSize=8, alignment=2, textColor=colors.white)),
        ])

        # Row 2: Job ID (for quick dashboard search)
        if job_id:
            card_data.append([Paragraph(f"ID: <b>{job_id}</b>  (search this in the Jobs tab)", styles['JobID']), ''])

        # Row 3: Details
        details = []
        if salary and salary != 'N/A':
            details.append(f"Salary: {salary}")
        if location:
            details.append(f"Location: {location}")
        if work_type:
            details.append(f"Type: {work_type}")
        detail_str = "  |  ".join(details) if details else "No details available"
        card_data.append([Paragraph(detail_str, styles['JobDetail']), ''])

        # Row 4: Description (relevant parts only - responsibilities, requirements, skills)
        raw_description = job.get('description', '')
        description = _extract_relevant_description(raw_description, max_chars=800)
        if description:
            # Escape XML special chars for reportlab
            description = description.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            card_data.append([Paragraph(f"<b>Role Details:</b> {description}", styles['Description']), ''])

        # Row 5: Links
        links = []
        if apply_link:
            links.append(f'<link href="{apply_link}"><u>Apply</u></link>')
        if job_link:
            links.append(f'<link href="{job_link}"><u>LinkedIn</u></link>')
        if resume_path:
            links.append(f"Resume: {resume_path}")
        if links:
            card_data.append([Paragraph("  |  ".join(links), styles['JobDetail']), ''])

        # Row 6: Notes
        if notes:
            card_data.append([Paragraph(f"Notes: {notes}", styles['Notes']), ''])

        # Row 5: Checkbox
        card_data.append([Paragraph("[ ] Applied", styles['JobDetail']), ''])

        col_widths = [5.4 * inch, 1.7 * inch]
        t = Table(card_data, colWidths=col_widths)
        style_cmds = [
            ('BACKGROUND', (0, 0), (-1, 0), tier_bg),
            ('BACKGROUND', (1, 0), (1, 0), tier_color),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#cbd5e1')),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#e2e8f0')),
        ]
        # Span all rows after the header across both columns
        for r in range(1, len(card_data)):
            style_cmds.append(('SPAN', (0, r), (1, r)))
        t.setStyle(TableStyle(style_cmds))

        story.append(t)
        story.append(Spacer(1, 10))

    # Summary footer
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 8))

    tier_counts = {}
    for pj in plan.get('jobs', []):
        j = jobs_lookup.get(pj.get('id', ''), {})
        t = j.get('tier', 'Unknown')
        tier_counts[t] = tier_counts.get(t, 0) + 1

    summary_parts = [f"<b>Summary:</b> {job_count} positions"]
    for t_name in ['Strong Match', 'Match', 'Weak Match']:
        if tier_counts.get(t_name, 0) > 0:
            hex_color = '#16a34a' if t_name == 'Strong Match' else '#2563eb' if t_name == 'Match' else '#d97706'
            summary_parts.append(f"<font color='{hex_color}'>{t_name}: {tier_counts[t_name]}</font>")
    story.append(Paragraph("  |  ".join(summary_parts), styles['JobDetail']))

    doc.build(story)
    return pdf_path


def format_job_description(raw):
    """Format a raw LinkedIn job description into readable HTML.

    LinkedIn's Voyager API strips newlines, leaving double-space as the
    only separator between sections. This function reconstructs structure
    by detecting section headers and bullet-like patterns.
    """
    import html as html_mod
    if not raw or not raw.strip():
        return "<p>No description available.</p>"

    # Normalize curly quotes/apostrophes to ASCII
    raw = raw.replace('\u2018', "'").replace('\u2019', "'")
    raw = raw.replace('\u201c', '"').replace('\u201d', '"')

    # Split on double-space boundaries (LinkedIn's section separator)
    chunks = re.split(r'  +', raw.strip())

    # Common section header patterns
    header_re = re.compile(
        r"^(?:about (?:the |this )?(?:role|position|team|company|job|us|you)|"
        r"what you'?ll (?:do|bring|need|work on)|"
        r"who you are|who we are|"
        r"(?:key |core |your )?responsibilities|"
        r"(?:minimum |preferred |basic |required |desired )?(?:qualifications|requirements|skills|experience)|"
        r"nice to have|bonus points|preferred skills|"
        r"(?:what )?we (?:offer|provide|'re looking for)|"
        r"benefits(?: and perks)?|compensation|perks|"
        r"why (?:join|work)|our (?:team|culture|mission)|"
        r"tech(?:nology)? stack|tools we use|"
        r"job (?:description|details|summary|category)|"
        r"equal opportunity|eeo|diversity|"
        r"(?:location|pay|salary|base|total) (?:requirement|range|details)?|"
        r"how to apply|additional information|"
        r"the (?:role|team|opportunity|impact)|"
        r"you(?:'ll| will) (?:have|be)|in this role)s?\s*:?\s*$",
        re.IGNORECASE
    )

    # Detect bullet-like items (start with •, -, *, or numbered)
    bullet_re = re.compile(r'^(?:[•\-\*]|\d+[.)]\s)\s*')

    # Common sentence-starting verbs for detecting run-together bullets
    _verbs = ('Design|Build|Develop|Create|Implement|Ensure|Collaborate|Leverage|'
              'Maintain|Lead|Drive|Work|Manage|Write|Test|Deploy|Configure|Monitor|'
              'Support|Architect|Define|Integrate|Evaluate|Establish|Own|Partner|'
              'Contribute|Participate|Apply|Optimize|Analyze|Research|Coordinate|'
              'Deliver|Provide|Identify|Review|Conduct|Troubleshoot|Automate|'
              'Mentor|Scale|Ship|Debug|Refactor|Improve|Reduce|Increase|Execute|'
              'Perform|Assist|Help|Communicate|Report|Document|Plan|Resolve|'
              'Investigate|Utilize|Adopt|Use|Set|Engage|Serve|Gather|Operate|'
              'Demonstrate|Translate|Navigate|Enable|Transform|Propose|Assess|'
              'Craft|Champion|Facilitate|Spearhead|Streamline|Oversee|Prioritize')
    run_together_re = re.compile(r'(?<=[a-zA-Z)])(?=(?:' + _verbs + r') )')

    lines = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        # Check bullets first (before header, since "• Python" could match title-case heuristic)
        if bullet_re.match(chunk):
            lines.append(('bullet', bullet_re.sub('', chunk)))
        # Then check if this chunk is a section header
        elif header_re.match(chunk) or (len(chunk) < 60 and chunk.endswith(':')) or (len(chunk) < 50 and chunk == chunk.title() and not chunk.endswith('.')):
            lines.append(('header', chunk.rstrip(':')))
        else:
            # Try to detect run-together bullet items within a chunk
            split_bullets = run_together_re.split(chunk)
            if len(split_bullets) > 2 and all(len(s.strip()) > 15 for s in split_bullets):
                for b in split_bullets:
                    lines.append(('bullet', b.strip()))
            else:
                lines.append(('text', chunk))

    # Build HTML
    parts = []
    in_list = False
    for kind, content in lines:
        escaped = html_mod.escape(content)
        if kind == 'header':
            if in_list:
                parts.append('</ul>')
                in_list = False
            parts.append(f'<h4 style="font-weight:600;font-size:0.8rem;color:#374151;margin:0.75rem 0 0.25rem 0;">{escaped}</h4>')
        elif kind == 'bullet':
            if not in_list:
                parts.append('<ul style="margin:0.25rem 0;padding-left:1.25rem;list-style:disc;">')
                in_list = True
            parts.append(f'<li style="font-size:0.75rem;color:#4b5563;margin-bottom:0.2rem;">{escaped}</li>')
        else:
            if in_list:
                parts.append('</ul>')
                in_list = False
            parts.append(f'<p style="font-size:0.75rem;color:#4b5563;margin:0.35rem 0;">{escaped}</p>')
    if in_list:
        parts.append('</ul>')

    return '\n'.join(parts)


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):
        if self.path == "/":
            self.path = "/dashboard.html"
        if self.path == "/api/config":
            payload = {
                "name": _config["candidate"]["name"],
                "linkedin_url": _config["candidate"]["linkedin_url"],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return
        if self.path == "/api/state":
            state = load_state()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
            return
        if self.path == "/api/update-status":
            with _update_lock:
                status = dict(_update_status)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
            return
        if self.path == "/api/live-check-status":
            with _live_check_lock:
                status = dict(_live_check_status)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
            return
        if self.path == "/api/jobs":
            # Return job IDs and their LinkedIn job links for the applicant update script
            try:
                with open(METADATA_FILE, "r") as f:
                    meta = json.load(f)
                jobs = [{"id": j.get("id", ""), "job_link": j.get("job_link", "")} for j in meta if j.get("job_link")]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(jobs).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/api/refresh-job/"):
            job_id = self.path[len("/api/refresh-job/"):].strip()
            if not job_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"Missing job ID"}')
                return
            try:
                with open(METADATA_FILE, "r") as f:
                    meta = json.load(f)
                job = next((j for j in meta if j.get("id") == job_id), None)
                if not job:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"Job not found"}')
                    return
                state = load_state()
                saved = state.get(job_id, {})
                result = dict(saved)
                # Fetch latest applicant count from LinkedIn
                job_link = job.get("job_link", "")
                m = re.search(r'/view/(\d+)', job_link)
                if m:
                    applicants = _fetch_applicants_curl(m.group(1))
                    if applicants:
                        result["applicants"] = applicants
                        if job_id not in state:
                            state[job_id] = {}
                        state[job_id]["applicants"] = applicants
                        save_state(state)
                # Also check live status
                is_linkedin = "/linkedin.com/" in job_link
                if is_linkedin:
                    live_result = _check_job_live_linkedin(job_link)
                else:
                    live_result = _check_apply_link(job_link)
                if live_result != "inconclusive":
                    result["live_status"] = live_result
                    state[job_id]["live_status"] = live_result
                    import datetime
                    state[job_id]["live_status_checked"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    result["live_status_checked"] = state[job_id]["live_status_checked"]
                    save_state(state)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/api/company-info/"):
            from urllib.parse import unquote
            company_name = unquote(self.path[len("/api/company-info/"):]).strip()
            if not company_name:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"Missing company name"}')
                return
            try:
                info = _fetch_company_info(company_name)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(info).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path == "/api/scrape-status":
            with _scrape_lock:
                status = dict(_scrape_status)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
            return
        if self.path == "/api/plans":
            plans = load_plans()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(plans).encode())
            return
        if self.path.startswith("/api/job-description/"):
            job_id = self.path[len("/api/job-description/"):]
            try:
                with open(METADATA_FILE, "r") as f:
                    meta = json.load(f)
                job = next((j for j in meta if j.get("id") == job_id), None)
                if not job:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"Job not found"}')
                    return
                raw = job.get("description", "")
                formatted = format_job_description(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"description": raw, "formatted": formatted}).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/api/plan-pdf/"):
            plan_id = self.path[len("/api/plan-pdf/"):]
            plans = load_plans()
            plan = next((p for p in plans if p['id'] == plan_id), None)
            if not plan:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"Plan not found"}')
                return
            try:
                with open(METADATA_FILE) as f:
                    meta = json.load(f)
                jobs_lookup = {}
                for j in meta:
                    if j.get('id'):
                        jobs_lookup[j['id']] = j
                pdf_path = generate_plan_pdf(plan, jobs_lookup)
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="application_plan_{plan["date"]}.pdf"')
                self.end_headers()
                with open(pdf_path, "rb") as f:
                    self.wfile.write(f.read())
                os.remove(pdf_path)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        # Serve resume files from the parent directory
        if self.path.startswith("/resumes/"):
            from urllib.parse import unquote
            rel = unquote(self.path[1:])  # strip leading /
            file_path = os.path.normpath(os.path.join(PARENT_DIR, rel))
            # Security: ensure path stays within PARENT_DIR
            if not file_path.startswith(PARENT_DIR):
                self.send_response(403)
                self.end_headers()
                return
            if os.path.isfile(file_path):
                self.send_response(200)
                if file_path.endswith(".pdf"):
                    self.send_header("Content-Type", "application/pdf")
                else:
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
                return
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/upload-resume/"):
            # Upload a PDF resume for a specific job
            # Path format: /api/upload-resume/{resume_path_base64}
            # The resume_path is the relative path to the resume directory (e.g. resumes/Google/SWE_III)
            from urllib.parse import unquote
            import base64
            try:
                encoded_path = self.path[len("/api/upload-resume/"):]
                resume_dir = base64.urlsafe_b64decode(encoded_path).decode("utf-8")
                full_dir = os.path.normpath(os.path.join(PARENT_DIR, resume_dir))
                # Security check
                if not full_dir.startswith(PARENT_DIR):
                    self.send_response(403)
                    self.end_headers()
                    return

                content_length = int(self.headers.get("Content-Length", 0))
                file_data = self.rfile.read(content_length)

                os.makedirs(full_dir, exist_ok=True)
                pdf_path = os.path.join(full_dir, "resume.pdf")
                with open(pdf_path, "wb") as f:
                    f.write(file_data)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "path": resume_dir + "/resume.pdf"}).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/api/state":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                job_id = data.get("id")
                job_state = data.get("state")
                if job_id and job_state:
                    all_state = load_state()
                    all_state[job_id] = job_state
                    save_state(all_state)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":"missing id or state"}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/api/plans":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                plans = json.loads(body)
                save_plans(plans)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/api/scrape-careers":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                url = data.get("url", "").strip()
                company = data.get("company", "").strip() or None
                if not url:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"URL is required"}')
                    return
                if _scrape_status.get("running"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true,"message":"Already running"}')
                else:
                    t = threading.Thread(target=_run_scrape_careers, args=(url, company), daemon=True)
                    t.start()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true,"message":"Started"}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/api/ai-chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            user_message = body.get("message", "")
            job_id = body.get("job_id")
            history = body.get("history", [])
            model = body.get("model", "qwen2.5:14b")

            if not user_message:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"No message provided"}')
                return

            # Build rich system prompt with job context, resume, and bullet pool
            _candidate_name = _config["candidate"]["name"]
            _candidate_summary = _config["candidate"].get("summary", "")
            _experience_contexts = ", ".join(
                entry.get("context", key)
                for key, entry in _config["experience"].items()
            )
            system_parts = [
                f"You are a job search assistant for a software engineer named {_candidate_name}.",
                f"{_candidate_name} has experience at: {_experience_contexts}.",
                f"Their skills include: {_CANDIDATE_SKILLS}",
                "",
                "YOUR ROLE:",
                f"- Analyze job postings and evaluate fit against {_candidate_name}'s actual experience",
                "- Suggest SPECIFIC resume bullet rewording or reordering based on job requirements",
                "- When suggesting resume changes, reference their ACTUAL bullets by quoting them and suggest concrete edits",
                "- For interview prep, tailor questions to the specific technologies and responsibilities in the job posting",
                "- Be direct and actionable — no generic advice",
                "",
                f"IMPORTANT: When discussing resume improvements, you must work with {_candidate_name}'s ACTUAL experience bullets listed below. You can suggest rephrasing them to better match a job, reordering them, or highlighting specific ones — but do NOT invent experience they don't have.",
            ]

            # Always include the bullet pool so the AI knows what's available
            system_parts.append(f"\n--- {_candidate_name.upper()}'S FULL EXPERIENCE BULLET POOL ---")
            system_parts.append("These are all the resume bullets available. The resume for each job is built by selecting and ordering from this pool:")
            system_parts.append(_format_bullet_pool())

            if job_id:
                job_context, resume_text = _build_job_context(job_id)
                if job_context:
                    system_parts.append("\n--- TARGET JOB POSTING ---\n" + job_context)
                if resume_text:
                    system_parts.append("\n--- CURRENT TAILORED RESUME FOR THIS JOB ---")
                    system_parts.append("This is the resume that was auto-generated for this specific job. Suggest improvements:")
                    system_parts.append(resume_text)

            messages = [{"role": "system", "content": "\n".join(system_parts)}]
            messages.extend(history)
            messages.append({"role": "user", "content": user_message})

            try:
                response = _call_ollama(messages, model=model)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"response": response, "model": model}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == "/api/run-live-check":
            if _live_check_status["running"]:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"message":"Already running"}')
            else:
                batch_filter = None
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    try:
                        body = json.loads(self.rfile.read(length))
                        batch_filter = body.get("batch")
                    except Exception:
                        pass
                t = threading.Thread(target=_run_live_check, args=(batch_filter,), daemon=True)
                t.start()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"message":"Started"}')
            return

        if self.path == "/api/run-update-applicants":
            if _update_status["running"]:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"message":"Already running"}')
            else:
                t = threading.Thread(target=_run_applicant_update, daemon=True)
                t.start()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"message":"Started"}')
            return

        if self.path == "/api/update-applicants":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                # Expects: {"updates": [{"id": "abc123", "applicants": "142 applicants"}, ...]}
                updates = data.get("updates", [])
                all_state = load_state()
                count = 0
                for u in updates:
                    job_id = u.get("id")
                    applicants = u.get("applicants")
                    if job_id and applicants is not None:
                        if job_id not in all_state:
                            all_state[job_id] = {}
                        all_state[job_id]["applicants"] = applicants
                        count += 1
                save_state(all_state)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "updated": count}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # Only log API calls, not static file serving
        try:
            if args and "/api/" in str(args[0]):
                super().log_message(format, *args)
        except Exception:
            pass


if __name__ == "__main__":
    print(f"Starting Job Dashboard server...")
    print(f"State file: {STATE_FILE}")
    print(f"Open http://localhost:{PORT} in your browser")
    print(f"Press Ctrl+C to stop\n")

    server = http.server.HTTPServer(("", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()
