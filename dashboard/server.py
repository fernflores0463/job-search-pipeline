#!/usr/bin/env python3
"""
Job Search Dashboard Server
Serves the dashboard and persists state to PostgreSQL (Aurora/RDS).

Usage:
    Local:      DB_HOST=localhost DB_PASSWORD=localdev python3 server.py
    Production: Reads DB credentials from AWS Parameter Store automatically.
    Then open http://localhost:8080 in your browser.
"""

import http.server

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
import urllib.parse

PORT = 8080
DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(DIR)

# Allow importing db module from repo root
sys.path.insert(0, PARENT_DIR)
from db.db import Db, init_pool  # noqa: E402

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

# ─────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────


def _load_auth_password():
    """
    Password resolution order:
    1. DASHBOARD_PASSWORD env var (local dev / docker-compose override)
    2. AWS Parameter Store /job-search/dashboard-password (production)
    3. Empty string → auth bypassed (local dev with no password set)
    """
    if "DASHBOARD_PASSWORD" in os.environ:
        return os.environ["DASHBOARD_PASSWORD"]  # empty string = auth bypassed
    try:
        import boto3
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        resp = ssm.get_parameter(Name="/job-search/dashboard-password", WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception:
        pass
    return ""


_AUTH_PASSWORD = _load_auth_password()
_SESSIONS: set = set()   # In-memory active session tokens; cleared on restart

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Job Dashboard \u2014 Login</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #f1f5f9; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; }}
    .card {{ background: white; border-radius: 12px; padding: 2.5rem;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08); width: 100%; max-width: 380px; }}
    h1 {{ font-size: 1.4rem; font-weight: 700; color: #1e293b; margin-bottom: 0.25rem; }}
    p  {{ font-size: 0.875rem; color: #64748b; margin-bottom: 1.75rem; }}
    label {{ display: block; font-size: 0.8rem; font-weight: 600;
            color: #374151; margin-bottom: 0.4rem; }}
    input[type=password] {{
      width: 100%; padding: 0.65rem 0.875rem; border: 1.5px solid #e2e8f0;
      border-radius: 8px; font-size: 0.9rem; outline: none;
      transition: border-color 0.15s; }}
    input[type=password]:focus {{ border-color: #3b82f6; }}
    button {{ width: 100%; margin-top: 1.25rem; padding: 0.7rem;
             background: #2563eb; color: white; border: none;
             border-radius: 8px; font-size: 0.9rem; font-weight: 600;
             cursor: pointer; transition: background 0.15s; }}
    button:hover {{ background: #1d4ed8; }}
    .error {{ background: #fef2f2; color: #dc2626; border: 1px solid #fecaca;
             border-radius: 8px; padding: 0.65rem 0.875rem;
             font-size: 0.8rem; margin-bottom: 1rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Job Dashboard</h1>
    <p>Enter your password to continue.</p>
    {error_block}
    <form method="POST" action="/api/login">
      <label for="pw">Password</label>
      <input id="pw" type="password" name="password" autofocus autocomplete="current-password">
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>"""


def _get_session_cookie(handler):
    """Extract the 'session' cookie value from the request, or None."""
    for part in handler.headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "session":
            return v.strip()
    return None


def _is_authenticated(handler):
    """Return True if auth is disabled (no password set) or the session cookie is valid."""
    if not _AUTH_PASSWORD:
        return True
    return _get_session_cookie(handler) in _SESSIONS


# Initialize DB connection pool at startup
try:
    init_pool()
    print("Database connection pool initialized.")
except Exception as e:
    print(f"Warning: Could not connect to database: {e}")
    print("Some features may not work. Check DB_HOST and DB_PASSWORD env vars.")

# Background task state (in-memory only)
_update_lock = threading.Lock()
_update_status = {"running": False, "progress": 0, "total": 0, "updated": 0, "failed": 0, "message": ""}

_scrape_lock = threading.Lock()
_scrape_status = {"running": False, "progress": 0, "total": 0, "added": 0, "message": "", "jobs_found": []}

_import_lock = threading.Lock()
_import_status = {"running": False, "progress": 0, "total": 0, "added": 0, "message": "", "jobs_found": []}

_ai_import_lock = threading.Lock()
_ai_import_status = {
    "running": False, "progress": 0, "total": 0, "added": 0,
    "scored_ai": 0, "scored_fallback": 0,
    "estimated_cost": 0.0, "message": "", "jobs_found": [],
    "stopped": False,
    # AI-vs-regex comparison counters (only counted on AI-scored jobs):
    #   regex_agree    = AI tier matches regex tier
    #   ai_promoted    = AI tier > regex tier (AI is more bullish)
    #   ai_demoted     = AI tier < regex tier (AI is less bullish)
    "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
    # Most recent fallback error string (truncated to 240 chars). Used
    # by the UI to surface why jobs are falling back without needing to
    # SSH into the container. Cleared at the start of each new run.
    "last_error": "",
    # Mode and batch-API state. mode is "live" or "batch".
    # batch_id is set only when mode == "batch".
    "mode": "live",
    "batch_id": None,
    "batch_processing_status": None,
    "batch_request_counts": None,
}
_ai_import_stop_event = threading.Event()

_live_check_lock = threading.Lock()
_live_check_status = {"running": False, "progress": 0, "total": 0, "closed": 0, "live": 0, "inconclusive": 0, "auto_updated": 0, "message": ""}

# Company info cache (in-memory, warmed from DB at startup)
_company_cache = {}
_company_cache_lock = threading.Lock()


# ─────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────

def _to_uuid(val):
    """Convert any string to a valid UUID — uses uuid5 for non-UUID strings."""
    try:
        return str(uuid.UUID(str(val)))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(val)))


def _row_to_dict(cursor, row):
    """Convert a DB row to a dict using cursor column names."""
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _serialize(obj):
    """Make a DB row dict JSON-serializable (handles datetime, UUID)."""
    import datetime
    result = {}
    for k, v in obj.items():
        if isinstance(v, datetime.datetime):
            result[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            result[k] = str(v)
        else:
            result[k] = v
    return result


# ── Jobs ──────────────────────────────────────────────────

def load_jobs():
    """Return all jobs from DB as a list of dicts (no description — use load_job for that)."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, company, title, location, work_type, salary,
                       posted_date, import_date, score, tier, job_link, apply_link
                FROM jobs
                ORDER BY import_date DESC, score DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        print(f"Error loading jobs: {e}")
        return []


def load_job(job_id):
    """Return a single job dict including description and resume_text."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, company, title, location, work_type, salary,
                       posted_date, import_date, description, score, tier,
                       job_link, apply_link, resume_text
                FROM jobs WHERE id = %s
            """, (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_dict(cur, row)
    except Exception as e:
        print(f"Error loading job {job_id}: {e}")
        return None


# ── State ─────────────────────────────────────────────────

def load_state():
    """Return all job state from DB as a dict keyed by job_id."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT job_id, status, notes, applicants,
                       live_status, live_status_checked, timestamps,
                       pdf_path
                FROM job_state
            """)
            result = {}
            for row in cur.fetchall():
                job_id, status, notes, applicants, live_status, live_status_checked, timestamps, pdf_path = row
                result[job_id] = {
                    "status": status,
                    "notes": notes,
                    "applicants": applicants,
                    "live_status": live_status,
                    "live_status_checked": live_status_checked.isoformat() if live_status_checked else None,
                    "timestamps": timestamps if isinstance(timestamps, dict) else {},
                    "pdf_path": pdf_path,
                }
            return result
    except Exception as e:
        print(f"Error loading state: {e}")
        return {}


def save_job_state(job_id, state):
    """Upsert a single job's state to DB. Used by background threads and API."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO job_state
                    (job_id, status, notes, applicants, live_status,
                     live_status_checked, timestamps, pdf_path, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (job_id) DO UPDATE SET
                    status               = EXCLUDED.status,
                    notes                = EXCLUDED.notes,
                    applicants           = EXCLUDED.applicants,
                    live_status          = EXCLUDED.live_status,
                    live_status_checked  = EXCLUDED.live_status_checked,
                    timestamps           = EXCLUDED.timestamps,
                    pdf_path             = COALESCE(EXCLUDED.pdf_path,
                                                    job_state.pdf_path),
                    updated_at           = NOW()
            """, (
                job_id,
                state.get("status", "New"),
                state.get("notes"),
                state.get("applicants"),
                state.get("live_status"),
                state.get("live_status_checked"),
                json.dumps(state.get("timestamps", {})),
                state.get("pdf_path"),
            ))
    except Exception as e:
        print(f"Error saving state for {job_id}: {e}")


def save_state(state_dict):
    """Batch upsert a full state dict to DB (used by background threads after each batch)."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            for job_id, state in state_dict.items():
                cur.execute("""
                    INSERT INTO job_state
                        (job_id, status, notes, applicants, live_status, live_status_checked, timestamps, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (job_id) DO UPDATE SET
                        status               = EXCLUDED.status,
                        notes                = EXCLUDED.notes,
                        applicants           = EXCLUDED.applicants,
                        live_status          = EXCLUDED.live_status,
                        live_status_checked  = EXCLUDED.live_status_checked,
                        timestamps           = EXCLUDED.timestamps,
                        updated_at           = NOW()
                """, (
                    job_id,
                    state.get("status", "New"),
                    state.get("notes"),
                    state.get("applicants"),
                    state.get("live_status"),
                    state.get("live_status_checked"),
                    json.dumps(state.get("timestamps", {})),
                ))
    except Exception as e:
        print(f"Error batch saving state: {e}")


# ── Plans ─────────────────────────────────────────────────

def load_plans():
    """Return all application plans from DB."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT p.id, p.title, p.date,
                       pj.job_id, pj.notes AS job_notes,
                       j.company, j.title AS job_title
                FROM application_plans p
                LEFT JOIN application_plan_jobs pj ON p.id = pj.plan_id
                LEFT JOIN jobs j ON pj.job_id = j.id
                ORDER BY p.date DESC, p.created_at DESC
            """)
            plans = {}
            for row in cur.fetchall():
                plan_id, plan_title, plan_date, job_id, job_notes, company, job_title = row
                pid = str(plan_id)
                if pid not in plans:
                    plans[pid] = {"id": pid, "title": plan_title, "date": plan_date, "jobs": []}
                if job_id:
                    plans[pid]["jobs"].append({
                        "id": job_id,
                        "notes": job_notes or "",
                        "company": company or "",
                        "title": job_title or "",
                    })
            return list(plans.values())
    except Exception as e:
        print(f"Error loading plans: {e}")
        return []


def save_plans(plans):
    """Replace all plans in DB with the given list."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM application_plans")
            for plan in plans:
                pid = _to_uuid(plan.get("id", ""))
                cur.execute("""
                    INSERT INTO application_plans (id, title, date)
                    VALUES (%s::uuid, %s, %s)
                """, (pid, plan.get("title"), plan.get("date")))
                for job in plan.get("jobs", []):
                    # Verify the job exists before inserting
                    cur.execute("SELECT 1 FROM jobs WHERE id = %s", (job.get("id"),))
                    if cur.fetchone():
                        cur.execute("""
                            INSERT INTO application_plan_jobs (plan_id, job_id, notes)
                            VALUES (%s::uuid, %s, %s)
                            ON CONFLICT DO NOTHING
                        """, (pid, job.get("id"), job.get("notes", "")))
    except Exception as e:
        print(f"Error saving plans: {e}")


# ── Company Cache ──────────────────────────────────────────

def _load_company_cache_from_db():
    """Warm the in-memory company cache from DB at startup."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT company_name, summary, logo_url, industry FROM company_cache")
            for name, summary, logo_url, industry in cur.fetchall():
                _company_cache[name.lower()] = {
                    "name": name,
                    "summary": summary or "",
                    "logo_url": logo_url or "",
                    "industry": industry or "",
                    "known_for": "",
                    "website": "",
                }
    except Exception as e:
        print(f"Warning: could not load company cache from DB: {e}")


def _save_company_cache_entry(key, info):
    """Upsert a single company cache entry to DB."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO company_cache (company_name, summary, logo_url, industry)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (company_name) DO UPDATE SET
                    summary   = EXCLUDED.summary,
                    logo_url  = EXCLUDED.logo_url,
                    industry  = EXCLUDED.industry,
                    cached_at = NOW()
            """, (key, info.get("summary"), info.get("logo_url"), info.get("industry")))
    except Exception as e:
        print(f"Warning: could not save company cache entry: {e}")


# Warm cache at startup
_load_company_cache_from_db()


# ─────────────────────────────────────────────────────────
# COMPANY INFO (Wikipedia + Clearbit)
# ─────────────────────────────────────────────────────────

def _fetch_company_info(company_name):
    """Fetch company summary from Wikipedia and logo from Clearbit."""
    key = company_name.strip().lower()
    with _company_cache_lock:
        if key in _company_cache:
            return _company_cache[key]

    result = {"name": company_name, "summary": "", "logo_url": "", "known_for": "", "industry": "", "website": ""}

    try:
        search_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "list": "search",
            "srsearch": company_name + " company", "srlimit": 1, "format": "json"
        })
        req = urllib.request.Request(search_url, headers={"User-Agent": "JobDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        results = data.get("query", {}).get("search", [])
        if results:
            title = results[0]["title"]
            summary_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
                "action": "query", "prop": "extracts", "exintro": True,
                "explaintext": True, "exsentences": 4, "titles": title, "format": "json"
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

    if result["summary"]:
        sentences = result["summary"].split(". ")
        if sentences:
            result["known_for"] = sentences[0].rstrip(".") + "."
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

    clean = re.sub(r'[^a-zA-Z0-9]', '', company_name.lower().strip())
    result["website"] = clean + ".com"
    result["logo_url"] = f"https://logo.clearbit.com/{result['website']}"

    with _company_cache_lock:
        _company_cache[key] = result
    _save_company_cache_entry(key, result)
    return result


# ─────────────────────────────────────────────────────────
# LINKEDIN / LIVE CHECK
# ─────────────────────────────────────────────────────────

def _fetch_applicants_curl(linkedin_job_id):
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
    """Background thread: curl each job's LinkedIn page for updated applicant counts."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    jobs = load_jobs()
    state = load_state()

    def _parse_count(s):
        if not s:
            return 0
        m = re.search(r'([\d,]+)', str(s))
        return int(m.group(1).replace(',', '')) if m else 0

    pairs = []
    skipped = 0
    for j in jobs:
        m = re.search(r'/view/(\d+)', j.get('job_link', ''))
        if not m or not j.get('id'):
            continue
        job_id = j['id']
        current = state.get(job_id, {}).get('applicants') or j.get('applicants', '')
        if _parse_count(current) >= 500:
            skipped += 1
            continue
        pairs.append((job_id, m.group(1)))

    with _update_lock:
        _update_status.update({"running": True, "progress": 0, "total": len(pairs),
                               "updated": 0, "failed": 0,
                               "message": f"Running... ({skipped} skipped with 500+ applicants)"})

    for i in range(0, len(pairs), 15):
        if not _update_status["running"]:
            break
        batch = pairs[i:i + 15]

        def fetch_one(pair):
            job_id, linkedin_id = pair
            return job_id, _fetch_applicants_curl(linkedin_id)

        with ThreadPoolExecutor(max_workers=15) as executor:
            results = list(executor.map(fetch_one, batch))

        for job_id, applicants in results:
            with _update_lock:
                _update_status["progress"] += 1
            if applicants:
                if job_id not in state:
                    state[job_id] = {}
                state[job_id]["applicants"] = applicants
                save_job_state(job_id, state[job_id])
                with _update_lock:
                    _update_status["updated"] += 1
            else:
                with _update_lock:
                    _update_status["failed"] += 1

        if i + 15 < len(pairs):
            time.sleep(0.5)

    with _update_lock:
        _update_status["running"] = False
        _update_status["message"] = (
            f"Done! {_update_status['updated']} updated, {_update_status['failed']} failed."
        )


def _check_job_live_linkedin(job_link):
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
        if not body or len(body) < 10000:
            return "inconclusive"
        body_lower = body.lower()
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
    if not apply_link:
        return "inconclusive"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", apply_link],
            capture_output=True, text=True, timeout=15
        )
        code = result.stdout.strip()
        if code in ("404", "410"):
            return "closed"
        if code == "200":
            return "live"
        return "inconclusive"
    except Exception:
        return "inconclusive"


INTERVIEW_STAGES = {
    "Online Assessment", "Recruiter Screen",
    "1st Interview", "2nd Interview", "Onsite", "Manager Screen",
    "Verbal Offer", "Offer", "Accepted",
}


def _run_live_check(batch_filter=None):
    """Background thread: check if jobs are still live on LinkedIn."""
    import time
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor

    jobs = load_jobs()
    state = load_state()

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
        js = state.get(job_id, {})
        pairs.append((job_id, job_link, apply_link, js.get("status"), js.get("timestamps", {})))

    with _live_check_lock:
        _live_check_status.update({
            "running": True, "progress": 0, "total": len(pairs),
            "closed": 0, "live": 0, "inconclusive": 0, "auto_updated": 0,
            "message": f"Checking {len(pairs)} jobs..."
        })

    now_iso = datetime.now(timezone.utc).isoformat()

    for i in range(0, len(pairs), 10):
        if not _live_check_status["running"]:
            break
        batch = pairs[i:i + 10]

        def check_one(pair):
            job_id, job_link, apply_link, current_status, timestamps = pair
            is_linkedin = "/linkedin.com/" in job_link
            result = _check_job_live_linkedin(job_link) if is_linkedin else _check_apply_link(job_link)
            if result == "inconclusive" and apply_link and apply_link != job_link:
                r2 = _check_apply_link(apply_link)
                if r2 in ("closed", "live"):
                    result = r2
            return job_id, result, current_status, timestamps

        with ThreadPoolExecutor(max_workers=10) as executor:
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

            save_job_state(job_id, state[job_id])

            with _live_check_lock:
                _live_check_status["progress"] += 1
                _live_check_status[result if result in ("closed", "live") else "inconclusive"] += 1
                if auto_updated:
                    _live_check_status["auto_updated"] += 1

        if i + 10 < len(pairs):
            time.sleep(1.5)

    with _live_check_lock:
        _live_check_status["running"] = False
        s = _live_check_status
        _live_check_status["message"] = (
            f"Done! {s['closed']} closed, {s['live']} live, {s['inconclusive']} inconclusive. "
            f"{s['auto_updated']} auto-updated."
        )


# ─────────────────────────────────────────────────────────
# AI ASSISTANT
# ─────────────────────────────────────────────────────────

_BULLET_POOL = {
    entry.get("display_name", key): entry["bullets"]
    for key, entry in _config["experience"].items()
}
_CANDIDATE_SKILLS = _config["candidate"]["skills"]


def _build_job_context(job_id):
    """Build AI context string from DB for a given job."""
    job = load_job(job_id)
    if not job:
        return None, None

    parts = [
        f"Company: {job.get('company', 'Unknown')}",
        f"Title: {job.get('title', 'Unknown')}",
        f"Location: {job.get('location', 'Unknown')}",
        f"Work Type: {job.get('work_type', 'Unknown')}",
        f"Salary: {job.get('salary', 'N/A')}",
        f"Score: {job.get('score', 0)} | Tier: {job.get('tier', 'Unknown')}",
    ]
    if job.get("description"):
        parts.append(f"\nJOB DESCRIPTION:\n{job['description']}")

    return "\n".join(parts), job.get("resume_text")


def _format_bullet_pool():
    lines = []
    for company, bullets in _BULLET_POOL.items():
        lines.append(f"\n{company}:")
        for i, b in enumerate(bullets, 1):
            lines.append(f"  {i}. {b}")
    return "\n".join(lines)


def _call_ollama(messages, model="qwen2.5:14b"):
    payload = json.dumps({"model": model, "messages": messages, "stream": False}).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result.get("message", {}).get("content", "")


def _load_anthropic_key():
    """Load Anthropic API key from env var or AWS Parameter Store."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    try:
        import boto3
        ssm = boto3.client(
            "ssm",
            region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        resp = ssm.get_parameter(
            Name="/job-search/anthropic-api-key",
            WithDecryption=True
        )
        return resp["Parameter"]["Value"]
    except Exception:
        return ""


_ANTHROPIC_KEY = _load_anthropic_key()

# Reuse a single client instance for connection pooling
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None and _ANTHROPIC_KEY:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
    return _anthropic_client


def _call_claude(system_prompt, messages, model="claude-sonnet-4-20250514"):
    """Call the Anthropic Claude API with prompt caching.

    The system prompt is marked with cache_control so Anthropic
    caches it across calls. Subsequent requests within the 5-minute
    TTL pay only 10% of the input token cost for the cached portion.
    """
    client = _get_anthropic_client()
    if not client:
        raise RuntimeError("Anthropic client not initialized")

    # Structure system prompt as a cacheable content block
    system_blocks = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_blocks,
        messages=messages,
    )
    return response.content[0].text


def _call_claude_with_retry(
    system_prompt, messages, model="claude-sonnet-4-20250514",
    max_retries=3, initial_backoff=5,
):
    """Call _call_claude with exponential backoff on rate limits.

    Retries on 429 (rate_limit_error) and 529 (overloaded_error) only.
    Other exceptions propagate immediately. Backoff: initial_backoff,
    initial_backoff*2, initial_backoff*4 (default 5s, 10s, 20s).

    Used by AI scoring to recover from Anthropic Tier 1 ITPM (50K/min)
    rate limits instead of falling back to regex on every spike.
    """
    backoff = initial_backoff
    last_error = None
    for attempt in range(max_retries):
        try:
            return _call_claude(system_prompt, messages, model=model)
        except Exception as e:
            last_error = e
            msg = str(e)
            is_rate_limit = (
                "429" in msg or "rate_limit" in msg.lower()
            )
            is_overloaded = (
                "529" in msg or "overloaded" in msg.lower()
            )
            if (is_rate_limit or is_overloaded) and attempt < max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
    # Defensive: should be unreachable, but if the loop exits
    # without returning or raising, surface the last error.
    raise last_error if last_error else RuntimeError("retry loop exited without result")


def _extract_pdf_text(pdf_path):
    """Extract text from a PDF file. Returns None on failure."""
    try:
        from PyPDF2 import PdfReader
        full_path = os.path.join(PARENT_DIR, pdf_path)
        if not os.path.exists(full_path):
            return None
        reader = PdfReader(full_path)
        text_parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
        return "\n".join(text_parts) if text_parts else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# AI JOB SCORING (Claude Haiku)
# ─────────────────────────────────────────────────────────

AI_SCORING_MODEL = "claude-haiku-4-5-20251001"
# Haiku 4.5: $1/MTok input, $5/MTok output. With prompt caching on
# the system prompt and trimmed descriptions (~2500 chars), avg call:
# ~700 input tokens + ~150 output tokens = ~$0.0017/job amortized.
AI_SCORING_COST_PER_JOB = 0.0017


def _ai_score_to_legacy_scale(ai_score):
    """Map Claude's 1-10 fit_score to the legacy 0-24 scale.

    Preserves tier boundaries from config.json (strong>=13, match>=7):
      AI 8-10 (Strong) -> legacy 13-24
      AI 5-7  (Match)  -> legacy 7-12
      AI 1-4  (Weak)   -> legacy 0-6
    """
    if ai_score >= 8:
        # 8->13, 9->18 or 19, 10->24
        return 13 + round((ai_score - 8) * 5.5)
    if ai_score >= 5:
        # 5->7, 6->9 or 10, 7->12
        return 7 + round((ai_score - 5) * 2.5)
    # 1->0, 2->2, 3->4, 4->6
    return max(0, (ai_score - 1) * 2)


def _build_scoring_system_prompt():
    """Build the system prompt for AI job scoring.

    Uses the current in-memory _config so Profile page edits take
    effect without a restart. Returns a string ~1,500 tokens.
    """
    name = _config["candidate"].get("name", "the candidate")
    return "\n".join([
        f"You are a job-fit scoring assistant evaluating how well {name} "
        "(a senior software engineer with 10+ years of professional "
        "experience) matches each job posting.",
        "",
        f"CANDIDATE SKILLS: {_CANDIDATE_SKILLS}",
        "",
        "EXPERIENCE BULLETS (organized by company):",
        _format_bullet_pool(),
        "",
        "SCORING CRITERIA (1-10 integer scale):",
        "  9-10: Near-perfect stack overlap + relevant domain experience",
        "        AND seniority level matches (Senior, Staff, Principal,",
        "        Lead, or unleveled IC roles at 5+ YoE)",
        "  7-8:  Strong overlap, minor gaps, seniority is appropriate",
        "  5-6:  Partial overlap, would need to learn some technologies",
        "  3-4:  Weak overlap, but transferable skills exist",
        "  1-2:  Wrong domain or requires expertise candidate lacks",
        "",
        "NEGATIVE SIGNALS (reduce score):",
        "  - Entry-level, new-grad, intern, SE I, SE II, Junior, or",
        "    Associate roles \u2014 candidate is overqualified and likely",
        "    would not be seriously considered. Cap these at score 5",
        "    (still surfaced as Match for manual review, never Strong).",
        "  - Requires security clearance candidate doesn't have",
        "  - Requires 8+ years in a technology candidate has <2 years in",
        "  - Primary language is one candidate doesn't know",
        "    (e.g. Rust, Scala, .NET as primary)",
        "  - Heavy domain expertise (healthcare EMR, financial regs)",
        "",
        "Return ONLY a JSON object with this exact shape (no markdown, "
        "no explanation outside the JSON):",
        '{"fit_score": <1-10 integer>,',
        ' "reasoning": "<2 sentences max. Describe the match '
        'qualitatively, referencing specific technologies or experience. '
        'Do NOT mention any score numbers in the reasoning text.>"}',
    ])


def _score_job_with_haiku(job, system_prompt):
    """Score a single job using Claude Haiku. Returns dict.

    On success: {legacy_score, ai_fit_score, reasoning, used_ai: True}
      legacy_score   - integer 0-24, on the same scale as the regex
                       scoring pipeline (caller derives tier via
                       assign_tier)
      ai_fit_score   - original 1-10 score from Claude (kept for
                       debugging / analysis)
    On failure: {used_ai: False, error: str}
    """
    try:
        # Strip boilerplate (About Us, Benefits, EEO, etc.) and cap
        # at 2500 chars. Cuts ~60% of input tokens vs full description.
        relevant_desc = _extract_relevant_description(
            job.get('description', ''), max_chars=2500
        )
        user_message = (
            f"Score this job posting.\n\n"
            f"Company: {job.get('company', '')}\n"
            f"Title: {job.get('title', '')}\n\n"
            f"DESCRIPTION (relevant excerpt):\n{relevant_desc}"
        )
        raw = _call_claude_with_retry(
            system_prompt,
            [{"role": "user", "content": user_message}],
            model=AI_SCORING_MODEL,
        )

        # Extract the JSON block from the response
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return {"used_ai": False, "error": "No JSON in response"}

        data = json.loads(match.group(0))
        ai_fit_score = int(data.get("fit_score", 5))
        ai_fit_score = max(1, min(10, ai_fit_score))  # Clamp 1-10
        legacy_score = _ai_score_to_legacy_scale(ai_fit_score)
        reasoning = str(data.get("reasoning", "")).strip()

        return {
            "legacy_score": legacy_score,
            "ai_fit_score": ai_fit_score,
            "reasoning": reasoning,
            "used_ai": True,
        }
    except Exception as e:
        return {"used_ai": False, "error": str(e)}


# ─────────────────────────────────────────────────────────
# ANTHROPIC BATCH API SUPPORT (50% discount, async)
# ─────────────────────────────────────────────────────────
#
# Bypasses ITPM rate limits entirely by submitting all requests at
# once and letting Anthropic process them at its own pace. Trades the
# live progress UX for ~50% lower cost. Typical turnaround: minutes to
# hours, max 24h. Used by /api/import-csv-ai?mode=batch.

def _build_batch_request(job_id, system_prompt, job):
    """Build one entry of an Anthropic Messages Batches request.

    Each entry has a custom_id that we use to match results back to
    the originating job. The params dict mirrors messages.create()
    arguments exactly.
    """
    relevant_desc = _extract_relevant_description(
        job.get('description', ''), max_chars=2500
    )
    user_message = (
        f"Score this job posting.\n\n"
        f"Company: {job.get('company', '')}\n"
        f"Title: {job.get('title', '')}\n\n"
        f"DESCRIPTION (relevant excerpt):\n{relevant_desc}"
    )
    return {
        "custom_id": job_id,
        "params": {
            "model": AI_SCORING_MODEL,
            "max_tokens": 512,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user_message}],
        },
    }


def _create_anthropic_batch(requests):
    """Submit a batch of message requests to Anthropic. Returns the
    batch object (with .id and .processing_status).
    """
    client = _get_anthropic_client()
    if not client:
        raise RuntimeError("Anthropic client not initialized")
    return client.messages.batches.create(requests=requests)


def _get_anthropic_batch(batch_id):
    """Fetch the current status of a batch."""
    client = _get_anthropic_client()
    return client.messages.batches.retrieve(batch_id)


def _retrieve_anthropic_batch_results(batch_id):
    """Stream and parse batch results.

    Yields (custom_id, parsed_result_dict) tuples. parsed_result_dict
    has the same shape as _score_job_with_haiku's success / failure
    return value, so the caller can reuse the same downstream logic.
    """
    client = _get_anthropic_client()
    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        # result.result is a union type: succeeded | errored | expired | canceled
        rtype = result.result.type
        if rtype == "succeeded":
            try:
                msg = result.result.message
                raw = msg.content[0].text
                m = re.search(r'\{[\s\S]*\}', raw)
                if not m:
                    yield custom_id, {
                        "used_ai": False,
                        "error": "No JSON in batch response"
                    }
                    continue
                data = json.loads(m.group(0))
                ai_fit_score = max(1, min(10, int(data.get("fit_score", 5))))
                yield custom_id, {
                    "used_ai": True,
                    "ai_fit_score": ai_fit_score,
                    "legacy_score": _ai_score_to_legacy_scale(ai_fit_score),
                    "reasoning": str(data.get("reasoning", "")).strip(),
                }
            except Exception as e:
                yield custom_id, {
                    "used_ai": False,
                    "error": f"parse error: {e}",
                }
        elif rtype == "errored":
            err = getattr(result.result, "error", None)
            err_msg = str(err) if err else "unknown batch error"
            yield custom_id, {"used_ai": False, "error": err_msg[:240]}
        elif rtype == "expired":
            yield custom_id, {"used_ai": False, "error": "batch request expired"}
        elif rtype == "canceled":
            yield custom_id, {"used_ai": False, "error": "batch request canceled"}
        else:
            yield custom_id, {"used_ai": False, "error": f"unknown result type: {rtype}"}


# ─────────────────────────────────────────────────────────
# CAREERS PAGE SCRAPER
# ─────────────────────────────────────────────────────────

def _run_scrape_careers(url, company_override=None):
    """Background thread: scrape a careers page and import new jobs."""
    import time as _time
    sys.path.insert(0, PARENT_DIR)

    try:
        from process_new_postings import (
            is_excluded_company, is_excluded_description,
            is_excluded_title, is_excluded_role, is_swe_role,
            calc_tech_score, calc_level_bonus, calc_company_bonus, assign_tier,
            make_job_id, generate_resume_files,
        )
        from scrape_careers_page import (
            fetch_page, detect_platform, extract_greenhouse, extract_lever,
            extract_ashby, extract_generic, fetch_description,
        )
        from datetime import date as _date
        from urllib.parse import urlparse as _urlparse

        with _scrape_lock:
            _scrape_status.update({"running": True, "progress": 0, "total": 0,
                                   "added": 0, "message": "Fetching careers page...", "jobs_found": []})

        soup, final_url = fetch_page(url)
        platform = detect_platform(final_url, soup)

        company = company_override
        if not company:
            og_site = soup.find('meta', property='og:site_name')
            title_tag = soup.find('title')
            if og_site and og_site.get('content'):
                company = og_site['content'].strip()
            elif title_tag:
                t = title_tag.get_text(strip=True)
                for pat in [r'(?:jobs?\s+at|careers?\s+at)\s+(.+?)(?:\s*[-|]|$)',
                            r'^(.+?)\s+(?:careers?|jobs?|openings?)', r'^(.+?)\s*[-|]']:
                    m = re.search(pat, t, re.I)
                    if m:
                        company = m.group(1).strip()
                        break
            if not company:
                company = _urlparse(final_url).hostname.split('.')[0].title()

        if is_excluded_company(company):
            with _scrape_lock:
                _scrape_status.update({
                    "running": False,
                    "message": f"Skipped: '{company}' matches a staffing firm exclusion pattern."
                })
            return

        with _scrape_lock:
            _scrape_status["message"] = f"Detected {platform} — extracting jobs from {company}..."

        extractor = {"greenhouse": extract_greenhouse, "lever": extract_lever,
                     "ashby": extract_ashby}.get(platform, extract_generic)
        raw_jobs = extractor(soup, final_url)

        # Deduplicate
        seen, unique = set(), []
        for j in raw_jobs:
            if j['url'] not in seen:
                seen.add(j['url'])
                unique.append(j)
        raw_jobs = unique

        swe_jobs = [j for j in raw_jobs
                    if is_swe_role(j['title'].lower())
                    and not is_excluded_title(j['title'].lower())
                    and not is_excluded_role(j['title'].lower())]
        if not swe_jobs:
            swe_jobs = raw_jobs

        with _scrape_lock:
            _scrape_status["total"] = len(swe_jobs)
            _scrape_status["message"] = f"Found {len(swe_jobs)} positions at {company}. Fetching descriptions..."

        if not swe_jobs:
            with _scrape_lock:
                _scrape_status.update({"running": False, "message": "No jobs found."})
            return

        # Load existing job links from DB to deduplicate
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT job_link FROM jobs")
            existing_links = {row[0] for row in cur.fetchall()}

        processed_jobs = []
        for i, j in enumerate(swe_jobs):
            desc = fetch_description(j['url'])
            dl, tl = desc.lower(), j['title'].lower()
            if is_excluded_description(dl):
                with _scrape_lock:
                    _scrape_status["progress"] = i + 1
                continue
            total = calc_tech_score(dl) + calc_level_bonus(tl) + calc_company_bonus(company)
            tier = assign_tier(total)
            processed_jobs.append({
                'id': make_job_id(company, j['title'], j['url']),
                'company': company, 'title': j['title'], 'salary': 'N/A',
                'description': desc, 'apply_link': j['url'], 'job_link': j['url'],
                'posted_date': '', 'location': j.get('location', ''), 'work_type': '',
                'applicants': '', 'score': total, 'tier': tier,
                'import_date': _date.today().isoformat(),
            })
            with _scrape_lock:
                _scrape_status["progress"] = i + 1
                _scrape_status["message"] = f"Scoring [{i + 1}/{len(swe_jobs)}]: {j['title'][:50]}"
            if i < len(swe_jobs) - 1:
                _time.sleep(0.3)

        # Insert new jobs into DB
        added = [j for j in processed_jobs if j.get('job_link') not in existing_links]
        if added:
            with Db() as conn:
                cur = conn.cursor()
                for j in added:
                    cur.execute("""
                        INSERT INTO jobs (id, company, title, location, work_type, salary,
                                         posted_date, import_date, description, score, tier,
                                         job_link, apply_link)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO NOTHING
                    """, (j['id'], j['company'], j['title'], j['location'], j['work_type'],
                          j['salary'], j['posted_date'], j['import_date'], j['description'],
                          j['score'], j['tier'], j['job_link'], j['apply_link']))
                    cur.execute("""
                        INSERT INTO job_state (job_id) VALUES (%s)
                        ON CONFLICT DO NOTHING
                    """, (j['id'],))
            generate_resume_files(added)

        tiers = {}
        for j in processed_jobs:
            tiers.setdefault(j['tier'], []).append(j['title'])
        tier_summary = ", ".join(f"{t}: {len(v)}" for t, v in sorted(tiers.items()))

        with _scrape_lock:
            _scrape_status.update({
                "running": False, "added": len(added),
                "jobs_found": [
                    {"title": j["title"], "tier": j["tier"], "score": j["score"],
                     "location": j.get("location", "")}
                    for j in sorted(processed_jobs, key=lambda x: -x["score"])[:20]
                ],
                "message": f"Done! {len(processed_jobs)} found, {len(added)} new added. {tier_summary}.",
            })

    except Exception as e:
        with _scrape_lock:
            _scrape_status.update({"running": False, "message": f"Error: {str(e)}"})


def _run_import_csv(csv_bytes, location):
    """Background thread: parse a LinkedIn CSV export and import new jobs into PostgreSQL."""
    try:
        import csv as _csv
        import io
        from datetime import date as _date
        from process_new_postings import (
            is_excluded_company, is_excluded_description,
            is_excluded_title, is_excluded_role, is_swe_role,
            calc_tech_score, calc_level_bonus, calc_company_bonus, assign_tier,
            make_job_id, pick_bullets, customize_skills, generate_resume_txt,
        )

        with _import_lock:
            _import_status.update({"running": True, "progress": 0, "total": 0,
                                   "added": 0, "message": "Parsing CSV…", "jobs_found": []})

        # utf-8-sig strips BOM that LinkedIn sometimes includes
        text = csv_bytes.decode("utf-8-sig")
        rows = list(_csv.DictReader(io.StringIO(text)))

        # Location priority: explicit arg → CSV's Search Location column → omit
        if not location and rows:
            location = (rows[0].get("Search Location") or "").strip() or None

        # Batch label — matches format used by process_new_postings.py:
        # "YYYY-MM-DD" or "YYYY-MM-DD — Location"
        batch_label = _date.today().isoformat()
        if location:
            batch_label += f" \u2014 {location}"

        # Filter & score — same logic as process_new_postings.py
        seen, processed = set(), []
        for r in rows:
            company = (r.get("Company") or "").strip()
            title = (r.get("Job Title") or "").strip()
            link = (r.get("Job Link") or "").strip()
            key = link if link else (company, title)
            if key in seen:
                continue
            seen.add(key)
            if is_excluded_company(company):
                continue
            tl = title.lower()
            desc = r.get("Description") or ""
            dl = desc.lower()
            if is_excluded_title(tl) or is_excluded_role(tl) or not is_swe_role(tl):
                continue
            if is_excluded_description(dl):
                continue
            score = calc_tech_score(dl) + calc_level_bonus(tl) + calc_company_bonus(company)
            processed.append({
                "id": make_job_id(company, title, link),
                "company": company,
                "title": title,
                "location": (r.get("Location") or "").strip(),
                "work_type": (r.get("Work Type") or "").strip(),
                "salary": (r.get("Salary") or "N/A").strip(),
                "posted_date": (r.get("Posted Date") or "").strip(),
                "import_date": batch_label,
                "description": desc,
                "score": score,
                "tier": assign_tier(score),
                "job_link": link,
                "apply_link": (r.get("Application Link") or "").strip(),
            })

        with _import_lock:
            _import_status["total"] = len(processed)
            _import_status["message"] = f"Found {len(processed)} qualifying jobs. Checking DB…"

        if not processed:
            with _import_lock:
                _import_status.update({"running": False,
                                       "message": "No qualifying SWE jobs found in CSV."})
            return

        # Dedup against existing DB rows by job_link
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT job_link FROM jobs WHERE job_link IS NOT NULL")
            existing = {row[0] for row in cur.fetchall()}

        new_jobs = [j for j in processed if j["job_link"] not in existing]

        # Insert new jobs + initial job_state rows
        with Db() as conn:
            cur = conn.cursor()
            for i, j in enumerate(new_jobs):
                bullets = pick_bullets(j["description"], j["title"])
                langs, frameworks, misc = customize_skills(j["description"])
                resume_text = generate_resume_txt(j, bullets, langs, frameworks, misc)
                cur.execute("""
                    INSERT INTO jobs (id, company, title, location, work_type, salary,
                                     posted_date, import_date, description, score, tier,
                                     job_link, apply_link, resume_text)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (j["id"], j["company"], j["title"], j["location"], j["work_type"],
                      j["salary"], j["posted_date"], j["import_date"], j["description"],
                      j["score"], j["tier"], j["job_link"], j["apply_link"], resume_text))
                cur.execute(
                    "INSERT INTO job_state (job_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (j["id"],))
                with _import_lock:
                    _import_status["progress"] = i + 1
                    _import_status["message"] = f"Inserting {i + 1}/{len(new_jobs)}: {j['title'][:50]}"

        tiers = {}
        for j in processed:
            tiers.setdefault(j["tier"], []).append(j["title"])
        summary = ", ".join(f"{t}: {len(v)}" for t, v in sorted(tiers.items()))
        with _import_lock:
            _import_status.update({
                "running": False,
                "added": len(new_jobs),
                "message": f"Done! {len(processed)} found, {len(new_jobs)} new. {summary}.",
                "jobs_found": [
                    {"title": j["title"], "tier": j["tier"],
                     "score": j["score"], "location": j["location"]}
                    for j in sorted(processed, key=lambda x: -x["score"])[:20]
                ],
            })

    except Exception as e:
        with _import_lock:
            _import_status.update({"running": False, "message": f"Error: {str(e)}"})


def _run_import_csv_ai(csv_bytes, location):
    """Background thread: parse CSV and import new jobs using
    Claude Haiku for scoring. Parallel to _run_import_csv() \u2014 does
    not modify the existing regex import path."""
    try:
        import csv as _csv
        import io
        from concurrent.futures import ThreadPoolExecutor
        from datetime import date as _date
        from process_new_postings import (
            is_excluded_company, is_excluded_description,
            is_excluded_title, is_excluded_role, is_swe_role,
            calc_tech_score, calc_level_bonus, calc_company_bonus,
            assign_tier, make_job_id, pick_bullets,
            customize_skills, generate_resume_txt,
        )

        # Reset stop event at the start of each run
        _ai_import_stop_event.clear()

        with _ai_import_lock:
            _ai_import_status.update({
                "running": True, "progress": 0, "total": 0,
                "added": 0, "scored_ai": 0, "scored_fallback": 0,
                "estimated_cost": 0.0, "stopped": False,
                "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
                "last_error": "", "mode": "live",
                "batch_id": None, "batch_processing_status": None,
                "batch_request_counts": None,
                "message": "Parsing CSV\u2026", "jobs_found": [],
            })

        text = csv_bytes.decode("utf-8-sig")
        rows = list(_csv.DictReader(io.StringIO(text)))

        if not location and rows:
            location = (rows[0].get("Search Location") or "").strip() or None

        # Distinguishable batch label so AI imports show separately
        batch_label = _date.today().isoformat()
        if location:
            batch_label += f" \u2014 {location} (AI)"
        else:
            batch_label += " \u2014 AI"

        # Filter (same logic as existing import)
        seen, filtered = set(), []
        for r in rows:
            company = (r.get("Company") or "").strip()
            title = (r.get("Job Title") or "").strip()
            link = (r.get("Job Link") or "").strip()
            key = link if link else (company, title)
            if key in seen:
                continue
            seen.add(key)
            if is_excluded_company(company):
                continue
            tl = title.lower()
            desc = r.get("Description") or ""
            dl = desc.lower()
            if is_excluded_title(tl) or is_excluded_role(tl) \
                    or not is_swe_role(tl):
                continue
            if is_excluded_description(dl):
                continue
            filtered.append({
                "id": make_job_id(company, title, link),
                "company": company,
                "title": title,
                "location": (r.get("Location") or "").strip(),
                "work_type": (r.get("Work Type") or "").strip(),
                "salary": (r.get("Salary") or "N/A").strip(),
                "posted_date": (r.get("Posted Date") or "").strip(),
                "import_date": batch_label,
                "description": desc,
                "job_link": link,
                "apply_link": (r.get("Application Link") or "").strip(),
            })

        with _ai_import_lock:
            _ai_import_status["total"] = len(filtered)
            _ai_import_status["message"] = (
                f"Found {len(filtered)} qualifying jobs. Checking DB\u2026"
            )

        if not filtered:
            with _ai_import_lock:
                _ai_import_status.update({
                    "running": False,
                    "message": "No qualifying SWE jobs found in CSV.",
                })
            return

        # Dedup against existing DB
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT job_link FROM jobs WHERE job_link IS NOT NULL"
            )
            existing = {row[0] for row in cur.fetchall()}

        new_jobs = [j for j in filtered if j["job_link"] not in existing]

        with _ai_import_lock:
            _ai_import_status["total"] = len(new_jobs)
            _ai_import_status["message"] = (
                f"Scoring {len(new_jobs)} new jobs with Claude Haiku\u2026"
            )

        if not new_jobs:
            with _ai_import_lock:
                _ai_import_status.update({
                    "running": False,
                    "message": "No new jobs to import "
                    "(all dedupped against existing).",
                })
            return

        # Build system prompt once — reused across all calls, cached
        system_prompt = _build_scoring_system_prompt()

        # Helper: compute regex score for a job (free, local, fast).
        # Used for both fallback scoring AND for the AI-vs-regex
        # comparison view shown in the dashboard.
        def _compute_regex_score(job):
            dl = job["description"].lower()
            tl = job["title"].lower()
            return (
                calc_tech_score(dl)
                + calc_level_bonus(tl)
                + calc_company_bonus(job["company"])
            )

        # Tier rank for AI-vs-regex agreement comparison
        _TIER_RANK = {"Weak Match": 0, "Match": 1, "Strong Match": 2}

        # Helper: regex-fallback scoring shared by stop and error paths
        def _regex_fallback(job):
            regex_score = _compute_regex_score(job)
            job["score"] = regex_score
            job["regex_score"] = regex_score
            job["tier"] = assign_tier(regex_score)
            job["ai_reasoning"] = None

        # Score in parallel (5 workers — conservative for rate limits)
        def score_one(job):
            # If stop was requested, skip the API call entirely and
            # use regex fallback for fast finish
            if _ai_import_stop_event.is_set():
                _regex_fallback(job)
                with _ai_import_lock:
                    _ai_import_status["scored_fallback"] += 1
                    _ai_import_status["progress"] += 1
                return job

            result = _score_job_with_haiku(job, system_prompt)
            if result.get("used_ai"):
                # Use the legacy 0-24 scale so AI and regex jobs are
                # comparable. Tier derived from the same scale via
                # assign_tier (uses config.json thresholds).
                job["score"] = result["legacy_score"]
                job["tier"] = assign_tier(result["legacy_score"])
                job["ai_reasoning"] = result["reasoning"]
                # Compute the regex score in parallel for comparison.
                # Free + local — no extra API cost.
                job["regex_score"] = _compute_regex_score(job)
                regex_tier = assign_tier(job["regex_score"])
                ai_rank = _TIER_RANK.get(job["tier"], 1)
                rx_rank = _TIER_RANK.get(regex_tier, 1)
                with _ai_import_lock:
                    _ai_import_status["scored_ai"] += 1
                    _ai_import_status["estimated_cost"] += (
                        AI_SCORING_COST_PER_JOB
                    )
                    if ai_rank == rx_rank:
                        _ai_import_status["regex_agree"] += 1
                    elif ai_rank > rx_rank:
                        _ai_import_status["ai_promoted"] += 1
                    else:
                        _ai_import_status["ai_demoted"] += 1
            else:
                _regex_fallback(job)
                err = (result.get("error") or "")[:240]
                with _ai_import_lock:
                    _ai_import_status["scored_fallback"] += 1
                    if err:
                        _ai_import_status["last_error"] = err
            with _ai_import_lock:
                _ai_import_status["progress"] += 1
                s = _ai_import_status
                _ai_import_status["message"] = (
                    f"AI scoring: {s['progress']}/{s['total']} "
                    f"({s['scored_ai']} AI, "
                    f"{s['scored_fallback']} fallback, "
                    f"~${s['estimated_cost']:.3f})"
                )
            return job

        # 2 workers keeps us under Anthropic Tier 1 ITPM (50K/min)
        # with ~2,200 input tokens/call. Was 5 — caused 27-50% 429
        # fallbacks. Bump back to 5 once on Tier 2.
        with ThreadPoolExecutor(max_workers=2) as executor:
            scored_jobs = list(executor.map(score_one, new_jobs))

        with _ai_import_lock:
            _ai_import_status["message"] = (
                f"Inserting {len(scored_jobs)} jobs into DB\u2026"
            )

        # Insert — same pattern as _run_import_csv but with ai_reasoning
        with Db() as conn:
            cur = conn.cursor()
            for i, j in enumerate(scored_jobs):
                bullets = pick_bullets(j["description"], j["title"])
                langs, frameworks, misc = customize_skills(j["description"])
                resume_text = generate_resume_txt(
                    j, bullets, langs, frameworks, misc
                )
                cur.execute("""
                    INSERT INTO jobs
                        (id, company, title, location, work_type, salary,
                         posted_date, import_date, description, score,
                         tier, job_link, apply_link, resume_text,
                         ai_reasoning, regex_score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    j["id"], j["company"], j["title"], j["location"],
                    j["work_type"], j["salary"], j["posted_date"],
                    j["import_date"], j["description"], j["score"],
                    j["tier"], j["job_link"], j["apply_link"],
                    resume_text, j.get("ai_reasoning"), j.get("regex_score"),
                ))
                cur.execute(
                    "INSERT INTO job_state (job_id) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (j["id"],)
                )

        tiers = {}
        for j in scored_jobs:
            tiers.setdefault(j["tier"], []).append(j["title"])
        summary = ", ".join(
            f"{t}: {len(v)}" for t, v in sorted(tiers.items())
        )
        was_stopped = _ai_import_stop_event.is_set()
        with _ai_import_lock:
            _ai_import_status.update({
                "running": False,
                "added": len(scored_jobs),
                "stopped": was_stopped,
                "message": (
                    ("Stopped early. " if was_stopped else "Done! ")
                    + f"{len(scored_jobs)} inserted "
                    f"({_ai_import_status['scored_ai']} AI, "
                    f"{_ai_import_status['scored_fallback']} fallback), "
                    f"cost ~${_ai_import_status['estimated_cost']:.3f}. "
                    f"{summary}."
                ),
                "jobs_found": [
                    {"title": j["title"], "tier": j["tier"],
                     "score": j["score"], "location": j["location"]}
                    for j in sorted(
                        scored_jobs, key=lambda x: -x["score"]
                    )[:20]
                ],
            })

    except Exception as e:
        with _ai_import_lock:
            _ai_import_status.update(
                {"running": False, "message": f"Error: {str(e)}"}
            )


# ─────────────────────────────────────────────────────────
# BATCH MODE — same import flow but uses Anthropic Batches
# ─────────────────────────────────────────────────────────

def _run_import_csv_ai_batch(csv_bytes, location):
    """Background thread: parse CSV, submit a single Batch API job to
    Anthropic, poll until complete, retrieve results, then insert.

    Trades ~50% lower cost for async processing (minutes to hours).
    Reuses _ai_import_status (with mode="batch") so the existing UI
    polling continues to work.
    """
    try:
        import csv as _csv
        import io
        from datetime import date as _date
        from process_new_postings import (
            is_excluded_company, is_excluded_description,
            is_excluded_title, is_excluded_role, is_swe_role,
            calc_tech_score, calc_level_bonus, calc_company_bonus,
            assign_tier, make_job_id, pick_bullets,
            customize_skills, generate_resume_txt,
        )

        _ai_import_stop_event.clear()

        with _ai_import_lock:
            _ai_import_status.update({
                "running": True, "progress": 0, "total": 0,
                "added": 0, "scored_ai": 0, "scored_fallback": 0,
                "estimated_cost": 0.0, "stopped": False,
                "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
                "last_error": "", "mode": "batch",
                "batch_id": None, "batch_processing_status": None,
                "batch_request_counts": None,
                "message": "Parsing CSV\u2026", "jobs_found": [],
            })

        text = csv_bytes.decode("utf-8-sig")
        rows = list(_csv.DictReader(io.StringIO(text)))

        if not location and rows:
            location = (rows[0].get("Search Location") or "").strip() or None

        batch_label = _date.today().isoformat()
        if location:
            batch_label += f" \u2014 {location} (AI batch)"
        else:
            batch_label += " \u2014 AI batch"

        # Same filter pass as live mode
        seen, filtered = set(), []
        for r in rows:
            company = (r.get("Company") or "").strip()
            title = (r.get("Job Title") or "").strip()
            link = (r.get("Job Link") or "").strip()
            key = link if link else (company, title)
            if key in seen:
                continue
            seen.add(key)
            if is_excluded_company(company):
                continue
            tl = title.lower()
            desc = r.get("Description") or ""
            dl = desc.lower()
            if is_excluded_title(tl) or is_excluded_role(tl) \
                    or not is_swe_role(tl):
                continue
            if is_excluded_description(dl):
                continue
            filtered.append({
                "id": make_job_id(company, title, link),
                "company": company,
                "title": title,
                "location": (r.get("Location") or "").strip(),
                "work_type": (r.get("Work Type") or "").strip(),
                "salary": (r.get("Salary") or "N/A").strip(),
                "posted_date": (r.get("Posted Date") or "").strip(),
                "import_date": batch_label,
                "description": desc,
                "job_link": link,
                "apply_link": (r.get("Application Link") or "").strip(),
            })

        with _ai_import_lock:
            _ai_import_status["total"] = len(filtered)
            _ai_import_status["message"] = (
                f"Found {len(filtered)} qualifying jobs. Checking DB\u2026"
            )

        if not filtered:
            with _ai_import_lock:
                _ai_import_status.update({
                    "running": False,
                    "message": "No qualifying SWE jobs found in CSV.",
                })
            return

        # Dedup against existing
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT job_link FROM jobs WHERE job_link IS NOT NULL"
            )
            existing = {row[0] for row in cur.fetchall()}
        new_jobs = [j for j in filtered if j["job_link"] not in existing]
        # Keep an index from custom_id (job id) to job dict
        job_by_id = {j["id"]: j for j in new_jobs}

        with _ai_import_lock:
            _ai_import_status["total"] = len(new_jobs)
            _ai_import_status["message"] = (
                f"Submitting batch of {len(new_jobs)} jobs to Anthropic\u2026"
            )

        if not new_jobs:
            with _ai_import_lock:
                _ai_import_status.update({
                    "running": False,
                    "message": "No new jobs to import "
                    "(all dedupped against existing).",
                })
            return

        system_prompt = _build_scoring_system_prompt()
        requests = [
            _build_batch_request(j["id"], system_prompt, j)
            for j in new_jobs
        ]
        batch = _create_anthropic_batch(requests)

        with _ai_import_lock:
            _ai_import_status["batch_id"] = batch.id
            _ai_import_status["batch_processing_status"] = (
                getattr(batch, "processing_status", "in_progress")
            )
            _ai_import_status["message"] = (
                f"Batch {batch.id[:8]}\u2026 submitted. "
                f"Polling every 30s."
            )

        # Poll until done. The status endpoint reflects this state, so
        # the user can keep the dashboard open and watch progress.
        poll_interval = 30
        while True:
            if _ai_import_stop_event.is_set():
                # User requested stop. We don't cancel the Anthropic
                # batch (it's already paid for) — just exit and let
                # them retrieve results manually later if they want.
                with _ai_import_lock:
                    _ai_import_status.update({
                        "running": False,
                        "stopped": True,
                        "message": (
                            f"Stopped polling batch {batch.id[:8]}\u2026. "
                            f"Results will still be available from "
                            f"Anthropic when complete."
                        ),
                    })
                return

            time.sleep(poll_interval)
            try:
                batch = _get_anthropic_batch(batch.id)
            except Exception as e:
                with _ai_import_lock:
                    _ai_import_status["last_error"] = (
                        f"poll error: {str(e)[:200]}"
                    )
                continue

            counts = getattr(batch, "request_counts", None)
            counts_dict = None
            if counts is not None:
                counts_dict = {
                    "processing": getattr(counts, "processing", 0),
                    "succeeded": getattr(counts, "succeeded", 0),
                    "errored": getattr(counts, "errored", 0),
                    "canceled": getattr(counts, "canceled", 0),
                    "expired": getattr(counts, "expired", 0),
                }

            with _ai_import_lock:
                _ai_import_status["batch_processing_status"] = (
                    batch.processing_status
                )
                _ai_import_status["batch_request_counts"] = counts_dict
                if counts_dict:
                    finished = (
                        counts_dict["succeeded"]
                        + counts_dict["errored"]
                        + counts_dict["canceled"]
                        + counts_dict["expired"]
                    )
                    _ai_import_status["progress"] = finished
                    _ai_import_status["message"] = (
                        f"Batch {batch.id[:8]}\u2026 "
                        f"{batch.processing_status} "
                        f"({finished}/{len(new_jobs)} processed)"
                    )

            if batch.processing_status == "ended":
                break

        # Retrieve and parse results
        with _ai_import_lock:
            _ai_import_status["message"] = (
                f"Batch {batch.id[:8]}\u2026 ended. Retrieving results\u2026"
            )

        _TIER_RANK = {"Weak Match": 0, "Match": 1, "Strong Match": 2}
        scored_jobs = []
        for custom_id, result in _retrieve_anthropic_batch_results(batch.id):
            job = job_by_id.get(custom_id)
            if not job:
                # Result for an unknown id — skip
                continue
            # Always compute regex baseline for the comparison column
            dl = job["description"].lower()
            tl = job["title"].lower()
            regex_score = (
                calc_tech_score(dl)
                + calc_level_bonus(tl)
                + calc_company_bonus(job["company"])
            )
            job["regex_score"] = regex_score
            regex_tier = assign_tier(regex_score)

            if result.get("used_ai"):
                job["score"] = result["legacy_score"]
                job["tier"] = assign_tier(result["legacy_score"])
                job["ai_reasoning"] = result["reasoning"]
                ai_rank = _TIER_RANK.get(job["tier"], 1)
                rx_rank = _TIER_RANK.get(regex_tier, 1)
                with _ai_import_lock:
                    _ai_import_status["scored_ai"] += 1
                    # Batch API: 50% discount applied
                    _ai_import_status["estimated_cost"] += (
                        AI_SCORING_COST_PER_JOB * 0.5
                    )
                    if ai_rank == rx_rank:
                        _ai_import_status["regex_agree"] += 1
                    elif ai_rank > rx_rank:
                        _ai_import_status["ai_promoted"] += 1
                    else:
                        _ai_import_status["ai_demoted"] += 1
            else:
                job["score"] = regex_score
                job["tier"] = regex_tier
                job["ai_reasoning"] = None
                err = (result.get("error") or "")[:240]
                with _ai_import_lock:
                    _ai_import_status["scored_fallback"] += 1
                    if err:
                        _ai_import_status["last_error"] = err
            scored_jobs.append(job)

        with _ai_import_lock:
            _ai_import_status["message"] = (
                f"Inserting {len(scored_jobs)} jobs into DB\u2026"
            )

        # Insert (mirrors _run_import_csv_ai)
        with Db() as conn:
            cur = conn.cursor()
            for j in scored_jobs:
                bullets = pick_bullets(j["description"], j["title"])
                langs, frameworks, misc = customize_skills(j["description"])
                resume_text = generate_resume_txt(
                    j, bullets, langs, frameworks, misc
                )
                cur.execute("""
                    INSERT INTO jobs
                        (id, company, title, location, work_type, salary,
                         posted_date, import_date, description, score,
                         tier, job_link, apply_link, resume_text,
                         ai_reasoning, regex_score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    j["id"], j["company"], j["title"], j["location"],
                    j["work_type"], j["salary"], j["posted_date"],
                    j["import_date"], j["description"], j["score"],
                    j["tier"], j["job_link"], j["apply_link"],
                    resume_text, j.get("ai_reasoning"), j.get("regex_score"),
                ))
                cur.execute(
                    "INSERT INTO job_state (job_id) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (j["id"],)
                )

        tiers = {}
        for j in scored_jobs:
            tiers.setdefault(j["tier"], []).append(j["title"])
        summary = ", ".join(
            f"{t}: {len(v)}" for t, v in sorted(tiers.items())
        )

        with _ai_import_lock:
            _ai_import_status.update({
                "running": False,
                "added": len(scored_jobs),
                "message": (
                    f"Done! Batch {batch.id[:8]}\u2026 inserted "
                    f"{len(scored_jobs)} jobs "
                    f"({_ai_import_status['scored_ai']} AI, "
                    f"{_ai_import_status['scored_fallback']} fallback), "
                    f"cost ~${_ai_import_status['estimated_cost']:.3f} "
                    f"(50% batch discount). {summary}."
                ),
                "jobs_found": [
                    {"title": j["title"], "tier": j["tier"],
                     "score": j["score"], "location": j["location"]}
                    for j in sorted(
                        scored_jobs, key=lambda x: -x["score"]
                    )[:20]
                ],
            })

    except Exception as e:
        with _ai_import_lock:
            _ai_import_status.update(
                {"running": False, "message": f"Batch error: {str(e)}"}
            )


# ─────────────────────────────────────────────────────────
# JOB DESCRIPTION FORMATTER
# ─────────────────────────────────────────────────────────

def _extract_relevant_description(description, max_chars=800):
    if not description:
        return ''
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
    skip_pattern = re.compile(
        r'(about us|about the company|who we are|our mission'
        r'|benefits|total rewards|compensation|salary range|pay range|pay transparency'
        r'|equal opportunity|eeo |diversity|accommodation'
        r'|privacy notice|employee applicant|disclaimer|legal notice'
        r'|how to apply|next steps|interview process'
        r'|please note that|note:)',
        re.IGNORECASE
    )
    match = relevant_pattern.search(description)
    if match:
        relevant_text = description[match.start():]
        skip_match = skip_pattern.search(relevant_text)
        if skip_match and skip_match.start() > 100:
            relevant_text = relevant_text[:skip_match.start()]
    else:
        total = len(description)
        start = min(total // 3, 500)
        boundary = description.find('.  ', start)
        if 0 < boundary < start + 200:
            relevant_text = description[boundary + 3:]
        else:
            relevant_text = description[start:]

    relevant_text = re.sub(r'\s{2,}', '  ', relevant_text).strip()
    if len(relevant_text) > max_chars:
        relevant_text = relevant_text[:max_chars].rsplit(' ', 1)[0] + '...'
    return relevant_text


def format_job_description(raw):
    import html as html_mod
    if not raw or not raw.strip():
        return "<p>No description available.</p>"

    raw = raw.replace('\u2018', "'").replace('\u2019', "'")
    raw = raw.replace('\u201c', '"').replace('\u201d', '"')
    # Normalize markdown headers (## Header or **Header**) to plain
    # text on their own line so the header_re can detect them
    raw = re.sub(r'^#{1,4}\s+(.+)$', r'\1', raw, flags=re.MULTILINE)
    raw = re.sub(
        r'^\*{1,2}([^*\n]+)\*{1,2}\s*$', r'\1',
        raw, flags=re.MULTILINE
    )
    # Convert inline bold/italic markdown to HTML
    raw = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', raw)
    raw = re.sub(r'\*([^*]+)\*', r'<i>\1</i>', raw)
    chunks = re.split(r'(?:  +|\n\n+|\n)', raw.strip())

    header_re = re.compile(
        r"^(?:about (?:the |this )?(?:role|position|team|company|job|us|you)|"
        r"what you'?ll (?:do|bring|need|work on)|who you are|who we are|"
        r"(?:key |core |your )?responsibilities|"
        r"(?:minimum |preferred |basic |required |desired )?(?:qualifications|requirements|skills|experience)|"
        r"nice to have|bonus points|preferred skills|(?:what )?we (?:offer|provide|'re looking for)|"
        r"benefits(?: and perks)?|compensation|perks|why (?:join|work)|our (?:team|culture|mission)|"
        r"tech(?:nology)? stack|tools we use|job (?:description|details|summary|category)|"
        r"equal opportunity|eeo|diversity|"
        r"(?:location|pay|salary|base|total) (?:requirement|range|details)?|"
        r"how to apply|additional information|the (?:role|team|opportunity|impact)|"
        r"you(?:'ll| will) (?:have|be)|in this role)s?\s*:?\s*$",
        re.IGNORECASE
    )
    bullet_re = re.compile(r'^(?:[•\-\*]|\d+[.)]\s)\s*')
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
        if bullet_re.match(chunk):
            lines.append(('bullet', bullet_re.sub('', chunk)))
        elif (header_re.match(chunk) or (len(chunk) < 60 and chunk.endswith(':'))
              or (len(chunk) < 50 and chunk == chunk.title() and not chunk.endswith('.'))):
            lines.append(('header', chunk.rstrip(':')))
        else:
            split_bullets = run_together_re.split(chunk)
            if len(split_bullets) > 2 and all(len(s.strip()) > 15 for s in split_bullets):
                for b in split_bullets:
                    lines.append(('bullet', b.strip()))
            else:
                lines.append(('text', chunk))

    parts, in_list = [], False
    for kind, content in lines:
        escaped = html_mod.escape(content)
        # Restore inline bold/italic tags that were converted
        # from markdown before chunking
        escaped = escaped.replace(
            '&lt;b&gt;', '<b>'
        ).replace(
            '&lt;/b&gt;', '</b>'
        ).replace(
            '&lt;i&gt;', '<i>'
        ).replace(
            '&lt;/i&gt;', '</i>'
        )
        if kind == 'header':
            if in_list:
                parts.append('</ul>')
                in_list = False
            parts.append(f'<h4 style="font-weight:600;font-size:0.85rem;color:#374151;margin:1rem 0 0.35rem 0;">{escaped}</h4>')
        elif kind == 'bullet':
            if not in_list:
                parts.append('<ul style="margin:0.25rem 0;padding-left:1.25rem;list-style:disc;">')
                in_list = True
            parts.append(f'<li style="font-size:0.75rem;color:#4b5563;margin-bottom:0.2rem;">{escaped}</li>')
        else:
            if in_list:
                parts.append('</ul>')
                in_list = False
            parts.append(f'<p style="font-size:0.75rem;color:#4b5563;margin:0.5rem 0;">{escaped}</p>')
    if in_list:
        parts.append('</ul>')
    return '\n'.join(parts)


# ─────────────────────────────────────────────────────────
# PDF PLAN GENERATOR
# ─────────────────────────────────────────────────────────

def generate_plan_pdf(plan, jobs_lookup):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

    pdf_path = os.path.join(DIR, f"plan_{plan['id']}.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=letter,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle('PlanTitle', parent=styles['Title'], fontSize=22, spaceAfter=4, textColor=colors.HexColor('#1e3a5f')))
    styles.add(ParagraphStyle('PlanSubtitle', parent=styles['Normal'], fontSize=11, textColor=colors.HexColor('#64748b'), spaceAfter=16))
    styles.add(ParagraphStyle('JobTitle', parent=styles['Normal'], fontSize=11, textColor=colors.HexColor('#1e293b'), leading=14))
    styles.add(ParagraphStyle('JobDetail', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#475569'), leading=12))
    styles.add(ParagraphStyle('Notes', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#6b21a8'), leading=12, fontName='Helvetica-Oblique'))
    styles.add(ParagraphStyle('JobID', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#94a3b8'), leading=10))
    styles.add(ParagraphStyle('Description', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#334155'), leading=11))

    TIER_COLORS = {'Strong Match': colors.HexColor('#16a34a'), 'Match': colors.HexColor('#2563eb'), 'Weak Match': colors.HexColor('#d97706')}
    TIER_BG = {'Strong Match': colors.HexColor('#f0fdf4'), 'Match': colors.HexColor('#eff6ff'), 'Weak Match': colors.HexColor('#fffbeb')}

    story = [Paragraph("Application Plan", styles['PlanTitle'])]
    job_count = len(plan.get('jobs', []))
    story.append(Paragraph(f"Date: <b>{plan['date']}</b>  &bull;  {job_count} position{'s' if job_count != 1 else ''}", styles['PlanSubtitle']))
    if plan.get('title'):
        story.append(Paragraph(plan['title'], styles['Heading2']))
        story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 12))

    for idx, plan_job in enumerate(plan.get('jobs', [])):
        job_id = plan_job.get('id', '')
        job = jobs_lookup.get(job_id, {})
        tier = job.get('tier', 'Weak Match')
        tier_color = TIER_COLORS.get(tier, colors.gray)
        tier_bg = TIER_BG.get(tier, colors.HexColor('#f8fafc'))
        company = job.get('company', plan_job.get('company', 'Unknown'))
        title = job.get('title', plan_job.get('title', 'Unknown'))
        score = job.get('score', 0)
        notes = plan_job.get('notes', '')

        card_data = [[
            Paragraph(f"<b>{idx + 1}.</b> <b>{company}</b> - {title}", styles['JobTitle']),
            Paragraph(f"<font color='white'><b> {tier} (Score: {score}) </b></font>",
                      ParagraphStyle('tb', parent=styles['Normal'], fontSize=8, alignment=2, textColor=colors.white)),
        ]]
        if job_id:
            card_data.append([Paragraph(f"ID: <b>{job_id}</b>", styles['JobID']), ''])
        details = " | ".join(filter(None, [
            f"Salary: {job.get('salary')}" if job.get('salary') and job.get('salary') != 'N/A' else None,
            f"Location: {job.get('location')}" if job.get('location') else None,
            f"Type: {job.get('work_type')}" if job.get('work_type') else None,
        ])) or "No details"
        card_data.append([Paragraph(details, styles['JobDetail']), ''])
        desc = _extract_relevant_description(job.get('description', ''), 800)
        if desc:
            desc = desc.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            card_data.append([Paragraph(f"<b>Role Details:</b> {desc}", styles['Description']), ''])
        links = " | ".join(filter(None, [
            f'<link href="{job.get("apply_link")}"><u>Apply</u></link>' if job.get('apply_link') else None,
            f'<link href="{job.get("job_link")}"><u>LinkedIn</u></link>' if job.get('job_link') else None,
        ]))
        if links:
            card_data.append([Paragraph(links, styles['JobDetail']), ''])
        if notes:
            card_data.append([Paragraph(f"Notes: {notes}", styles['Notes']), ''])
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
        for r in range(1, len(card_data)):
            style_cmds.append(('SPAN', (0, r), (1, r)))
        t.setStyle(TableStyle(style_cmds))
        story.append(t)
        story.append(Spacer(1, 10))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 8))
    tier_counts = {}
    for pj in plan.get('jobs', []):
        t = jobs_lookup.get(pj.get('id', ''), {}).get('tier', 'Unknown')
        tier_counts[t] = tier_counts.get(t, 0) + 1
    summary_parts = [f"<b>Summary:</b> {job_count} positions"]
    for t_name, hex_color in [('Strong Match', '#16a34a'), ('Match', '#2563eb'), ('Weak Match', '#d97706')]:
        if tier_counts.get(t_name, 0) > 0:
            summary_parts.append(f"<font color='{hex_color}'>{t_name}: {tier_counts[t_name]}</font>")
    story.append(Paragraph("  |  ".join(summary_parts), styles['JobDetail']))

    doc.build(story)
    return pdf_path


# ─────────────────────────────────────────────────────────
# HTTP REQUEST HANDLER
# ─────────────────────────────────────────────────────────

def _json_response(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):
        # ── /login — serve login page (no auth required) ─
        if self.path.startswith("/login"):
            error = "error=1" in self.path
            error_block = (
                '<div class="error">Incorrect password. Please try again.</div>'
                if error else ""
            )
            html = _LOGIN_HTML.replace("{error_block}", error_block)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            return

        # ── /api/logout — clear session ───────────────────
        if self.path == "/api/logout":
            token = _get_session_cookie(self)
            if token:
                _SESSIONS.discard(token)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header(
                "Set-Cookie",
                "session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0",
            )
            self.end_headers()
            return

        # ── Auth gate — redirect to /login if unauthenticated ─
        if not _is_authenticated(self):
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        # ── Serve resume PDFs from resumes/ directory ─────
        if self.path.startswith("/resumes/") and self.path.endswith(".pdf"):
            pdf_file = os.path.join(PARENT_DIR, self.path.lstrip("/"))
            if os.path.isfile(pdf_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                with open(pdf_file, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        if self.path == "/":
            self.path = "/dashboard.html"

        # ── /api/config ──────────────────────────────────
        if self.path == "/api/config":
            _json_response(self, {
                "name": _config["candidate"]["name"],
                "linkedin_url": _config["candidate"]["linkedin_url"],
            })
            return

        # ── /api/profile — candidate + experience config ──
        if self.path == "/api/profile":
            _json_response(self, {
                "candidate": _config.get("candidate", {}),
                "experience": {
                    key: {
                        "key": key,
                        "display_name": entry.get("display_name", key),
                        "context": entry.get("context", ""),
                        "bullet_limit": entry.get("bullet_limit", 5),
                        "bullets": entry.get("bullets", []),
                    }
                    for key, entry in _config.get("experience", {}).items()
                },
                "skills_template": _config.get("skills_template", {}),
            })
            return

        # ── /api/state — all job state ───────────────────
        if self.path == "/api/state":
            _json_response(self, load_state())
            return

        # ── /api/jobs-data — full job list + state merged ─
        if self.path == "/api/jobs-data":
            try:
                # Local import to match the existing pattern in this file
                from process_new_postings import assign_tier
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT j.id, j.company, j.title, j.location, j.work_type, j.salary,
                               j.posted_date, j.import_date, j.score, j.tier,
                               j.job_link, j.apply_link, j.ai_reasoning, j.regex_score,
                               js.status, js.notes, js.applicants,
                               js.live_status, js.live_status_checked, js.timestamps,
                               js.pdf_path
                        FROM jobs j
                        LEFT JOIN job_state js ON j.id = js.job_id
                        ORDER BY j.import_date DESC, j.created_at DESC, j.score DESC
                    """)
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                jobs = []
                for row in rows:
                    job = _serialize(dict(zip(cols, row)))
                    # Derive regex_tier from regex_score using the same
                    # assign_tier() the scoring pipeline uses, so the
                    # frontend doesn't need its own thresholds.
                    rs = job.get("regex_score")
                    job["regex_tier"] = (
                        assign_tier(rs) if rs is not None else None
                    )
                    jobs.append(job)
                _json_response(self, jobs)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/batch-stats ─────────────────────────────
        if self.path == "/api/batch-stats":
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT import_date, tier, COUNT(*) as count
                        FROM jobs GROUP BY import_date, tier
                        ORDER BY import_date DESC
                    """)
                    rows = cur.fetchall()
                batches = {}
                for import_date, tier, count in rows:
                    if import_date not in batches:
                        batches[import_date] = {"date": import_date, "tiers": {}}
                    batches[import_date]["tiers"][tier] = count
                _json_response(self, list(batches.values()))
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/jobs — job IDs + links (for update scripts) ─
        if self.path == "/api/jobs":
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id, job_link FROM jobs WHERE job_link IS NOT NULL")
                    jobs = [{"id": row[0], "job_link": row[1]} for row in cur.fetchall()]
                _json_response(self, jobs)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/plans ───────────────────────────────────
        if self.path == "/api/plans":
            _json_response(self, load_plans())
            return

        # ── /api/update-status ───────────────────────────
        if self.path == "/api/update-status":
            with _update_lock:
                _json_response(self, dict(_update_status))
            return

        # ── /api/live-check-status ───────────────────────
        if self.path == "/api/live-check-status":
            with _live_check_lock:
                _json_response(self, dict(_live_check_status))
            return

        # ── /api/scrape-status ───────────────────────────
        if self.path == "/api/scrape-status":
            with _scrape_lock:
                _json_response(self, dict(_scrape_status))
            return

        # ── /api/import-status ───────────────────────────
        if self.path == "/api/import-status":
            with _import_lock:
                _json_response(self, dict(_import_status))
            return

        # ── /api/ai-import-status ────────────────────────
        if self.path == "/api/ai-import-status":
            with _ai_import_lock:
                _json_response(self, dict(_ai_import_status))
            return

        # ── /api/ai-import-stream (Server-Sent Events) ───
        # Holds the connection open and pushes status frames as the AI
        # import progresses.  The browser's EventSource API auto-
        # reconnects on page refresh, so callers don't need separate
        # polling after a reload.
        if self.path == "/api/ai-import-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # Tell nginx / AWS ALB not to buffer the stream.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                last_json = None
                while True:
                    with _ai_import_lock:
                        snap = dict(_ai_import_status)
                    snap_json = json.dumps(snap)
                    if snap_json != last_json:
                        frame = f"data: {snap_json}\n\n"
                        self.wfile.write(frame.encode())
                        self.wfile.flush()
                        last_json = snap_json
                    else:
                        # Heartbeat keeps nginx / load-balancers from
                        # closing an idle connection (SSE comment line).
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    if not snap.get("running"):
                        break   # import done — client handles reconnect
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # client disconnected, nothing to do
            return

        # ── /api/refresh-job/<id> ────────────────────────
        if self.path.startswith("/api/refresh-job/"):
            job_id = self.path[len("/api/refresh-job/"):].strip()
            if not job_id:
                _json_response(self, {"error": "Missing job ID"}, 400)
                return
            try:
                job = load_job(job_id)
                if not job:
                    _json_response(self, {"error": "Job not found"}, 404)
                    return
                state = load_state()
                result = dict(state.get(job_id, {}))

                job_link = job.get("job_link", "")
                m = re.search(r'/view/(\d+)', job_link)
                if m:
                    applicants = _fetch_applicants_curl(m.group(1))
                    if applicants:
                        result["applicants"] = applicants
                        if job_id not in state:
                            state[job_id] = {}
                        state[job_id]["applicants"] = applicants
                        save_job_state(job_id, state[job_id])

                is_linkedin = "/linkedin.com/" in job_link
                live_result = _check_job_live_linkedin(job_link) if is_linkedin else _check_apply_link(job_link)
                if live_result != "inconclusive":
                    import datetime
                    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    result["live_status"] = live_result
                    result["live_status_checked"] = now_iso
                    if job_id not in state:
                        state[job_id] = {}
                    state[job_id]["live_status"] = live_result
                    state[job_id]["live_status_checked"] = now_iso
                    save_job_state(job_id, state[job_id])

                _json_response(self, result)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/company-info/<name> ─────────────────────
        if self.path.startswith("/api/company-info/"):
            from urllib.parse import unquote
            company_name = unquote(self.path[len("/api/company-info/"):]).strip()
            if not company_name:
                _json_response(self, {"error": "Missing company name"}, 400)
                return
            try:
                _json_response(self, _fetch_company_info(company_name))
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/resume-text/<id> ─────────────────────────
        if self.path.startswith("/api/resume-text/"):
            job_id = self.path[len("/api/resume-text/"):]
            try:
                job = load_job(job_id)
                if not job:
                    _json_response(
                        self, {"error": "Job not found"}, 404
                    )
                    return
                text = job.get("resume_text", "")
                if not text:
                    _json_response(
                        self, {"error": "No resume text"}, 404
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(text.encode())
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/job-description/<id> ────────────────────
        if self.path.startswith("/api/job-description/"):
            job_id = self.path[len("/api/job-description/"):]
            try:
                job = load_job(job_id)
                if not job:
                    _json_response(self, {"error": "Job not found"}, 404)
                    return
                raw = job.get("description", "")
                _json_response(self, {"description": raw, "formatted": format_job_description(raw)})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/plan-pdf/<id> ───────────────────────────
        if self.path.startswith("/api/plan-pdf/"):
            plan_id = self.path[len("/api/plan-pdf/"):]
            plans = load_plans()
            plan = next((p for p in plans if p['id'] == plan_id), None)
            if not plan:
                _json_response(self, {"error": "Plan not found"}, 404)
                return
            try:
                jobs = load_jobs()
                jobs_lookup = {j['id']: j for j in jobs}
                pdf_path = generate_plan_pdf(plan, jobs_lookup)
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="application_plan_{plan["date"]}.pdf"')
                self.end_headers()
                with open(pdf_path, "rb") as f:
                    self.wfile.write(f.read())
                os.remove(pdf_path)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /resumes/* — serve resume files ─────────────
        if self.path.startswith("/resumes/"):
            from urllib.parse import unquote
            rel = unquote(self.path[1:])
            file_path = os.path.normpath(os.path.join(PARENT_DIR, rel))
            if not file_path.startswith(PARENT_DIR):
                self.send_response(403)
                self.end_headers()
                return
            if os.path.isfile(file_path):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf" if file_path.endswith(".pdf") else "text/plain; charset=utf-8")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
            return

        return super().do_GET()

    def do_POST(self):
        # ── POST /api/login — check password, set session cookie ──
        if self.path == "/api/login":
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode())
            password = params.get("password", [""])[0]
            if _AUTH_PASSWORD and password == _AUTH_PASSWORD:
                token = secrets.token_hex(32)
                _SESSIONS.add(token)
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"session={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000",
                )
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header("Location", "/login?error=1")
                self.end_headers()
            return

        # ── Auth gate for POST routes ─────────────────────
        if not _is_authenticated(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"Unauthorized"}')
            return

        # ── /api/upload-resume/<job_id> ───────────────────
        if self.path.startswith("/api/upload-resume/"):
            try:
                job_id = self.path[len("/api/upload-resume/"):]
                pdf_dir = os.path.join(PARENT_DIR, "resumes", job_id)
                os.makedirs(pdf_dir, exist_ok=True)
                content_length = int(
                    self.headers.get("Content-Length", 0)
                )
                file_data = self.rfile.read(content_length)
                pdf_file = os.path.join(pdf_dir, "resume.pdf")
                with open(pdf_file, "wb") as f:
                    f.write(file_data)
                relative_path = f"resumes/{job_id}/resume.pdf"
                # Persist pdf_path in job_state
                state = load_state().get(job_id, {})
                state["pdf_path"] = relative_path
                save_job_state(job_id, state)
                _json_response(
                    self, {"ok": True, "path": relative_path}
                )
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/delete-jobs — delete one or more jobs ─
        if self.path == "/api/delete-jobs":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            job_ids = body.get("ids", [])
            if not job_ids:
                _json_response(
                    self, {"error": "No job IDs provided"}, 400
                )
                return
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "DELETE FROM application_plan_jobs "
                        "WHERE job_id = ANY(%s)", (job_ids,)
                    )
                    cur.execute(
                        "DELETE FROM job_state "
                        "WHERE job_id = ANY(%s)", (job_ids,)
                    )
                    cur.execute(
                        "DELETE FROM jobs WHERE id = ANY(%s)",
                        (job_ids,)
                    )
                    deleted = cur.rowcount
                _json_response(
                    self, {"ok": True, "deleted": deleted}
                )
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/delete-batch — delete all jobs in a batch
        if self.path == "/api/delete-batch":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            batch = body.get("batch", "")
            if not batch:
                _json_response(
                    self, {"error": "No batch specified"}, 400
                )
                return
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    # Get all job IDs in this batch
                    cur.execute(
                        "SELECT id FROM jobs "
                        "WHERE import_date = %s", (batch,)
                    )
                    job_ids = [r[0] for r in cur.fetchall()]
                    if not job_ids:
                        _json_response(
                            self, {"ok": True, "deleted": 0}
                        )
                        return
                    cur.execute(
                        "DELETE FROM application_plan_jobs "
                        "WHERE job_id = ANY(%s)", (job_ids,)
                    )
                    cur.execute(
                        "DELETE FROM job_state "
                        "WHERE job_id = ANY(%s)", (job_ids,)
                    )
                    cur.execute(
                        "DELETE FROM jobs WHERE id = ANY(%s)",
                        (job_ids,)
                    )
                    deleted = cur.rowcount
                _json_response(
                    self, {"ok": True, "deleted": deleted}
                )
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/state — save single job state ──────
        if self.path == "/api/state":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                job_id = data.get("id")
                job_state = data.get("state")
                if job_id and job_state is not None:
                    save_job_state(job_id, job_state)
                    _json_response(self, {"ok": True})
                else:
                    _json_response(self, {"error": "missing id or state"}, 400)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/profile — update candidate + experience ─
        if self.path == "/api/profile":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                config_path = os.path.join(PARENT_DIR, "config.json")
                with open(config_path) as f:
                    cfg = json.load(f)

                # Update candidate info if provided
                if "candidate" in body:
                    for k in ("name", "linkedin_url", "summary",
                              "skills"):
                        if k in body["candidate"]:
                            cfg["candidate"][k] = body["candidate"][k]

                # Update experience if provided
                if "experience" in body:
                    cfg["experience"] = body["experience"]

                # Update skills_template if provided
                if "skills_template" in body:
                    cfg["skills_template"] = body["skills_template"]

                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=2)
                    f.write("\n")

                # Reload in-memory config
                global _config, _BULLET_POOL, _CANDIDATE_SKILLS
                _config = cfg
                _BULLET_POOL = {
                    entry.get("display_name", key): entry["bullets"]
                    for key, entry in cfg["experience"].items()
                }
                _CANDIDATE_SKILLS = cfg["candidate"]["skills"]

                # Reload process_new_postings module variables
                # so pick_bullets/customize_skills use new config
                import process_new_postings as pnp
                pnp._config = cfg
                pnp.BULLETS = {
                    entry.get("display_name", key): entry["bullets"]
                    for key, entry in cfg["experience"].items()
                }
                pnp.BULLET_LIMITS = {
                    entry.get("display_name", key): entry[
                        "bullet_limit"
                    ]
                    for key, entry in cfg["experience"].items()
                }
                pnp.SKILLS_DEFAULT = cfg.get(
                    "skills_template", pnp.SKILLS_DEFAULT
                )
                pnp.BULLET_KEYWORD_PAIRS = [
                    (p[0], p[1])
                    for p in cfg.get("bullet_keyword_pairs", [])
                ]

                _json_response(self, {"ok": True})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/plans — save all plans ────────────
        if self.path == "/api/plans":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                plans = json.loads(body)
                save_plans(plans)
                _json_response(self, {"ok": True})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/import-url (AI extraction) ─────────
        if self.path == "/api/import-url":
            if not _ANTHROPIC_KEY:
                _json_response(self, {
                    "error": "Anthropic API key not configured."
                }, 500)
                return

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            url = (body.get("url") or "").strip()
            company_override = (body.get("company_override") or "").strip()

            if not url or not url.startswith(("http://", "https://")):
                _json_response(
                    self, {"error": "Invalid URL"}, 400
                )
                return

            try:
                import requests as req_lib
                from bs4 import BeautifulSoup
                from datetime import date as _date
                sys.path.insert(0, PARENT_DIR)
                from process_new_postings import (
                    is_excluded_company,
                    calc_tech_score, calc_level_bonus,
                    calc_company_bonus, assign_tier,
                    make_job_id, pick_bullets,
                    customize_skills, generate_resume_txt,
                )

                # Fetch page
                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; "
                    "Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                }
                resp = req_lib.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                page_text = soup.get_text(separator="\n", strip=True)

                if len(page_text) < 100:
                    _json_response(self, {
                        "error": "Page returned too little text. "
                        "It may require JavaScript rendering."
                    }, 400)
                    return

                # Truncate to avoid huge token costs
                page_text = page_text[:15000]

                # Ask Claude to extract fields
                extraction_prompt = (
                    "Extract the following fields from this job "
                    "posting page text.\n"
                    "Return ONLY a JSON object with these keys:\n\n"
                    '{\n'
                    '  "company": "Company name",\n'
                    '  "title": "Job title",\n'
                    '  "location": "Job location or Remote",\n'
                    '  "salary": "Salary range if listed, '
                    'otherwise N/A",\n'
                    '  "work_type": "Remote, Hybrid, On-site, '
                    'or empty if unclear",\n'
                    '  "description": "The full job description '
                    'text, cleaned up and formatted with clear '
                    'section headers, bullet points for '
                    'requirements/responsibilities, and '
                    'readable paragraphs. Remove boilerplate '
                    '(EEO statements, cookie notices, nav text). '
                    'Use newlines for structure."\n'
                    '}\n\n'
                    "If a field cannot be determined, use an "
                    "empty string.\n"
                    "Do not include any text outside the JSON "
                    "object.\n\n"
                    "PAGE TEXT:\n" + page_text
                )

                ai_response = _call_claude(
                    "You extract structured job posting data "
                    "from web page text. Return only valid JSON.",
                    [{"role": "user", "content": extraction_prompt}]
                )

                # Parse Claude's JSON response
                json_match = re.search(
                    r'\{[\s\S]*\}', ai_response
                )
                if not json_match:
                    _json_response(self, {
                        "error": "AI could not extract job data"
                    }, 400)
                    return

                extracted = json.loads(json_match.group())
                company = (
                    company_override
                    or extracted.get("company", "").strip()
                )
                title = extracted.get("title", "").strip()
                desc = extracted.get("description", "").strip()

                if not company or not title or not desc:
                    _json_response(self, {
                        "error": "Could not extract required "
                        "fields (company, title, description)"
                    }, 400)
                    return

                if is_excluded_company(company):
                    _json_response(self, {
                        "error": f"'{company}' matches a "
                        "staffing firm exclusion pattern."
                    }, 400)
                    return

                # Dedup
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id FROM jobs "
                        "WHERE job_link = %s", (url,)
                    )
                    if cur.fetchone():
                        _json_response(self, {
                            "error": "Job already exists"
                        }, 409)
                        return

                # Score and insert
                dl = desc.lower()
                tl = title.lower()
                score = (calc_tech_score(dl)
                         + calc_level_bonus(tl)
                         + calc_company_bonus(company))
                tier = assign_tier(score)
                job_id = make_job_id(company, title, url)
                job = {
                    "company": company,
                    "title": title,
                    "description": desc,
                }
                bullets = pick_bullets(desc, title)
                langs, fw, misc = customize_skills(desc)
                resume_text = generate_resume_txt(
                    job, bullets, langs, fw, misc
                )
                batch = "AI Import"

                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO jobs
                            (id, company, title, location,
                             work_type, salary, posted_date,
                             import_date, description, score,
                             tier, job_link, apply_link,
                             resume_text)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                                %s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO NOTHING
                    """, (
                        job_id, company, title,
                        extracted.get("location", ""),
                        extracted.get("work_type", ""),
                        extracted.get("salary", "N/A"),
                        "", batch, desc, score, tier,
                        url, url, resume_text,
                    ))
                    cur.execute(
                        "INSERT INTO job_state (job_id) "
                        "VALUES (%s) ON CONFLICT DO NOTHING",
                        (job_id,)
                    )

                _json_response(self, {
                    "ok": True,
                    "job": {
                        "id": job_id,
                        "company": company,
                        "title": title,
                        "score": score,
                        "tier": tier,
                    }
                })
            except json.JSONDecodeError:
                _json_response(self, {
                    "error": "AI returned invalid JSON"
                }, 500)
            except Exception as e:
                _json_response(
                    self, {"error": str(e)}, 500
                )
            return

        # ── POST /api/add-job (manual entry) ──────────────
        if self.path == "/api/add-job":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            company = (body.get("company") or "").strip()
            title = (body.get("title") or "").strip()
            desc = (body.get("description") or "").strip()

            if not company or not title or not desc:
                _json_response(self, {
                    "error": "Company, title, and description "
                    "are required"
                }, 400)
                return

            try:
                from datetime import date as _date
                sys.path.insert(0, PARENT_DIR)
                from process_new_postings import (
                    calc_tech_score, calc_level_bonus,
                    calc_company_bonus, assign_tier,
                    make_job_id, pick_bullets,
                    customize_skills, generate_resume_txt,
                )

                job_link = (body.get("job_link") or "").strip()
                apply_link = (
                    body.get("apply_link") or job_link
                ).strip()

                # Dedup if URL provided
                if job_link:
                    with Db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT id FROM jobs "
                            "WHERE job_link = %s", (job_link,)
                        )
                        if cur.fetchone():
                            _json_response(self, {
                                "error": "Job already exists"
                            }, 409)
                            return

                dl = desc.lower()
                tl = title.lower()
                score = (calc_tech_score(dl)
                         + calc_level_bonus(tl)
                         + calc_company_bonus(company))
                tier = assign_tier(score)
                job_id = make_job_id(company, title, job_link)
                job = {
                    "company": company,
                    "title": title,
                    "description": desc,
                }
                bullets = pick_bullets(desc, title)
                langs, fw, misc = customize_skills(desc)
                resume_text = generate_resume_txt(
                    job, bullets, langs, fw, misc
                )
                batch = "AI Import"

                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO jobs
                            (id, company, title, location,
                             work_type, salary, posted_date,
                             import_date, description, score,
                             tier, job_link, apply_link,
                             resume_text)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                                %s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO NOTHING
                    """, (
                        job_id, company, title,
                        (body.get("location") or "").strip(),
                        (body.get("work_type") or "").strip(),
                        (body.get("salary") or "N/A").strip(),
                        "", batch, desc, score, tier,
                        job_link, apply_link, resume_text,
                    ))
                    cur.execute(
                        "INSERT INTO job_state (job_id) "
                        "VALUES (%s) ON CONFLICT DO NOTHING",
                        (job_id,)
                    )

                _json_response(self, {
                    "ok": True,
                    "job": {
                        "id": job_id,
                        "company": company,
                        "title": title,
                        "score": score,
                        "tier": tier,
                    }
                })
            except Exception as e:
                _json_response(
                    self, {"error": str(e)}, 500
                )
            return

        # ── POST /api/scrape-careers ─────────────────────
        if self.path == "/api/scrape-careers":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                url = data.get("url", "").strip()
                company = data.get("company", "").strip() or None
                if not url:
                    _json_response(self, {"error": "URL is required"}, 400)
                    return
                if _scrape_status.get("running"):
                    _json_response(self, {"ok": True, "message": "Already running"})
                else:
                    threading.Thread(target=_run_scrape_careers, args=(url, company), daemon=True).start()
                    _json_response(self, {"ok": True, "message": "Started"})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-import-stop — graceful stop ─────
        if self.path == "/api/ai-import-stop":
            if not _ai_import_status.get("running"):
                _json_response(self, {
                    "ok": True, "message": "Not running"
                })
                return
            _ai_import_stop_event.set()
            with _ai_import_lock:
                _ai_import_status["message"] = (
                    "Stop requested \u2014 finishing remaining "
                    "jobs with regex fallback..."
                )
            _json_response(self, {"ok": True})
            return

        # ── POST /api/import-csv-ai — AI scoring CSV import ─
        # Must come BEFORE /api/import-csv since that uses startswith.
        # Optional ?mode=batch query param routes to the Anthropic
        # Batch API path (50% discount, async). Default mode is "live".
        if self.path.startswith("/api/import-csv-ai"):
            if not _ANTHROPIC_KEY:
                _json_response(self, {
                    "error": "Anthropic API key not configured"
                }, 500)
                return
            if _ai_import_status.get("running"):
                _json_response(self, {
                    "ok": True, "message": "Already running"
                })
                return
            try:
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                )
                loc = (qs.get("location", [""])[0] or "").strip() or None
                mode = (qs.get("mode", ["live"])[0] or "live").strip().lower()
                length = int(self.headers.get("Content-Length", 0))
                csv_bytes = self.rfile.read(length)
                if not csv_bytes:
                    _json_response(self, {"error": "Empty body"}, 400)
                    return
                target = (
                    _run_import_csv_ai_batch if mode == "batch"
                    else _run_import_csv_ai
                )
                threading.Thread(
                    target=target,
                    args=(csv_bytes, loc),
                    daemon=True,
                ).start()
                _json_response(self, {"ok": True, "mode": mode})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/import-csv — LinkedIn CSV upload ───
        if self.path.startswith("/api/import-csv"):
            if _import_status.get("running"):
                _json_response(self, {"ok": True, "message": "Already running"})
                return
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                loc = (qs.get("location", [""])[0] or "").strip() or None
                length = int(self.headers.get("Content-Length", 0))
                csv_bytes = self.rfile.read(length)
                if not csv_bytes:
                    _json_response(self, {"error": "Empty body"}, 400)
                    return
                threading.Thread(target=_run_import_csv, args=(csv_bytes, loc), daemon=True).start()
                _json_response(self, {"ok": True})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-chat ────────────────────────────
        if self.path == "/api/ai-chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            user_message = body.get("message", "")
            job_id = body.get("job_id")
            history = body.get("history", [])
            model = body.get("model", "qwen2.5:14b")

            if not user_message:
                _json_response(self, {"error": "No message provided"}, 400)
                return

            _candidate_name = _config["candidate"]["name"]
            _experience_contexts = ", ".join(
                entry.get("context", key) for key, entry in _config["experience"].items()
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
                f"IMPORTANT: When discussing resume improvements, you must work with {_candidate_name}'s ACTUAL experience bullets listed below.",
                f"\n--- {_candidate_name.upper()}'S FULL EXPERIENCE BULLET POOL ---",
                "These are all the resume bullets available:",
                _format_bullet_pool(),
            ]

            if job_id:
                job_context, resume_text = _build_job_context(job_id)
                if job_context:
                    system_parts.append("\n--- TARGET JOB POSTING ---\n" + job_context)
                if resume_text:
                    system_parts.append("\n--- CURRENT TAILORED RESUME FOR THIS JOB ---")
                    system_parts.append(resume_text)

            messages = [{"role": "system", "content": "\n".join(system_parts)}]
            messages.extend(history)
            messages.append({"role": "user", "content": user_message})

            try:
                response = _call_ollama(messages, model=model)
                _json_response(self, {"response": response, "model": model})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-review (Claude API) ──────────────
        if self.path == "/api/ai-review":
            if not _ANTHROPIC_KEY:
                _json_response(
                    self,
                    {"error": "Anthropic API key not configured. "
                     "Set ANTHROPIC_API_KEY env var or add "
                     "/job-search/anthropic-api-key to Parameter Store."},
                    500)
                return

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            user_message = body.get("message", "")
            job_id = body.get("job_id")
            history = body.get("history", [])
            include_pdf = body.get("include_pdf", False)

            if not user_message:
                _json_response(self, {"error": "No message provided"}, 400)
                return

            _candidate_name = _config["candidate"]["name"]
            _experience_contexts = ", ".join(
                entry.get("context", key)
                for key, entry in _config["experience"].items()
            )
            system_parts = [
                "You are a resume review assistant for a software "
                f"engineer named {_candidate_name}.",
                f"{_candidate_name} has experience at: "
                f"{_experience_contexts}.",
                f"Their skills include: {_CANDIDATE_SKILLS}",
                "",
                "YOU HAVE ACCESS TO THREE KEY DOCUMENTS:",
                "1. FULL BULLET POOL — every resume bullet the "
                "candidate has available across all past employers",
                "2. TARGET JOB POSTING — the job description they "
                "are applying to",
                "3. CURRENT TAILORED RESUME — the resume that was "
                "auto-generated for this specific job by selecting "
                "and ordering bullets based on keyword matching",
                "",
                "CRITICAL CONSTRAINT — BULLET OWNERSHIP:",
                "Each bullet belongs to a SPECIFIC company section "
                "(Compass, Mastercard, JP Morgan, or Leidos). "
                "Bullets CANNOT be moved between companies. A "
                "JP Morgan bullet can ONLY replace another JP Morgan "
                "bullet. A Compass bullet can ONLY replace another "
                "Compass bullet. Never suggest swapping a bullet "
                "from one company into a different company's section.",
                "",
                "YOUR ROLE:",
                "- Compare the TAILORED RESUME against the JOB "
                "POSTING and identify gaps, missing keywords, and "
                "weak alignment",
                "- Suggest SPECIFIC bullet replacements or rewordings "
                "by pulling from the FULL BULLET POOL, respecting "
                "the company ownership constraint above",
                "- When suggesting changes, QUOTE the current bullet "
                "and show the improved version side by side, and "
                "name which company section it belongs to",
                "- Rate resume-to-job fit using concrete evidence "
                "from both documents",
                "- For interview prep, base questions on the specific "
                "technologies in the job posting and the candidate's "
                "actual experience",
                "- Be direct and actionable — no generic advice",
                "",
                f"\n--- {_candidate_name.upper()}'S FULL EXPERIENCE "
                "BULLET POOL ---",
                "These are ALL available resume bullets the candidate "
                "can draw from:",
                _format_bullet_pool(),
            ]

            if job_id:
                job_context, resume_text = _build_job_context(job_id)
                if job_context:
                    system_parts.append(
                        "\n--- TARGET JOB POSTING ---\n" + job_context
                    )
                if resume_text:
                    system_parts.append(
                        "\n--- CURRENT TAILORED RESUME FOR THIS JOB ---"
                    )
                    system_parts.append(
                        "This resume was auto-generated for this job. "
                        "Review it critically and suggest improvements:"
                    )
                    system_parts.append(resume_text)

                if include_pdf:
                    job = load_job(job_id)
                    pdf_path = None
                    if job:
                        state = load_state()
                        pdf_path = state.get(job_id, {}).get("pdf_path")
                    if pdf_path:
                        pdf_text = _extract_pdf_text(pdf_path)
                        if pdf_text:
                            system_parts.append(
                                "\n--- UPLOADED PDF RESUME ---"
                            )
                            system_parts.append(pdf_text)

            system_prompt = "\n".join(system_parts)
            claude_messages = list(history)
            claude_messages.append(
                {"role": "user", "content": user_message}
            )

            try:
                response = _call_claude(system_prompt, claude_messages)
                _json_response(self, {"response": response})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/run-live-check ─────────────────────
        if self.path == "/api/run-live-check":
            if _live_check_status["running"]:
                _json_response(self, {"ok": True, "message": "Already running"})
            else:
                batch_filter = None
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    try:
                        batch_filter = json.loads(self.rfile.read(length)).get("batch")
                    except Exception:
                        pass
                threading.Thread(target=_run_live_check, args=(batch_filter,), daemon=True).start()
                _json_response(self, {"ok": True, "message": "Started"})
            return

        # ── POST /api/run-update-applicants ─────────────
        if self.path == "/api/run-update-applicants":
            if _update_status["running"]:
                _json_response(self, {"ok": True, "message": "Already running"})
            else:
                threading.Thread(target=_run_applicant_update, daemon=True).start()
                _json_response(self, {"ok": True, "message": "Started"})
            return

        # ── POST /api/update-applicants ──────────────────
        if self.path == "/api/update-applicants":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                updates = data.get("updates", [])
                count = 0
                for u in updates:
                    job_id = u.get("id")
                    applicants = u.get("applicants")
                    if job_id and applicants is not None:
                        save_job_state(job_id, {"applicants": applicants})
                        count += 1
                _json_response(self, {"ok": True, "updated": count})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
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
        try:
            if args and "/api/" in str(args[0]):
                super().log_message(format, *args)
        except Exception:
            pass


if __name__ == "__main__":
    print(f"Starting Job Dashboard server on port {PORT}...")
    print(f"Open http://localhost:{PORT} in your browser")
    print("Press Ctrl+C to stop\n")
    server = http.server.HTTPServer(("", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()
