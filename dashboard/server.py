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

# Per-import state keyed by batch UUID. Replaces the old singleton.
# Each value has the same keys as the old _ai_import_status plus
# 'id', 'status', 'mode', 'import_date', 'location'.
_ai_imports_lock = threading.RLock()
_ai_imports: dict = {}                    # batch_uuid -> status dict
_ai_import_stop_events: dict = {}         # batch_uuid -> threading.Event
_ai_live_import_running = False           # single-runner guard for live mode


_live_check_lock = threading.Lock()
_live_check_status = {"running": False, "progress": 0, "total": 0, "closed": 0, "live": 0, "inconclusive": 0, "auto_updated": 0, "message": ""}

# Company info cache (in-memory, warmed from DB at startup)
_company_cache = {}
_company_cache_lock = threading.Lock()


# ─────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────

# ── import_batches helpers ─────────────────────────────────

def _build_batch_label(mode, location, short_id):
    """Build a unique import_date label for a new import batch."""
    import datetime
    base = datetime.date.today().isoformat()
    suffix = f" \u2014 {location} (AI{' batch' if mode == 'batch' else ''})" if location \
             else f" \u2014 AI{' batch' if mode == 'batch' else ''}"
    return f"{base}{suffix} #{short_id}"


def _new_import_row(mode, location, csv_filename=None):
    """Insert a fresh import_batches row; return (batch_uuid, import_date label)."""
    batch_uuid = str(uuid.uuid4())
    short_id = batch_uuid.replace("-", "")[:6]
    label = _build_batch_label(mode, location, short_id)
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO import_batches
                    (id, import_date, mode, status, location, csv_filename, message)
                VALUES (%s, %s, %s, 'queued', %s, %s, 'Queued')
            """, (batch_uuid, label, mode, location, csv_filename))
    except Exception as e:
        print(f"Warning: could not insert import_batches row: {e}")
    return batch_uuid, label


_IMPORT_BATCH_COLS = {
    "status", "anthropic_batch_id", "batch_processing_status",
    "total", "progress", "added", "scored_ai", "scored_fallback",
    "regex_agree", "ai_promoted", "ai_demoted", "estimated_cost",
    "request_counts", "pending_jobs", "message", "last_error",
    "stopped", "finished_at", "started_at",
}


def _update_import_row(batch_uuid, **fields):
    """Patch-update an import_batches row with whitelisted fields."""
    kv = {k: v for k, v in fields.items() if k in _IMPORT_BATCH_COLS}
    if not kv:
        return
    sets = []
    vals = []
    for k, v in kv.items():
        sets.append(f"{k} = %s")
        if k in ("request_counts", "pending_jobs") and v is not None and not isinstance(v, str):
            vals.append(json.dumps(v))
        else:
            vals.append(v)
    vals.append(batch_uuid)
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE import_batches SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s",
                vals,
            )
    except Exception as e:
        print(f"Warning: _update_import_row failed for {batch_uuid}: {e}")


def _mem_update(batch_uuid, patch):
    """Atomically patch the in-memory import status dict."""
    with _ai_imports_lock:
        s = _ai_imports.get(batch_uuid)
        if s is not None:
            s.update(patch)


def _flush_import_progress(batch_uuid):
    """Copy current in-memory state to the DB row (all fields at once)."""
    with _ai_imports_lock:
        s = dict(_ai_imports.get(batch_uuid) or {})
    if not s:
        return
    _update_import_row(
        batch_uuid,
        status=s.get("status", "running"),
        anthropic_batch_id=s.get("batch_id"),
        batch_processing_status=s.get("batch_processing_status"),
        total=s.get("total") or 0,
        progress=s.get("progress") or 0,
        added=s.get("added") or 0,
        scored_ai=s.get("scored_ai") or 0,
        scored_fallback=s.get("scored_fallback") or 0,
        regex_agree=s.get("regex_agree") or 0,
        ai_promoted=s.get("ai_promoted") or 0,
        ai_demoted=s.get("ai_demoted") or 0,
        estimated_cost=s.get("estimated_cost") or 0,
        request_counts=s.get("batch_request_counts"),
        message=s.get("message"),
        last_error=s.get("last_error"),
        stopped=bool(s.get("stopped")),
    )


def _import_row_to_dict(row, desc):
    """Convert a DB cursor row to a serialisable dict.

    Merges fresher in-memory state for any import that is currently
    tracked in _ai_imports (i.e. still active in this process).
    """
    from decimal import Decimal
    keys = [c[0] for c in desc]
    d = {}
    for k, v in zip(keys, row):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            # NUMERIC columns (e.g. estimated_cost) come back as
            # Decimal which json.dumps can't serialize. Cast to float
            # so SSE frames and JSON responses don't blow up.
            d[k] = float(v)
        else:
            d[k] = v
    d["id"] = str(d.get("id", ""))
    # Merge fresher in-memory progress fields
    with _ai_imports_lock:
        mem = dict(_ai_imports.get(d["id"]) or {})
    if mem:
        for dst, src in [
            ("status", "status"), ("progress", "progress"), ("total", "total"),
            ("added", "added"), ("scored_ai", "scored_ai"),
            ("scored_fallback", "scored_fallback"),
            ("estimated_cost", "estimated_cost"),
            ("message", "message"), ("last_error", "last_error"),
            ("batch_processing_status", "batch_processing_status"),
        ]:
            val = mem.get(src)
            if val is not None:
                d[dst] = val
    return d


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
                       job_link, apply_link, resume_text,
                       ai_reasoning, ai_resume_generated_at, ai_resume_status
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


# ─────────────────────────────────────────────────────────
# AI RESUME BULLET GENERATION (Claude Sonnet — Phase 2)
# ─────────────────────────────────────────────────────────

AI_RESUME_MODEL = "claude-sonnet-4-20250514"
# NOTE: Phase 2 PRD targets "Sonnet 4.6" (claude-sonnet-4-6). When the
# 4.6 model ID is available in the Anthropic API, swap this constant
# (and ideally the existing _call_claude default at the top of this
# file) to the new ID. Until then we use the same Sonnet 4 model as
# the existing /api/ai-chat endpoint to guarantee a valid model ID.
# Sonnet: $3/MTok input, $15/MTok output. Batch API gives 50% off.
# Per-job token budget (amortized with prompt caching):
#   ~3,500 input (cached system ~2,000 + user ~1,500)
#   ~800 output
# Live: ~$0.022/job. Batch: ~$0.011/job.
AI_RESUME_COST_PER_JOB_LIVE = 0.022
AI_RESUME_COST_PER_JOB_BATCH = 0.011

# Per-company rendered-line budgets. Mastercard is a hard equality.
# Total must land in [AI_RESUME_TOTAL_LINES_MIN, AI_RESUME_TOTAL_LINES_MAX].
AI_RESUME_LINE_BUDGET = {
    "Compass": (9, 10),
    "Mastercard": (5, 5),   # fixed
    "JP Morgan": (9, 10),
    "Leidos": (6, 7),
}
AI_RESUME_TOTAL_LINES_MIN = 27
AI_RESUME_TOTAL_LINES_MAX = 31

# Character thresholds for rendered-line counting. Derived from
# measured examples in the target resume template:
#   131-char bullet renders as 1 line, 139-char renders as 2 lines
#   longest observed 2-line bullet ≈ 253 chars
# 135 (was 130) gives Sonnet a 4-char safety margin so near-misses
# (e.g. 134-char bullets) don't tip a single-line into a 2-liner.
AI_RESUME_ONE_LINE_MAX_CHARS = 135
AI_RESUME_TWO_LINE_MAX_CHARS = 265

# In-memory state for the AI resume batch runner + poller. Mirrors
# the _ai_imports / _ai_import_stop_events pattern used by Phase 1
# CSV batch imports.
_ai_resume_batches_state = {}
_ai_resume_batches_lock = threading.Lock()
_ai_resume_batch_stop_events = {}


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
    yoe = _config["candidate"].get("years_of_experience", 5)
    top_cos = ", ".join(_config.get("scoring", {}).get("top_companies", []))
    return "\n".join([
        f"You are a job-fit scoring assistant evaluating how well {name} "
        f"(a software engineer with {yoe} years of professional "
        "experience) matches each job posting.",
        "",
        f"CANDIDATE SKILLS: {_CANDIDATE_SKILLS}",
        "",
        "EXPERIENCE BULLETS (organized by company):",
        _format_bullet_pool(),
        "",
        "SCORING CRITERIA (1-10 integer scale):",
        "  9-10: Near-perfect stack overlap and domain alignment with the",
        "        candidate's demonstrated experience",
        "  7-8:  Strong overlap, minor gaps or one unfamiliar technology",
        "  5-6:  Partial overlap, would need to learn some technologies",
        "  3-4:  Weak overlap, but transferable skills exist",
        "  1-2:  Wrong domain or requires expertise candidate lacks",
        "",
        "POSITIVE SIGNALS (raise score):",
        "  CORE (strongest match signals):",
        "  - Go or gRPC as a primary backend language",
        "  - Java with Spring Boot or Kafka",
        "  - Backend platform engineering, event-driven systems,",
        "    distributed systems, or microservices architecture",
        "  SECONDARY (meaningful match signals):",
        "  - AWS, especially EMR, EKS, Lambda, Aurora, or RDS",
        "  - Kubernetes or Docker in a backend or infra context",
        "  - Auth/security engineering: OAuth2, OIDC, ReBAC,",
        "    certificate-based auth, access control, zero-trust",
        "  - Observability: Datadog, Chronosphere, Prometheus, Zipkin",
        "  - PostgreSQL or SQL in a backend service context",
        "  - REST API design or gRPC service development",
        "  - TypeScript/React (mild positive when backend is also present;",
        "    neutral if the role is exclusively frontend)",
        "  AMPLIFIER (boost when present; no penalty when absent):",
        f"  - Employer is a top-tier tech company ({top_cos})",
        "  - Posting explicitly limits eligibility to US Persons or US",
        "    Citizens only — candidate is a US citizen, a real advantage;",
        "    boost score by 1",
        "  - Active security clearance is a strong plus (not required) AND",
        "    overall stack alignment is already strong",
        "",
        "NEGATIVE SIGNALS (reduce score):",
        "  - Entry-level, new-grad, intern, SE I, Junior, or Associate",
        "    roles — candidate is overqualified; cap these at score 5",
        "  - SE II title — reduce score by 1-2 points (still worth",
        "    surfacing if the stack is a strong match)",
        "  - Staffing agency or recruiter posting (not the direct employer)",
        "  - Exclusively frontend role (React/Angular/Vue only, no backend",
        "    component) — candidate's strength is backend engineering",
        "  - Requires an active security clearance candidate does not hold",
        "    (clearance actively required, not merely preferred)",
        "  - Primary language is one candidate doesn't know",
        "    (e.g. Rust, Scala, .NET/C# as primary, mobile Swift/Kotlin)",
        "  - Deep domain expertise required in healthcare EMR systems",
        "    (Epic, Cerner) or insurance actuarial/P&C pricing models",
        "",
        "ALWAYS RETURN A SCORE. If the job description is short, sparse, "
        "vague, or missing technical details, DO NOT refuse, DO NOT ask "
        "for more information, and DO NOT return natural-language text. "
        "Instead, use fit_score = 3 and state in the reasoning that the "
        "posting lacked sufficient detail to assess fit confidently. "
        "Score based on whatever signal is available (company name, title "
        "level, any technology keywords, etc.). You must return the JSON "
        "object below no matter what.",
        "",
        "Return ONLY a JSON object with this exact shape (no markdown "
        "fences, no explanation outside the JSON):",
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
            preview = (raw or "").strip().replace("\n", " ")[:160]
            return {
                "used_ai": False,
                "error": (
                    "Sonnet returned non-JSON (likely refused to score "
                    "a sparse description): " + (preview or "empty")
                ),
            }

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
                    # Sonnet returned natural language instead of JSON.
                    # Happens when the job description is too sparse and
                    # the model refuses to guess. Include a preview of
                    # what it said so the fallback reason is obvious.
                    preview = (raw or "").strip().replace("\n", " ")[:160]
                    yield custom_id, {
                        "used_ai": False,
                        "error": (
                            "Sonnet returned non-JSON (likely refused "
                            "to score a sparse description): "
                            + (preview or "empty response")
                        ),
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
# AI RESUME BULLET GENERATION — helpers (Phase 2)
# ─────────────────────────────────────────────────────────

# Canonical company order and list used by the Sonnet prompt and
# validator. This is the SAME order that generate_resume_txt() uses
# when emitting sections, which is controlled by config.json's
# experience dict insertion order.
def _ai_resume_company_order():
    """Return the list of company display_names in the order they
    appear in the bullet pool (matches generate_resume_txt output)."""
    return list(_BULLET_POOL.keys())


def _count_lines(bullet_text):
    """Return the rendered-line count for a single bullet.

    1 line if <= AI_RESUME_ONE_LINE_MAX_CHARS
    2 lines if <= AI_RESUME_TWO_LINE_MAX_CHARS
    3 (forbidden) otherwise — callers treat this as a validation failure
    """
    n = len((bullet_text or "").strip())
    if n <= AI_RESUME_ONE_LINE_MAX_CHARS:
        return 1
    if n <= AI_RESUME_TWO_LINE_MAX_CHARS:
        return 2
    return 3


def _build_ai_resume_system_prompt():
    """Build the Sonnet system prompt for tailored bullet generation.

    Reads _BULLET_POOL and _CANDIDATE_SKILLS at call time so Profile
    page edits are picked up without a server restart. Returns a
    string that will be sent with cache_control: ephemeral so the
    ~2000 token prompt is cached across calls.
    """
    name = _config["candidate"].get("name", "the candidate")
    yoe = _config["candidate"].get("years_of_experience", 5)

    # Format the full bullet pool so Sonnet sees every candidate
    # source bullet, numbered per company for clarity in its
    # reasoning.
    pool_lines = []
    for company in _ai_resume_company_order():
        pool_lines.append("")
        pool_lines.append(f"{company}:")
        for i, b in enumerate(_BULLET_POOL[company], 1):
            pool_lines.append(f"  {i}. {b}")
    pool_block = "\n".join(pool_lines).strip("\n")

    # Per-company budgets, with concrete bullet-count targets so the
    # model has an unambiguous goal. The math is: target ~3 single-line
    # bullets + a couple of 2-line bullets per company. Mastercard is
    # the strict case (exactly 5 lines = exactly 5 single-line bullets).
    budget_lines = []
    target_lines = []
    for company in _ai_resume_company_order():
        lo, hi = AI_RESUME_LINE_BUDGET.get(company, (0, 0))
        if lo == hi:
            budget_lines.append(
                f"   - {company}: exactly {lo} rendered lines"
            )
            target_lines.append(
                f"   - {company}: write EXACTLY {lo} bullets, "
                f"each <= {AI_RESUME_ONE_LINE_MAX_CHARS} characters "
                f"(single-line). No 2-line bullets."
            )
        else:
            budget_lines.append(
                f"   - {company}: {lo}-{hi} rendered lines"
            )
            target_lines.append(
                f"   - {company}: write 4-6 bullets totaling {lo}-{hi} "
                f"rendered lines. Aim for ~3 single-line bullets "
                f"(<= {AI_RESUME_ONE_LINE_MAX_CHARS} chars) plus "
                f"~2 two-line bullets ({AI_RESUME_ONE_LINE_MAX_CHARS + 1}-"
                f"{AI_RESUME_TWO_LINE_MAX_CHARS} chars). No bullet may "
                f"exceed {AI_RESUME_TWO_LINE_MAX_CHARS} characters."
            )
    budget_block = "\n".join(budget_lines)
    target_block = "\n".join(target_lines)

    return "\n".join([
        f"You are a resume tailoring assistant for {name}, a software "
        f"engineer with {yoe} years of experience. Your job is to choose "
        "and tailor the candidate's existing bullets so the resume "
        "highlights the work most relevant to the specific job posting "
        "you are given. You are NOT a creative writer — you are an "
        "editor who rearranges and refocuses TRUE statements.",
        "",
        "TWO RULES ABOVE ALL OTHERS:",
        "  (a) Truth: NEVER invent or embellish. Every fact in your "
        "      output must come from the candidate's pool, unchanged "
        "      in substance. If the source doesn't say it, you can't "
        "      say it.",
        "  (b) Relevance: when a pool bullet has clauses or sub-phrases "
        "      that aren't relevant to this job, drop or de-emphasize "
        "      them. When the job uses different vocabulary for the "
        "      same concept the source describes, you may use the "
        "      job's vocabulary as long as the meaning is identical.",
        "",
        "YOUR PROCESS — follow this every time:",
        "",
        "Step 1 (ANALYZE THE JOB). Read the job description carefully. "
        "Identify the 5-8 most important things this employer cares "
        "about — specific technologies, domain words, scale signals, "
        "team words, and any unusual requirements they call out. These "
        "are your tailoring targets.",
        "",
        "Step 2 (SELECT relevant pool bullets). For each company, find "
        "the source bullets that overlap with the job's targets. A "
        "bullet is \"relevant\" if it mentions even ONE of the "
        "technologies, domains, or skills the job cares about. Skip "
        "bullets that don't fit the job at all.",
        "",
        "Step 3 (DECIDE: keep, edit, or merge). For each selected "
        "bullet, ask: \"Is this already a perfect fit for the job as-is?\"",
        "  - If YES: keep it verbatim. Do not change a bullet just to "
        "    look like you did something. A great untouched bullet is "
        "    always better than a tampered one.",
        "  - If NO, but a small edit would make it more relevant: "
        "    drop irrelevant sub-phrases, reorder clauses so the "
        "    job-relevant part comes first, or swap vocabulary for the "
        "    job's exact wording (only when it means the SAME thing).",
        "  - If two pool bullets describe the SAME project or "
        "    initiative at the SAME company AND the merge would be "
        "    more relevant for this job: merge them. Different projects "
        "    must NEVER be merged.",
        "",
        "Step 4 (BUDGET FIT — see hard rules below). Check that your "
        "bullets fit the per-company line budget. Tighten or expand "
        "wording to hit the budget. NEVER add a new fact just to fill "
        "space; if you don't have enough relevant pool material, "
        "include a less-relevant pool bullet instead of inventing.",
        "",
        "WORKED EXAMPLE 1 — small edit improves relevance:",
        "",
        "  Source (Compass pool):",
        "  \"Built and maintained Go gRPC backend services and React "
        "frontend components for template publishing, brand-aware "
        "search, and multi-org access control on a platform serving "
        "thousands of real estate agents\"",
        "",
        "  Job cares about: backend distributed systems, multi-tenant "
        "access control. Job is backend-only.",
        "",
        "  Acceptable edited output (drops React and template "
        "publishing as off-topic, leads with \"multi-tenant access "
        "control\"; every remaining fact is in the source):",
        "  \"Built and maintained multi-tenant access control on Go "
        "gRPC backend services serving thousands of real estate "
        "agents\"",
        "",
        "  FORBIDDEN output (invented \"high-throughput\" and "
        "\"99.9% availability\" — neither is in the source):",
        "  \"Built and maintained high-throughput Go gRPC backend "
        "services with 99.9% availability and multi-tenant access "
        "control...\"",
        "",
        "WORKED EXAMPLE 2 — keep verbatim because it's already perfect:",
        "",
        "  Source (JP Morgan pool):",
        "  \"Configured distributed tracing with Zipkin and monitoring "
        "with Prometheus to identify and resolve latency bottlenecks "
        "in microservice architectures\"",
        "",
        "  Job cares about: observability, distributed systems, "
        "Prometheus, performance tuning.",
        "",
        "  Acceptable output (verbatim — this bullet already hits "
        "exactly what the job wants; rewriting it would not improve "
        "anything):",
        "  \"Configured distributed tracing with Zipkin and monitoring "
        "with Prometheus to identify and resolve latency bottlenecks "
        "in microservice architectures\"",
        "",
        f"CANDIDATE SKILLS: {_CANDIDATE_SKILLS}",
        "",
        "EXPERIENCE BULLETS (the \"pool\" — your only source of facts; "
        "every output bullet must trace back here):",
        "",
        pool_block,
        "",
        "═══════════════════════════════════════════════════════════════",
        "HARD CONSTRAINTS — violations cause your response to be "
        "rejected and the entire job to fail. There is NO partial credit.",
        "═══════════════════════════════════════════════════════════════",
        "",
        "1. LINE BUDGET (hard):",
        budget_block,
        f"   - Total: {AI_RESUME_TOTAL_LINES_MIN}-{AI_RESUME_TOTAL_LINES_MAX} "
        "rendered lines across all four companies (no exceptions)",
        "",
        "   How rendered lines are counted (this is the validator's "
        "EXACT rule — count characters yourself the same way):",
        f"   - bullet length <= {AI_RESUME_ONE_LINE_MAX_CHARS} chars "
        "  ==> 1 rendered line",
        f"   - bullet length {AI_RESUME_ONE_LINE_MAX_CHARS + 1}-"
        f"{AI_RESUME_TWO_LINE_MAX_CHARS} chars ==> 2 rendered lines",
        f"   - bullet length > {AI_RESUME_TWO_LINE_MAX_CHARS} chars "
        "  ==> FORBIDDEN, the response is rejected",
        "",
        "1a. CONCRETE BUDGET TARGETS — follow these exactly:",
        target_block,
        "",
        "2. COMPANY LOCK: Every bullet you produce for a company must "
        "describe work done at THAT company. Never attribute work from "
        "one company to another. Your ONLY source for each company's "
        "bullets is that company's section of the pool above. (You can "
        "and should rewrite the language — but the underlying facts "
        "must come from that same company's pool.)",
        "",
        "3. GROUND-TRUTH FACTS: Rewrite freely, BUT every factual claim "
        "in your output — every technology, metric, scope, outcome, "
        "team size, timeline — must be traceable to a source bullet "
        "from the SAME company. You can rephrase 'thousands of real "
        "estate agents' as 'thousands of users on a high-scale "
        "platform' (same fact, different framing). You CANNOT invent a "
        "number, tool, or outcome that does not appear in the pool. "
        "If the source says 'reduced latency' without a percentage, "
        "you cannot write '40% latency reduction'.",
        "",
        "4. MERGE RULE: You may merge two bullets into one ONLY if they "
        "describe the SAME project or initiative in the pool. Different "
        "projects must stay separate — combining them fabricates "
        "relationships that did not exist.",
        "",
        "5. STYLE: Match the voice of the existing pool bullets. Use "
        "strong active verbs, technical specificity, and quantitative "
        "outcomes only where the source bullets provide them.",
        "",
        "6. OUTPUT STRUCTURE: Your bullets object must contain exactly "
        "these four keys in this order: "
        + ", ".join(f'"{c}"' for c in _ai_resume_company_order()) + ".",
        "",
        "MANDATORY SELF-CHECK before responding:",
        "",
        "Quality check Q1 — TRUTH. For EVERY bullet you wrote, walk "
        "through each technology, metric, scope, outcome, team size, "
        "and timeline word by word. Confirm each one appears in the "
        "SAME-company source bullets. If you can't trace a fact to a "
        "source bullet, REMOVE it. This is your single most important "
        "check — a fabricated detail is worse than a generic bullet.",
        "",
        "Quality check Q2 — RELEVANCE. For each bullet, ask: \"Would "
        "this employer find this useful?\" If a bullet is irrelevant to "
        "the job, swap it for a more relevant pool bullet (or its "
        "tailored edit). If a bullet is relevant but leads with the "
        "wrong clause, reorder it. If it's relevant AND already leads "
        "well — leave it alone, even if you didn't change it.",
        "",
        "Mechanical check M1 — CHARACTER COUNT. For EACH bullet you "
        "wrote, count its character length. If any bullet exceeds the "
        "per-company maximum from section 1a, rewrite or replace it. "
        f"Aim for ~{AI_RESUME_ONE_LINE_MAX_CHARS - 5} chars max for "
        f"single-line bullets to give yourself a safety margin.",
        "",
        "Mechanical check M2 — PER-COMPANY LINE SUM. For EACH company, "
        "sum up the rendered lines using the rule in section 1. "
        "Compare against that company's budget. If you are over or "
        "under budget, adjust bullet count or bullet lengths.",
        "",
        "Mechanical check M3 — TOTAL LINE SUM. Sum the rendered lines "
        "across all four companies. It MUST be in "
        f"[{AI_RESUME_TOTAL_LINES_MIN}, {AI_RESUME_TOTAL_LINES_MAX}].",
        "",
        "Only after every check passes, return your JSON response.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "RESPONSE FORMAT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "You must return a JSON object with TWO top-level fields: "
        "`analysis` (your self-explanation) and `bullets` (the actual "
        "tailored resume content). The analysis is what makes this "
        "auditable — be honest and specific.",
        "",
        "The `analysis` object has three sub-fields:",
        "  - `job_focus`: 1-2 sentences naming what this employer "
        "    cares about most (the tailoring targets you identified "
        "    in Step 1).",
        "  - `per_company`: an object with one entry per company "
        "    (Compass, Mastercard, JP Morgan, Leidos). Each entry is "
        "    1-3 sentences explaining what you kept verbatim, what "
        "    you edited and why, and what you merged. Be concrete — "
        "    name the bullets you touched.",
        "  - `dropped`: an object with one entry per company. Each "
        "    entry is a list of strings; each string names a pool "
        "    bullet you DID NOT include (use a short identifier like "
        "    'bullet #2: M&A REST APIs') and gives a one-phrase "
        "    reason (e.g., 'job is backend-only, frontend not "
        "    relevant'). If you included every pool bullet for a "
        "    company, use an empty list [].",
        "",
        "Return ONLY a JSON object in this exact shape, with no markdown "
        "and no text outside the JSON:",
        "",
        "{",
        '  "analysis": {',
        '    "job_focus": "1-2 sentences on what the job emphasizes",',
        '    "per_company": {',
        ",\n".join(
            f'      "{c}": "1-3 sentences on kept/edited/merged bullets"'
            for c in _ai_resume_company_order()
        ),
        "    },",
        '    "dropped": {',
        ",\n".join(
            f'      "{c}": ["bullet #X: short label — reason"]'
            for c in _ai_resume_company_order()
        ),
        "    }",
        "  },",
        '  "bullets": {',
        ",\n".join(
            f'    "{c}": ["bullet 1", "bullet 2", "..."]'
            for c in _ai_resume_company_order()
        ),
        "  }",
        "}",
    ])


def _build_ai_resume_user_message(job):
    """Per-job user message. Mirrors _build_batch_request (line ~1210)
    but adds a brief instruction to respect the budget for this job."""
    relevant_desc = _extract_relevant_description(
        job.get("description", ""), max_chars=2500
    )
    return (
        "Tailor a set of resume bullets for this specific job posting. "
        "Respect every hard constraint from the system prompt.\n\n"
        f"Company: {job.get('company', '')}\n"
        f"Title: {job.get('title', '')}\n\n"
        f"DESCRIPTION (relevant excerpt):\n{relevant_desc}"
    )


def _build_ai_resume_batch_request(job_id, system_prompt, job):
    """Build one entry of an Anthropic Messages Batches request for
    the Sonnet bullet generator. Same structure as _build_batch_request
    but uses AI_RESUME_MODEL and the resume-specific system prompt.
    """
    return {
        "custom_id": job_id,
        "params": {
            "model": AI_RESUME_MODEL,
            "max_tokens": 2048,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{
                "role": "user",
                "content": _build_ai_resume_user_message(job),
            }],
        },
    }


def _parse_ai_resume_response(raw_text):
    """Parse Sonnet's JSON response. Returns one of:

        {"ok": True,  "bullets": {...}, "analysis": {...}|None}
        {"ok": False, "error": "..."}

    Tolerates leading/trailing whitespace, code fences, and a top-level
    wrapping object — the only thing it insists on is finding a "bullets"
    key whose value is a dict of lists-of-strings. The "analysis" key
    is optional (older responses or tolerant fallbacks may omit it).
    """
    if not raw_text:
        return {"ok": False, "error": "empty response"}
    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"ok": False, "error": "no JSON object in response"}
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        return {"ok": False, "error": f"JSON parse error: {e}"}
    bullets = data.get("bullets") if isinstance(data, dict) else None
    if not isinstance(bullets, dict):
        return {"ok": False, "error": "missing 'bullets' object"}
    # Normalize per-company values to stripped-string lists
    out = {}
    for company, value in bullets.items():
        if not isinstance(value, list):
            return {"ok": False,
                    "error": f"'{company}' value is not a list"}
        clean = []
        for item in value:
            if not isinstance(item, str):
                return {"ok": False,
                        "error": f"'{company}' contains a non-string bullet"}
            s = item.strip()
            if s:
                clean.append(s)
        out[company] = clean

    # Best-effort extraction of the analysis block. Missing/malformed
    # analysis is NOT a hard error — we still return the bullets and
    # let downstream code render a "no explanation captured" notice.
    analysis = None
    raw_analysis = data.get("analysis") if isinstance(data, dict) else None
    if isinstance(raw_analysis, dict):
        job_focus = raw_analysis.get("job_focus")
        per_company = raw_analysis.get("per_company")
        dropped = raw_analysis.get("dropped")
        analysis = {
            "job_focus": (job_focus.strip()
                          if isinstance(job_focus, str) else None),
            "per_company": (per_company
                            if isinstance(per_company, dict) else {}),
            "dropped": (dropped
                        if isinstance(dropped, dict) else {}),
        }

    return {"ok": True, "bullets": out, "analysis": analysis}


def _validate_ai_resume_bullets(bullets):
    """Validate the parsed bullets dict against the hard constraints.

    Returns (ok: bool, errors: list[str]). On failure the runner will
    trigger the validation-retry path (one more live Sonnet call).
    """
    errors = []
    expected_companies = _ai_resume_company_order()

    # Shape: every expected company key must be present
    for c in expected_companies:
        if c not in bullets:
            errors.append(f"missing company key: {c!r}")
    extra = [k for k in bullets.keys() if k not in expected_companies]
    if extra:
        errors.append(f"unexpected company keys: {extra}")

    if errors:
        return False, errors

    # Per-bullet char length and per-company line budget
    total_lines = 0
    for c in expected_companies:
        company_bullets = bullets.get(c, [])
        if not company_bullets:
            errors.append(f"{c}: no bullets returned")
            continue
        company_lines = 0
        for idx, b in enumerate(company_bullets, 1):
            lines = _count_lines(b)
            if lines >= 3:
                errors.append(
                    f"{c} bullet #{idx}: {len(b)} chars exceeds "
                    f"{AI_RESUME_TWO_LINE_MAX_CHARS} char maximum"
                )
            company_lines += lines
        lo, hi = AI_RESUME_LINE_BUDGET.get(c, (0, 999))
        if company_lines < lo or company_lines > hi:
            errors.append(
                f"{c}: {company_lines} lines outside budget [{lo}, {hi}]"
            )
        total_lines += company_lines

    if (total_lines < AI_RESUME_TOTAL_LINES_MIN
            or total_lines > AI_RESUME_TOTAL_LINES_MAX):
        errors.append(
            f"total lines = {total_lines}, must be in "
            f"[{AI_RESUME_TOTAL_LINES_MIN}, "
            f"{AI_RESUME_TOTAL_LINES_MAX}]"
        )

    return (not errors), errors


def _find_on_disk_resume_path(job):
    """Return the absolute path of an existing on-disk resume.txt for
    this job, or None if no file is found. Mirrors the naming logic in
    process_new_postings.generate_resume_files()."""
    try:
        from process_new_postings import sanitize_dirname, RESUMES_DIR
    except Exception:
        return None
    try:
        company_dir = sanitize_dirname(job.get("company", ""))
        title_dir = sanitize_dirname(job.get("title", ""))
    except Exception:
        return None
    if not company_dir or not title_dir:
        return None
    base = os.path.join(RESUMES_DIR, company_dir, title_dir)
    candidates = [
        os.path.join(base, "resume.txt"),
        os.path.join(base + "_" + str(job.get("id", "")), "resume.txt"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _apply_ai_resume_to_job(job_id, bullets, analysis=None):
    """Render Sonnet's bullets dict into the final text resume and
    persist it. Overwrites jobs.resume_text and any on-disk resume.txt,
    sets ai_resume_status='ready' and ai_resume_generated_at=NOW().
    Also persists Sonnet's self-explanation block (analysis) to
    ai_resume_reasoning so the user can audit what changed and why.

    Returns (ok: bool, message: str). Raises on hard DB errors.
    """
    from process_new_postings import generate_resume_txt, customize_skills

    with Db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, company, title, location, work_type, salary, "
            "posted_date, import_date, description, score, tier, "
            "job_link, apply_link FROM jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return False, f"job {job_id} not found"
        (jid, company, title, location, work_type, salary,
         posted_date, import_date, description, score, tier,
         job_link, apply_link) = row
        job = {
            "id": jid, "company": company, "title": title,
            "location": location, "work_type": work_type, "salary": salary,
            "posted_date": posted_date, "import_date": import_date,
            "description": description or "", "score": score,
            "tier": tier, "job_link": job_link, "apply_link": apply_link,
        }

        langs, frameworks, misc = customize_skills(job["description"])
        resume_text = generate_resume_txt(
            job, bullets, langs, frameworks, misc
        )

        reasoning_json = json.dumps(analysis) if analysis else None
        cur.execute(
            "UPDATE jobs SET resume_text = %s, "
            "ai_resume_generated_at = NOW(), "
            "ai_resume_status = 'ready', "
            "ai_resume_last_raw = NULL, "
            "ai_resume_last_errors = NULL, "
            "ai_resume_reasoning = %s "
            "WHERE id = %s",
            (resume_text, reasoning_json, job_id),
        )

    # Overwrite the on-disk resume.txt if it exists so the file and
    # the DB stay in sync. If no file exists, the DB row is the
    # source of truth and /api/resume-text/<id> will serve the new
    # text anyway.
    disk_path = _find_on_disk_resume_path(job)
    if disk_path:
        try:
            with open(disk_path, "w") as f:
                f.write(resume_text)
        except Exception as e:
            # Non-fatal — DB is authoritative
            print(f"_apply_ai_resume_to_job: disk write failed for "
                  f"{job_id}: {e}")

    return True, f"applied AI resume for {job_id}"


def _retrieve_ai_resume_batch_results_raw(batch_id):
    """Yield (custom_id, {ok, text, error}) for each result in the
    Sonnet bullet-generation batch. Unlike _retrieve_anthropic_batch_results
    this returns the raw text so the caller can run its own parser and
    validator (which may trigger a retry)."""
    client = _get_anthropic_client()
    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        rtype = result.result.type
        if rtype == "succeeded":
            try:
                msg = result.result.message
                raw = msg.content[0].text
                yield custom_id, {"ok": True, "text": raw}
            except Exception as e:
                yield custom_id, {"ok": False,
                                  "error": f"extract error: {e}"}
        elif rtype == "errored":
            err = getattr(result.result, "error", None)
            err_msg = str(err) if err else "unknown batch error"
            yield custom_id, {"ok": False, "error": err_msg[:240]}
        elif rtype == "expired":
            yield custom_id, {"ok": False, "error": "batch request expired"}
        elif rtype == "canceled":
            yield custom_id, {"ok": False, "error": "batch request canceled"}
        else:
            yield custom_id, {"ok": False,
                              "error": f"unknown result type: {rtype}"}


# ─────────────────────────────────────────────────────────
# AI RESUME BULLET GENERATION — DB row + in-memory state helpers
# ─────────────────────────────────────────────────────────

_AI_RESUME_BATCH_COLS = {
    "anthropic_batch_id", "status", "batch_processing_status",
    "job_ids", "pending_jobs", "request_counts",
    "total", "succeeded", "failed", "estimated_cost",
    "message", "last_error", "stopped",
    "started_at", "finished_at",
}


def _new_ai_resume_batch_row(job_ids):
    """Insert a fresh ai_resume_batches row; return the batch UUID."""
    batch_uuid = str(uuid.uuid4())
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO ai_resume_batches
                    (id, status, job_ids, total, message)
                VALUES (%s, 'queued', %s, %s, 'Queued')
            """, (batch_uuid, json.dumps(list(job_ids)), len(job_ids)))
    except Exception as e:
        print(f"Warning: could not insert ai_resume_batches row: {e}")
    return batch_uuid


def _update_ai_resume_batch_row(batch_uuid, **fields):
    """Patch-update an ai_resume_batches row with whitelisted fields.

    JSONB columns are serialised to strings; timestamp sentinel
    "NOW()" is passed through as a SQL expression."""
    kv = {k: v for k, v in fields.items() if k in _AI_RESUME_BATCH_COLS}
    if not kv:
        return
    sets = []
    vals = []
    for k, v in kv.items():
        if v == "NOW()":
            sets.append(f"{k} = NOW()")
            continue
        sets.append(f"{k} = %s")
        if k in ("request_counts", "pending_jobs", "job_ids") \
                and v is not None and not isinstance(v, str):
            vals.append(json.dumps(v))
        else:
            vals.append(v)
    vals.append(batch_uuid)
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE ai_resume_batches SET {', '.join(sets)}, "
                "updated_at = NOW() WHERE id = %s",
                vals,
            )
    except Exception as e:
        print(f"Warning: _update_ai_resume_batch_row failed for "
              f"{batch_uuid}: {e}")


def _mem_update_ai_resume(batch_uuid, patch):
    """Atomically patch the in-memory AI resume batch state."""
    with _ai_resume_batches_lock:
        s = _ai_resume_batches_state.get(batch_uuid)
        if s is not None:
            s.update(patch)


def _flush_ai_resume_progress(batch_uuid):
    """Copy the current in-memory state to the DB row."""
    with _ai_resume_batches_lock:
        s = dict(_ai_resume_batches_state.get(batch_uuid) or {})
    if not s:
        return
    _update_ai_resume_batch_row(
        batch_uuid,
        status=s.get("status", "running"),
        anthropic_batch_id=s.get("batch_id"),
        batch_processing_status=s.get("batch_processing_status"),
        total=s.get("total") or 0,
        succeeded=s.get("succeeded") or 0,
        failed=s.get("failed") or 0,
        estimated_cost=s.get("estimated_cost") or 0,
        request_counts=s.get("batch_request_counts"),
        message=s.get("message"),
        last_error=s.get("last_error"),
        stopped=bool(s.get("stopped")),
    )


def _set_jobs_ai_resume_status(job_ids, status, batch_uuid=None):
    """Bulk-update ai_resume_status for a set of jobs. Used to mark
    jobs pending when a batch is submitted and failed/ready as
    results land."""
    if not job_ids:
        return
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE jobs SET ai_resume_status = %s WHERE id = ANY(%s)",
                (status, list(job_ids)),
            )
    except Exception as e:
        print(f"_set_jobs_ai_resume_status failed: {e}")


def _save_ai_resume_failure(job_id, raw_text, errors):
    """Persist Sonnet's raw output and the validator error list for a
    failed job, and flip ai_resume_status to 'failed'. Used so the
    Failure Inspection modal can show the user what Sonnet actually
    produced and let them Edit-as-text or Retry.

    raw_text may be None (e.g., when the failure happened before
    Sonnet returned anything — batch error, parse error). errors is
    a list of strings.
    """
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE jobs SET ai_resume_status = 'failed', "
                "ai_resume_last_raw = %s, "
                "ai_resume_last_errors = %s "
                "WHERE id = %s",
                (raw_text,
                 json.dumps(list(errors)) if errors else None,
                 job_id),
            )
    except Exception as e:
        print(f"_save_ai_resume_failure failed for {job_id}: {e}")


def _job_has_pending_ai_resume(job_id):
    """Check whether a job is currently inside an in-flight AI resume
    batch. Used to block the live regenerate path."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT ai_resume_status FROM jobs WHERE id = %s",
                (job_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        print(f"_job_has_pending_ai_resume failed: {e}")
        return False
    return bool(row) and row[0] == "pending"


def _ai_resume_batch_row_to_dict(row, desc):
    """Serialize an ai_resume_batches row to a JSON-safe dict.

    Also merges fresher in-memory state from _ai_resume_batches_state
    when the batch is still running in this process."""
    from decimal import Decimal
    keys = [c[0] for c in desc]
    d = {}
    for k, v in zip(keys, row):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            # estimated_cost is NUMERIC(10,4) → Decimal → not
            # JSON-serializable. Cast to float for the wire.
            d[k] = float(v)
        else:
            d[k] = v
    d["id"] = str(d.get("id", ""))
    # Merge fresher in-memory state
    with _ai_resume_batches_lock:
        mem = _ai_resume_batches_state.get(d["id"])
        if mem:
            for field in ("status", "message", "succeeded", "failed",
                          "estimated_cost", "batch_processing_status",
                          "last_error"):
                if mem.get(field) is not None:
                    d[field] = mem[field]
            if mem.get("batch_request_counts") is not None:
                d["request_counts"] = mem["batch_request_counts"]
    return d


def _load_jobs_for_ai_resume(job_ids):
    """Load the full job dicts needed to build Sonnet batch requests.

    Returns a list in the SAME order as job_ids, skipping any IDs
    that don't exist in the DB."""
    if not job_ids:
        return []
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, company, title, location, work_type, salary, "
                "posted_date, import_date, description, score, tier, "
                "job_link, apply_link FROM jobs WHERE id = ANY(%s)",
                (list(job_ids),),
            )
            rows = cur.fetchall()
    except Exception as e:
        print(f"_load_jobs_for_ai_resume failed: {e}")
        return []
    by_id = {}
    for r in rows:
        (jid, company, title, location, work_type, salary, posted_date,
         import_date, description, score, tier, job_link, apply_link) = r
        by_id[jid] = {
            "id": jid, "company": company, "title": title,
            "location": location, "work_type": work_type,
            "salary": salary, "posted_date": posted_date,
            "import_date": import_date, "description": description or "",
            "score": score, "tier": tier,
            "job_link": job_link, "apply_link": apply_link,
        }
    # Preserve caller order
    return [by_id[j] for j in job_ids if j in by_id]


# ─────────────────────────────────────────────────────────
# AI RESUME BULLET GENERATION — batch runner, live regenerate, restart
# ─────────────────────────────────────────────────────────

def _try_ai_resume_generate_one_live(job, system_prompt):
    """Call Sonnet synchronously for a single job, parse, validate,
    and apply on success. Returns a dict:
        {ok: True,  message: str}
        {ok: False, message: str, raw: str|None, errors: [str]}
    Used by the batch runner's validation-retry fallback and by
    /api/ai-resume/regenerate.
    """
    user_message = _build_ai_resume_user_message(job)
    try:
        raw = _call_claude_with_retry(
            system_prompt,
            [{"role": "user", "content": user_message}],
            model=AI_RESUME_MODEL,
        )
    except Exception as e:
        return {"ok": False,
                "message": f"sonnet call failed: {str(e)[:200]}",
                "raw": None,
                "errors": [f"sonnet call failed: {str(e)[:200]}"]}

    parsed = _parse_ai_resume_response(raw)
    if not parsed.get("ok"):
        return {"ok": False,
                "message": f"parse failed: {parsed.get('error')}",
                "raw": raw,
                "errors": [f"parse failed: {parsed.get('error')}"]}

    ok, errors = _validate_ai_resume_bullets(parsed["bullets"])
    if not ok:
        return {"ok": False,
                "message": "validation failed: " + "; ".join(errors[:3]),
                "raw": raw,
                "errors": list(errors)}

    try:
        aok, amsg = _apply_ai_resume_to_job(
            job["id"], parsed["bullets"], analysis=parsed.get("analysis")
        )
        if aok:
            return {"ok": True, "message": amsg}
        return {"ok": False, "message": amsg,
                "raw": raw, "errors": [amsg]}
    except Exception as e:
        msg = f"apply failed: {str(e)[:200]}"
        return {"ok": False, "message": msg, "raw": raw, "errors": [msg]}


def _regenerate_ai_resume_live(job_id):
    """Synchronous single-job regenerate. Blocked if the job has a
    pending batch. Returns (ok: bool, message: str). On failure also
    saves Sonnet's raw output and the validator errors so the user
    can inspect them in the failure modal."""
    if _job_has_pending_ai_resume(job_id):
        return False, (
            "Job is currently inside an in-flight AI resume batch. "
            "Wait for the batch to finish before regenerating."
        )
    jobs = _load_jobs_for_ai_resume([job_id])
    if not jobs:
        return False, f"job {job_id} not found"
    system_prompt = _build_ai_resume_system_prompt()
    result = _try_ai_resume_generate_one_live(jobs[0], system_prompt)
    if result.get("ok"):
        return True, result.get("message", "ok")
    _save_ai_resume_failure(
        job_id, result.get("raw"), result.get("errors") or []
    )
    return False, result.get("message", "regenerate failed")


def _run_ai_resume_batch(batch_uuid, job_ids):
    """Background thread: submit an AI resume batch to Anthropic,
    poll every 60s, apply results. Mirrors _run_import_csv_ai_batch
    (line ~1943 area) in structure and state-management style."""
    stop_event = _ai_resume_batch_stop_events.setdefault(
        batch_uuid, threading.Event()
    )

    with _ai_resume_batches_lock:
        _ai_resume_batches_state[batch_uuid] = {
            "id": batch_uuid, "running": True, "status": "running",
            "total": len(job_ids), "succeeded": 0, "failed": 0,
            "estimated_cost": 0.0,
            "message": f"Loading {len(job_ids)} jobs\u2026",
        }
    _flush_ai_resume_progress(batch_uuid)
    _update_ai_resume_batch_row(batch_uuid, status="running",
                                started_at="NOW()")

    try:
        jobs = _load_jobs_for_ai_resume(job_ids)
        if not jobs:
            _mem_update_ai_resume(batch_uuid, {
                "running": False, "status": "failed",
                "message": "No jobs loaded from DB — aborting",
            })
            _flush_ai_resume_progress(batch_uuid)
            _update_ai_resume_batch_row(batch_uuid, finished_at="NOW()")
            return

        job_by_id = {j["id"]: j for j in jobs}
        # Persist the full job snapshot so restart recovery can
        # rebuild job_by_id without re-querying.
        _update_ai_resume_batch_row(batch_uuid, pending_jobs=jobs)

        # Mark the rows as pending so the UI shows a spinner
        _set_jobs_ai_resume_status(
            [j["id"] for j in jobs], "pending", batch_uuid
        )

        system_prompt = _build_ai_resume_system_prompt()
        batch_requests = [
            _build_ai_resume_batch_request(j["id"], system_prompt, j)
            for j in jobs
        ]

        _mem_update_ai_resume(batch_uuid, {
            "message": f"Submitting batch of {len(jobs)} jobs to Anthropic\u2026",
        })
        _flush_ai_resume_progress(batch_uuid)

        try:
            batch = _create_anthropic_batch(batch_requests)
        except Exception as e:
            _mem_update_ai_resume(batch_uuid, {
                "running": False, "status": "failed",
                "message": f"Batch submission failed: {str(e)[:200]}",
                "last_error": str(e)[:240],
            })
            _flush_ai_resume_progress(batch_uuid)
            _update_ai_resume_batch_row(batch_uuid, finished_at="NOW()",
                                        pending_jobs=None)
            _set_jobs_ai_resume_status(
                [j["id"] for j in jobs], "failed", batch_uuid
            )
            return

        _mem_update_ai_resume(batch_uuid, {
            "batch_id": batch.id,
            "batch_processing_status": getattr(batch, "processing_status",
                                               "in_progress"),
            "message": (f"Batch {batch.id[:8]}\u2026 submitted. "
                        "Polling every 60s."),
        })
        _update_ai_resume_batch_row(
            batch_uuid,
            anthropic_batch_id=batch.id,
            batch_processing_status=getattr(batch, "processing_status",
                                            "in_progress"),
        )
        _flush_ai_resume_progress(batch_uuid)

        _poll_ai_resume_batch_until_done(batch_uuid, batch, stop_event,
                                         job_by_id)

    except Exception as e:
        _mem_update_ai_resume(batch_uuid, {
            "running": False, "status": "failed",
            "message": f"Batch error: {str(e)[:200]}",
            "last_error": str(e)[:240],
        })
        _flush_ai_resume_progress(batch_uuid)
        _update_ai_resume_batch_row(batch_uuid, finished_at="NOW()",
                                    pending_jobs=None)


def _poll_ai_resume_batch_until_done(batch_uuid, batch, stop_event,
                                     job_by_id):
    """Shared polling loop used by both fresh batches and restart
    resumes. Polls every 60s, applies results on 'ended'."""
    poll_interval = 60
    consecutive_errors = 0
    batch_id = batch.id

    while batch.processing_status != "ended":
        if stop_event.is_set():
            _mem_update_ai_resume(batch_uuid, {
                "running": False, "status": "canceled", "stopped": True,
                "message": (
                    f"Stopped polling batch {batch_id[:8]}\u2026. "
                    "Anthropic batch still running — results preserved."
                ),
            })
            _flush_ai_resume_progress(batch_uuid)
            _update_ai_resume_batch_row(batch_uuid, finished_at="NOW()")
            return

        time.sleep(poll_interval)
        try:
            batch = _get_anthropic_batch(batch_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            err = f"poll error: {str(e)[:200]}"
            _mem_update_ai_resume(batch_uuid, {"last_error": err})
            _flush_ai_resume_progress(batch_uuid)
            if consecutive_errors >= 20:
                _mem_update_ai_resume(batch_uuid, {
                    "running": False, "status": "failed",
                    "message": f"Polling failed 20 times: {err}",
                })
                _flush_ai_resume_progress(batch_uuid)
                _update_ai_resume_batch_row(batch_uuid,
                                            finished_at="NOW()")
                _set_jobs_ai_resume_status(
                    list(job_by_id.keys()), "failed", batch_uuid
                )
                return
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
        with _ai_resume_batches_lock:
            s = _ai_resume_batches_state.get(batch_uuid, {})
            s["batch_processing_status"] = batch.processing_status
            s["batch_request_counts"] = counts_dict
            if counts_dict:
                finished = (counts_dict["succeeded"] + counts_dict["errored"]
                            + counts_dict["canceled"]
                            + counts_dict["expired"])
                s["message"] = (
                    f"Batch {batch_id[:8]}\u2026 {batch.processing_status} "
                    f"({finished}/{len(job_by_id)} processed)"
                )
        _flush_ai_resume_progress(batch_uuid)

    # Batch ended — retrieve + apply
    _mem_update_ai_resume(batch_uuid, {
        "message": f"Batch {batch_id[:8]}\u2026 ended. Applying results\u2026",
    })
    _flush_ai_resume_progress(batch_uuid)

    # Build the system prompt once for the validation-retry path
    retry_system_prompt = _build_ai_resume_system_prompt()

    succeeded = 0
    failed = 0
    processed_ids = set()
    for custom_id, result in _retrieve_ai_resume_batch_results_raw(batch_id):
        processed_ids.add(custom_id)
        job = job_by_id.get(custom_id)
        if not job:
            failed += 1
            continue

        applied_ok = False
        last_err = None
        # Track raw + structured errors for the failure-inspection modal
        first_raw = None
        first_errors = []

        if result.get("ok"):
            first_raw = result.get("text")
            parsed = _parse_ai_resume_response(result["text"])
            if parsed.get("ok"):
                vok, verrors = _validate_ai_resume_bullets(parsed["bullets"])
                if vok:
                    try:
                        aok, amsg = _apply_ai_resume_to_job(
                            custom_id, parsed["bullets"],
                            analysis=parsed.get("analysis"),
                        )
                        if aok:
                            applied_ok = True
                            succeeded += 1
                        else:
                            last_err = amsg
                            first_errors = [amsg]
                    except Exception as e:
                        last_err = f"apply error: {str(e)[:200]}"
                        first_errors = [last_err]
                else:
                    last_err = "validation failed: " + "; ".join(verrors[:3])
                    first_errors = list(verrors)
            else:
                last_err = f"parse failed: {parsed.get('error')}"
                first_errors = [last_err]
        else:
            last_err = result.get("error") or "unknown batch result"
            first_errors = [last_err]

        if not applied_ok:
            # Validation-retry path: one extra live Sonnet call
            retry_result = _try_ai_resume_generate_one_live(
                job, retry_system_prompt
            )
            if retry_result.get("ok"):
                succeeded += 1
                applied_ok = True
            else:
                failed += 1
                # Persist whichever raw output is more useful — prefer
                # the retry's (newer) if it has one, fall back to the
                # batch's. Both attempts' errors are recorded so the
                # user sees the full picture in the modal.
                retry_raw = retry_result.get("raw")
                retry_errors = retry_result.get("errors") or []
                combined_errors = []
                if first_errors:
                    combined_errors.append("Batch attempt:")
                    combined_errors.extend(first_errors)
                if retry_errors:
                    combined_errors.append("Retry attempt:")
                    combined_errors.extend(retry_errors)
                final_raw = retry_raw if retry_raw else first_raw
                _save_ai_resume_failure(
                    custom_id, final_raw, combined_errors
                )
                last_err = (
                    f"{last_err} | retry: "
                    f"{retry_result.get('message', 'failed')}"
                )

        with _ai_resume_batches_lock:
            s = _ai_resume_batches_state.get(batch_uuid, {})
            s["succeeded"] = succeeded
            s["failed"] = failed
            s["estimated_cost"] = (
                succeeded * AI_RESUME_COST_PER_JOB_BATCH
                + failed * AI_RESUME_COST_PER_JOB_BATCH
            )
            if last_err and not applied_ok:
                s["last_error"] = last_err[:240]
        _flush_ai_resume_progress(batch_uuid)

    # Any job that was in the batch but has no result — mark failed
    missed = [j for j in job_by_id if j not in processed_ids]
    if missed:
        _set_jobs_ai_resume_status(missed, "failed", batch_uuid)
        failed += len(missed)

    total_jobs = len(job_by_id)
    _mem_update_ai_resume(batch_uuid, {
        "running": False, "status": "completed",
        "succeeded": succeeded, "failed": failed,
        "message": (
            f"Done! {succeeded}/{total_jobs} AI resumes generated "
            f"({failed} failed). Batch {batch_id[:8]}\u2026"
        ),
    })
    _flush_ai_resume_progress(batch_uuid)
    _update_ai_resume_batch_row(batch_uuid, finished_at="NOW()",
                                pending_jobs=None)


def _run_ai_resume_batch_resume(batch_uuid, anthropic_batch_id):
    """Resume polling an in-flight AI resume batch after a server
    restart. Reads pending_jobs from the DB to reconstruct the
    job dict map, then delegates to _poll_ai_resume_batch_until_done.
    """
    stop_event = _ai_resume_batch_stop_events.setdefault(
        batch_uuid, threading.Event()
    )

    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT pending_jobs FROM ai_resume_batches WHERE id = %s",
                (batch_uuid,),
            )
            row = cur.fetchone()
    except Exception as e:
        print(f"Resume AI-resume {batch_uuid}: DB error: {e}")
        return

    if not row:
        print(f"Resume AI-resume {batch_uuid}: row gone, aborting")
        return

    pending_jobs_data = row[0]
    if not pending_jobs_data:
        _update_ai_resume_batch_row(
            batch_uuid, status="failed",
            message="Resume failed: pending_jobs missing",
            finished_at="NOW()",
        )
        return

    new_jobs = pending_jobs_data if isinstance(pending_jobs_data, list) else []
    job_by_id = {j["id"]: j for j in new_jobs}

    with _ai_resume_batches_lock:
        _ai_resume_batches_state[batch_uuid] = {
            "id": batch_uuid, "running": True, "status": "running",
            "batch_id": anthropic_batch_id,
            "total": len(new_jobs),
            "succeeded": 0, "failed": 0, "estimated_cost": 0.0,
            "message": (f"Resumed — polling Anthropic batch "
                        f"{anthropic_batch_id[:8]}\u2026"),
        }
    _flush_ai_resume_progress(batch_uuid)

    try:
        batch = _get_anthropic_batch(anthropic_batch_id)
    except Exception as e:
        _mem_update_ai_resume(batch_uuid, {
            "running": False, "status": "failed",
            "message": f"Resume poll error: {e}",
        })
        _flush_ai_resume_progress(batch_uuid)
        _update_ai_resume_batch_row(batch_uuid, finished_at="NOW()")
        return

    _poll_ai_resume_batch_until_done(batch_uuid, batch, stop_event, job_by_id)


def _resume_in_flight_ai_resume_batches():
    """Called at server startup — respawn polling threads for any
    ai_resume_batches rows with status IN ('queued','running').
    Parallel to _resume_in_flight_imports()."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, anthropic_batch_id
                  FROM ai_resume_batches
                 WHERE status IN ('queued', 'running')
            """)
            rows = cur.fetchall()
    except Exception as e:
        print(f"_resume_in_flight_ai_resume_batches: DB error: {e}")
        return

    for batch_uuid, anthropic_batch_id in rows:
        batch_uuid = str(batch_uuid)
        if not anthropic_batch_id:
            _update_ai_resume_batch_row(
                batch_uuid, status="failed",
                message="Abandoned before Anthropic batch was submitted",
                finished_at="NOW()",
            )
            print(f"Resume: marked unsubmitted AI-resume batch "
                  f"{batch_uuid[:8]} as failed")
            continue

        t = threading.Thread(
            target=_run_ai_resume_batch_resume,
            args=(batch_uuid, anthropic_batch_id),
            daemon=True,
        )
        t.start()
        print(f"Resume: respawned AI-resume batch poller for "
              f"{batch_uuid[:8]} (anthropic={anthropic_batch_id[:8]})")


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


def _run_import_csv_ai(batch_uuid, csv_bytes, location):
    """Background thread: parse CSV and import new jobs using
    Claude Haiku for scoring (live mode). Single-runner — the POST
    handler sets _ai_live_import_running before spawning this thread."""
    global _ai_live_import_running
    stop_event = _ai_import_stop_events.get(batch_uuid, threading.Event())
    try:
        import csv as _csv
        import io
        from concurrent.futures import ThreadPoolExecutor
        from process_new_postings import (
            is_excluded_company, is_excluded_description,
            is_excluded_title, is_excluded_role, is_swe_role,
            calc_tech_score, calc_level_bonus, calc_company_bonus,
            assign_tier, make_job_id, pick_bullets,
            customize_skills, generate_resume_txt,
        )

        _mem_update(batch_uuid, {
            "running": True, "status": "running", "progress": 0, "total": 0,
            "added": 0, "scored_ai": 0, "scored_fallback": 0,
            "estimated_cost": 0.0, "stopped": False,
            "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
            "last_error": "", "mode": "live",
            "batch_id": None, "batch_processing_status": None,
            "batch_request_counts": None,
            "message": "Parsing CSV\u2026", "jobs_found": [],
        })
        _update_import_row(batch_uuid, status="running", started_at="NOW()")

        text = csv_bytes.decode("utf-8-sig")
        rows = list(_csv.DictReader(io.StringIO(text)))

        if not location and rows:
            location = (rows[0].get("Search Location") or "").strip() or None

        # Fetch import_date label from the pre-inserted row
        with _ai_imports_lock:
            batch_label = _ai_imports[batch_uuid]["import_date"]

        # Filter (same logic as regex import)
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
                "company": company, "title": title,
                "location": (r.get("Location") or "").strip(),
                "work_type": (r.get("Work Type") or "").strip(),
                "salary": (r.get("Salary") or "N/A").strip(),
                "posted_date": (r.get("Posted Date") or "").strip(),
                "import_date": batch_label,
                "description": desc,
                "job_link": link,
                "apply_link": (r.get("Application Link") or "").strip(),
            })

        _mem_update(batch_uuid, {
            "total": len(filtered),
            "message": f"Found {len(filtered)} qualifying jobs. Checking DB\u2026",
        })
        _flush_import_progress(batch_uuid)

        if not filtered:
            _mem_update(batch_uuid, {"running": False, "status": "completed",
                                     "message": "No qualifying SWE jobs found in CSV."})
            _flush_import_progress(batch_uuid)
            return

        with Db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT job_link FROM jobs WHERE job_link IS NOT NULL")
            existing = {row[0] for row in cur.fetchall()}

        new_jobs = [j for j in filtered if j["job_link"] not in existing]

        _mem_update(batch_uuid, {
            "total": len(new_jobs),
            "message": f"Scoring {len(new_jobs)} new jobs with Claude Haiku\u2026",
        })
        _flush_import_progress(batch_uuid)

        if not new_jobs:
            _mem_update(batch_uuid, {"running": False, "status": "completed",
                                     "message": "No new jobs to import (all dedupped)."})
            _flush_import_progress(batch_uuid)
            return

        system_prompt = _build_scoring_system_prompt()

        def _compute_regex_score(job):
            dl = job["description"].lower()
            tl = job["title"].lower()
            return calc_tech_score(dl) + calc_level_bonus(tl) + calc_company_bonus(job["company"])

        _TIER_RANK = {"Weak Match": 0, "Match": 1, "Strong Match": 2}

        def _regex_fallback(job):
            regex_score = _compute_regex_score(job)
            job["score"] = regex_score
            job["regex_score"] = regex_score
            job["tier"] = assign_tier(regex_score)
            job["ai_reasoning"] = None

        def score_one(job):
            if stop_event.is_set():
                _regex_fallback(job)
                with _ai_imports_lock:
                    s = _ai_imports.get(batch_uuid, {})
                    s["scored_fallback"] = s.get("scored_fallback", 0) + 1
                    s["progress"] = s.get("progress", 0) + 1
                return job

            result = _score_job_with_haiku(job, system_prompt)
            if result.get("used_ai"):
                job["score"] = result["legacy_score"]
                job["tier"] = assign_tier(result["legacy_score"])
                job["ai_reasoning"] = result["reasoning"]
                job["regex_score"] = _compute_regex_score(job)
                regex_tier = assign_tier(job["regex_score"])
                ai_rank = _TIER_RANK.get(job["tier"], 1)
                rx_rank = _TIER_RANK.get(regex_tier, 1)
                with _ai_imports_lock:
                    s = _ai_imports.get(batch_uuid, {})
                    s["scored_ai"] = s.get("scored_ai", 0) + 1
                    s["estimated_cost"] = s.get("estimated_cost", 0.0) + AI_SCORING_COST_PER_JOB
                    if ai_rank == rx_rank:
                        s["regex_agree"] = s.get("regex_agree", 0) + 1
                    elif ai_rank > rx_rank:
                        s["ai_promoted"] = s.get("ai_promoted", 0) + 1
                    else:
                        s["ai_demoted"] = s.get("ai_demoted", 0) + 1
            else:
                _regex_fallback(job)
                err = (result.get("error") or "")[:240]
                with _ai_imports_lock:
                    s = _ai_imports.get(batch_uuid, {})
                    s["scored_fallback"] = s.get("scored_fallback", 0) + 1
                    if err:
                        s["last_error"] = err

            with _ai_imports_lock:
                s = _ai_imports.get(batch_uuid, {})
                s["progress"] = s.get("progress", 0) + 1
                s["message"] = (
                    f"AI scoring: {s['progress']}/{s.get('total', 0)} "
                    f"({s.get('scored_ai', 0)} AI, "
                    f"{s.get('scored_fallback', 0)} fallback, "
                    f"~${s.get('estimated_cost', 0.0):.3f})"
                )
                return job

        # 2 workers stays under Anthropic Tier 1 ITPM (50K/min)
        with ThreadPoolExecutor(max_workers=2) as executor:
            scored_jobs = list(executor.map(score_one, new_jobs))

        _mem_update(batch_uuid, {"message": f"Inserting {len(scored_jobs)} jobs into DB\u2026"})

        with Db() as conn:
            cur = conn.cursor()
            for j in scored_jobs:
                bullets = pick_bullets(j["description"], j["title"])
                langs, frameworks, misc = customize_skills(j["description"])
                resume_text = generate_resume_txt(j, bullets, langs, frameworks, misc)
                # ON CONFLICT strategy: if a job with the same ID already
                # exists, ONLY upgrade its scoring fields when (1) the
                # existing row is regex-only (ai_reasoning IS NULL) AND
                # (2) the incoming row has AI data. This handles the case
                # where a job was regex-fallback-scored in an earlier batch
                # and later re-scored successfully by Sonnet — without
                # clobbering any manual edits, notes, or AI-generated
                # resumes on pre-existing rows.
                cur.execute("""
                    INSERT INTO jobs
                        (id, company, title, location, work_type, salary,
                         posted_date, import_date, description, score,
                         tier, job_link, apply_link, resume_text,
                         ai_reasoning, regex_score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        score        = EXCLUDED.score,
                        tier         = EXCLUDED.tier,
                        ai_reasoning = EXCLUDED.ai_reasoning,
                        regex_score  = EXCLUDED.regex_score
                    WHERE jobs.ai_reasoning IS NULL
                      AND EXCLUDED.ai_reasoning IS NOT NULL
                """, (
                    j["id"], j["company"], j["title"], j["location"],
                    j["work_type"], j["salary"], j["posted_date"],
                    j["import_date"], j["description"], j["score"],
                    j["tier"], j["job_link"], j["apply_link"],
                    resume_text, j.get("ai_reasoning"), j.get("regex_score"),
                ))
                cur.execute(
                    "INSERT INTO job_state (job_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (j["id"],)
                )

        tiers = {}
        for j in scored_jobs:
            tiers.setdefault(j["tier"], []).append(j["title"])
        summary = ", ".join(f"{t}: {len(v)}" for t, v in sorted(tiers.items()))
        was_stopped = stop_event.is_set()

        with _ai_imports_lock:
            s = _ai_imports.get(batch_uuid, {})
            s.update({
                "running": False, "status": "completed" if not was_stopped else "canceled",
                "added": len(scored_jobs), "stopped": was_stopped,
                "message": (
                    ("Stopped early. " if was_stopped else "Done! ")
                    + f"{len(scored_jobs)} inserted "
                    f"({s.get('scored_ai', 0)} AI, "
                    f"{s.get('scored_fallback', 0)} fallback), "
                    f"cost ~${s.get('estimated_cost', 0.0):.3f}. {summary}."
                ),
                "jobs_found": [
                    {"title": j["title"], "tier": j["tier"],
                     "score": j["score"], "location": j["location"]}
                    for j in sorted(scored_jobs, key=lambda x: -x["score"])[:20]
                ],
            })
        _flush_import_progress(batch_uuid)
        _update_import_row(batch_uuid, finished_at="NOW()", added=len(scored_jobs))

    except Exception as e:
        _mem_update(batch_uuid, {"running": False, "status": "failed",
                                 "message": f"Error: {str(e)}", "last_error": str(e)[:240]})
        _flush_import_progress(batch_uuid)
        _update_import_row(batch_uuid, finished_at="NOW()")
    finally:
        with _ai_imports_lock:
            global _ai_live_import_running
            _ai_live_import_running = False


# ─────────────────────────────────────────────────────────
# BATCH MODE — same import flow but uses Anthropic Batches
# ─────────────────────────────────────────────────────────

def _run_import_csv_ai_batch(batch_uuid, csv_bytes, location):
    """Background thread: parse CSV, submit to Anthropic Batch API,
    poll until complete, retrieve results, then insert.

    Trades ~50% lower cost for async processing (minutes to hours).
    Concurrent — the POST handler allows multiple batch imports at once.
    """
    stop_event = _ai_import_stop_events.get(batch_uuid, threading.Event())
    try:
        import csv as _csv
        import io
        from process_new_postings import (
            is_excluded_company, is_excluded_description,
            is_excluded_title, is_excluded_role, is_swe_role,
            calc_tech_score, calc_level_bonus, calc_company_bonus,
            assign_tier, make_job_id, pick_bullets,
            customize_skills, generate_resume_txt,
        )

        _mem_update(batch_uuid, {
            "running": True, "status": "running", "progress": 0, "total": 0,
            "added": 0, "scored_ai": 0, "scored_fallback": 0,
            "estimated_cost": 0.0, "stopped": False,
            "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
            "last_error": "", "mode": "batch",
            "batch_id": None, "batch_processing_status": None,
            "batch_request_counts": None,
            "message": "Parsing CSV\u2026", "jobs_found": [],
        })
        _update_import_row(batch_uuid, status="running", started_at="NOW()")

        text = csv_bytes.decode("utf-8-sig")
        rows = list(_csv.DictReader(io.StringIO(text)))

        if not location and rows:
            location = (rows[0].get("Search Location") or "").strip() or None

        with _ai_imports_lock:
            batch_label = _ai_imports[batch_uuid]["import_date"]

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
                "company": company, "title": title,
                "location": (r.get("Location") or "").strip(),
                "work_type": (r.get("Work Type") or "").strip(),
                "salary": (r.get("Salary") or "N/A").strip(),
                "posted_date": (r.get("Posted Date") or "").strip(),
                "import_date": batch_label,
                "description": desc,
                "job_link": link,
                "apply_link": (r.get("Application Link") or "").strip(),
            })

        _mem_update(batch_uuid, {
            "total": len(filtered),
            "message": f"Found {len(filtered)} qualifying jobs. Checking DB\u2026",
        })
        _flush_import_progress(batch_uuid)

        if not filtered:
            _mem_update(batch_uuid, {"running": False, "status": "completed",
                                     "message": "No qualifying SWE jobs found in CSV."})
            _flush_import_progress(batch_uuid)
            _update_import_row(batch_uuid, finished_at="NOW()")
            return

        with Db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT job_link FROM jobs WHERE job_link IS NOT NULL")
            existing = {row[0] for row in cur.fetchall()}

        new_jobs = [j for j in filtered if j["job_link"] not in existing]
        job_by_id = {j["id"]: j for j in new_jobs}

        _mem_update(batch_uuid, {
            "total": len(new_jobs),
            "message": f"Submitting batch of {len(new_jobs)} jobs to Anthropic\u2026",
        })
        _flush_import_progress(batch_uuid)

        if not new_jobs:
            _mem_update(batch_uuid, {"running": False, "status": "completed",
                                     "message": "No new jobs to import (all dedupped)."})
            _flush_import_progress(batch_uuid)
            _update_import_row(batch_uuid, finished_at="NOW()")
            return

        # Persist pending_jobs so restart recovery can rebuild job_by_id
        _update_import_row(batch_uuid, pending_jobs=new_jobs)

        system_prompt = _build_scoring_system_prompt()
        batch_requests = [_build_batch_request(j["id"], system_prompt, j) for j in new_jobs]

        try:
            batch = _create_anthropic_batch(batch_requests)
        except Exception as e:
            _mem_update(batch_uuid, {"running": False, "status": "failed",
                                     "message": f"Batch submission failed: {str(e)[:200]}",
                                     "last_error": str(e)[:240]})
            _flush_import_progress(batch_uuid)
            _update_import_row(batch_uuid, finished_at="NOW()", pending_jobs=None)
            return

        _mem_update(batch_uuid, {
            "batch_id": batch.id,
            "batch_processing_status": getattr(batch, "processing_status", "in_progress"),
            "message": f"Batch {batch.id[:8]}\u2026 submitted. Polling every 30s.",
        })
        _update_import_row(batch_uuid, anthropic_batch_id=batch.id,
                           batch_processing_status=getattr(batch, "processing_status", "in_progress"))
        _flush_import_progress(batch_uuid)

        # Poll until ended
        poll_interval = 30
        consecutive_errors = 0
        while True:
            if stop_event.is_set():
                _mem_update(batch_uuid, {
                    "running": False, "status": "canceled", "stopped": True,
                    "message": (
                        f"Stopped polling batch {batch.id[:8]}\u2026. "
                        "Anthropic batch still running — results preserved."
                    ),
                })
                _flush_import_progress(batch_uuid)
                _update_import_row(batch_uuid, finished_at="NOW()")
                return

            time.sleep(poll_interval)
            try:
                batch = _get_anthropic_batch(batch.id)
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                err = f"poll error: {str(e)[:200]}"
                _mem_update(batch_uuid, {"last_error": err})
                _flush_import_progress(batch_uuid)
                if consecutive_errors >= 20:
                    _mem_update(batch_uuid, {"running": False, "status": "failed",
                                             "message": f"Polling failed 20 times: {err}"})
                    _flush_import_progress(batch_uuid)
                    _update_import_row(batch_uuid, finished_at="NOW()")
                    return
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
            with _ai_imports_lock:
                s = _ai_imports.get(batch_uuid, {})
                s["batch_processing_status"] = batch.processing_status
                s["batch_request_counts"] = counts_dict
                if counts_dict:
                    finished = (counts_dict["succeeded"] + counts_dict["errored"]
                                + counts_dict["canceled"] + counts_dict["expired"])
                    s["progress"] = finished
                    s["message"] = (
                        f"Batch {batch.id[:8]}\u2026 {batch.processing_status} "
                        f"({finished}/{len(new_jobs)} processed)"
                    )
            _flush_import_progress(batch_uuid)

            if batch.processing_status == "ended":
                break

        _mem_update(batch_uuid, {"message": f"Batch {batch.id[:8]}\u2026 ended. Retrieving results\u2026"})

        _TIER_RANK = {"Weak Match": 0, "Match": 1, "Strong Match": 2}
        scored_jobs = []
        for custom_id, result in _retrieve_anthropic_batch_results(batch.id):
            job = job_by_id.get(custom_id)
            if not job:
                continue
            dl = job["description"].lower()
            tl = job["title"].lower()
            regex_score = (calc_tech_score(dl) + calc_level_bonus(tl)
                           + calc_company_bonus(job["company"]))
            job["regex_score"] = regex_score
            regex_tier = assign_tier(regex_score)
            if result.get("used_ai"):
                job["score"] = result["legacy_score"]
                job["tier"] = assign_tier(result["legacy_score"])
                job["ai_reasoning"] = result["reasoning"]
                ai_rank = _TIER_RANK.get(job["tier"], 1)
                rx_rank = _TIER_RANK.get(regex_tier, 1)
                with _ai_imports_lock:
                    s = _ai_imports.get(batch_uuid, {})
                    s["scored_ai"] = s.get("scored_ai", 0) + 1
                    s["estimated_cost"] = s.get("estimated_cost", 0.0) + AI_SCORING_COST_PER_JOB * 0.5
                    if ai_rank == rx_rank:
                        s["regex_agree"] = s.get("regex_agree", 0) + 1
                    elif ai_rank > rx_rank:
                        s["ai_promoted"] = s.get("ai_promoted", 0) + 1
                    else:
                        s["ai_demoted"] = s.get("ai_demoted", 0) + 1
            else:
                job["score"] = regex_score
                job["tier"] = regex_tier
                job["ai_reasoning"] = None
                err = (result.get("error") or "")[:240]
                with _ai_imports_lock:
                    s = _ai_imports.get(batch_uuid, {})
                    s["scored_fallback"] = s.get("scored_fallback", 0) + 1
                    if err:
                        s["last_error"] = err
            scored_jobs.append(job)

        _mem_update(batch_uuid, {"message": f"Inserting {len(scored_jobs)} jobs into DB\u2026"})

        with Db() as conn:
            cur = conn.cursor()
            for j in scored_jobs:
                bullets = pick_bullets(j["description"], j["title"])
                langs, frameworks, misc = customize_skills(j["description"])
                resume_text = generate_resume_txt(j, bullets, langs, frameworks, misc)
                # ON CONFLICT strategy: if a job with the same ID already
                # exists, ONLY upgrade its scoring fields when (1) the
                # existing row is regex-only (ai_reasoning IS NULL) AND
                # (2) the incoming row has AI data. This handles the case
                # where a job was regex-fallback-scored in an earlier batch
                # and later re-scored successfully by Sonnet — without
                # clobbering any manual edits, notes, or AI-generated
                # resumes on pre-existing rows.
                cur.execute("""
                    INSERT INTO jobs
                        (id, company, title, location, work_type, salary,
                         posted_date, import_date, description, score,
                         tier, job_link, apply_link, resume_text,
                         ai_reasoning, regex_score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        score        = EXCLUDED.score,
                        tier         = EXCLUDED.tier,
                        ai_reasoning = EXCLUDED.ai_reasoning,
                        regex_score  = EXCLUDED.regex_score
                    WHERE jobs.ai_reasoning IS NULL
                      AND EXCLUDED.ai_reasoning IS NOT NULL
                """, (
                    j["id"], j["company"], j["title"], j["location"],
                    j["work_type"], j["salary"], j["posted_date"],
                    j["import_date"], j["description"], j["score"],
                    j["tier"], j["job_link"], j["apply_link"],
                    resume_text, j.get("ai_reasoning"), j.get("regex_score"),
                ))
                cur.execute(
                    "INSERT INTO job_state (job_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (j["id"],)
                )

        tiers = {}
        for j in scored_jobs:
            tiers.setdefault(j["tier"], []).append(j["title"])
        summary = ", ".join(f"{t}: {len(v)}" for t, v in sorted(tiers.items()))

        with _ai_imports_lock:
            s = _ai_imports.get(batch_uuid, {})
            s.update({
                "running": False, "status": "completed", "added": len(scored_jobs),
                "message": (
                    f"Done! Batch {batch.id[:8]}\u2026 inserted {len(scored_jobs)} jobs "
                    f"({s.get('scored_ai', 0)} AI, {s.get('scored_fallback', 0)} fallback), "
                    f"cost ~${s.get('estimated_cost', 0.0):.3f} (50% batch discount). {summary}."
                ),
                "jobs_found": [
                    {"title": j["title"], "tier": j["tier"],
                     "score": j["score"], "location": j["location"]}
                    for j in sorted(scored_jobs, key=lambda x: -x["score"])[:20]
                ],
            })
        _flush_import_progress(batch_uuid)
        _update_import_row(batch_uuid, finished_at="NOW()", added=len(scored_jobs), pending_jobs=None)

    except Exception as e:
        _mem_update(batch_uuid, {"running": False, "status": "failed",
                                 "message": f"Batch error: {str(e)}", "last_error": str(e)[:240]})
        _flush_import_progress(batch_uuid)
        _update_import_row(batch_uuid, finished_at="NOW()")


def _run_import_csv_ai_batch_resume(batch_uuid, anthropic_batch_id):
    """Resume polling an in-flight Anthropic batch after a server restart.

    Reads pending_jobs from the DB to reconstruct job_by_id, then runs
    the same poll → retrieve → insert logic as the original batch worker.
    """
    from process_new_postings import (
        calc_tech_score, calc_level_bonus, calc_company_bonus,
        assign_tier, pick_bullets, customize_skills, generate_resume_txt,
    )

    stop_event = _ai_import_stop_events.get(batch_uuid, threading.Event())

    # Load pending_jobs from DB so we can map custom_ids back to job dicts
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT pending_jobs, import_date, location FROM import_batches WHERE id = %s",
                (batch_uuid,)
            )
            row = cur.fetchone()
    except Exception as e:
        print(f"Resume {batch_uuid}: DB error reading pending_jobs: {e}")
        _mem_update(batch_uuid, {"running": False, "status": "failed",
                                 "message": f"Resume failed: {e}"})
        _flush_import_progress(batch_uuid)
        return

    if not row:
        print(f"Resume {batch_uuid}: import_batches row gone, aborting resume")
        return

    pending_jobs_data, import_date_label, _ = row
    if not pending_jobs_data:
        _mem_update(batch_uuid, {"running": False, "status": "failed",
                                 "message": "Resume failed: pending_jobs missing from DB"})
        _flush_import_progress(batch_uuid)
        return

    new_jobs = pending_jobs_data if isinstance(pending_jobs_data, list) else []
    job_by_id = {j["id"]: j for j in new_jobs}

    _mem_update(batch_uuid, {
        "running": True, "status": "running",
        "batch_id": anthropic_batch_id,
        "message": f"Resumed — polling Anthropic batch {anthropic_batch_id[:8]}\u2026",
    })
    _flush_import_progress(batch_uuid)

    # First check if the batch already ended while the server was down
    try:
        batch = _get_anthropic_batch(anthropic_batch_id)
    except Exception as e:
        _mem_update(batch_uuid, {"running": False, "status": "failed",
                                 "message": f"Resume poll error: {e}"})
        _flush_import_progress(batch_uuid)
        _update_import_row(batch_uuid, finished_at="NOW()")
        return

    poll_interval = 30
    consecutive_errors = 0

    while batch.processing_status != "ended":
        if stop_event.is_set():
            _mem_update(batch_uuid, {"running": False, "status": "canceled", "stopped": True,
                                     "message": f"Stopped resumed polling of {anthropic_batch_id[:8]}\u2026"})
            _flush_import_progress(batch_uuid)
            _update_import_row(batch_uuid, finished_at="NOW()")
            return

        time.sleep(poll_interval)
        try:
            batch = _get_anthropic_batch(anthropic_batch_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 20:
                _mem_update(batch_uuid, {"running": False, "status": "failed",
                                         "message": f"Resume poll failed 20 times: {e}"})
                _flush_import_progress(batch_uuid)
                _update_import_row(batch_uuid, finished_at="NOW()")
                return
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
        with _ai_imports_lock:
            s = _ai_imports.get(batch_uuid, {})
            s["batch_processing_status"] = batch.processing_status
            s["batch_request_counts"] = counts_dict
            if counts_dict:
                finished = (counts_dict["succeeded"] + counts_dict["errored"]
                            + counts_dict["canceled"] + counts_dict["expired"])
                s["progress"] = finished
                s["total"] = sum(counts_dict.values())
                s["message"] = (
                    f"(Resumed) Batch {anthropic_batch_id[:8]}\u2026 "
                    f"{batch.processing_status} ({finished} processed)"
                )
        _flush_import_progress(batch_uuid)

    # Batch ended — retrieve results and insert
    _mem_update(batch_uuid, {"message": "(Resumed) Batch ended. Retrieving results\u2026"})

    scored_jobs = []
    for custom_id, result in _retrieve_anthropic_batch_results(anthropic_batch_id):
        job = job_by_id.get(custom_id)
        if not job:
            continue
        dl = job["description"].lower()
        tl = job["title"].lower()
        regex_score = (calc_tech_score(dl) + calc_level_bonus(tl)
                       + calc_company_bonus(job["company"]))
        job["regex_score"] = regex_score
        regex_tier = assign_tier(regex_score)
        if result.get("used_ai"):
            job["score"] = result["legacy_score"]
            job["tier"] = assign_tier(result["legacy_score"])
            job["ai_reasoning"] = result["reasoning"]
            with _ai_imports_lock:
                s = _ai_imports.get(batch_uuid, {})
                s["scored_ai"] = s.get("scored_ai", 0) + 1
                s["estimated_cost"] = s.get("estimated_cost", 0.0) + AI_SCORING_COST_PER_JOB * 0.5
        else:
            job["score"] = regex_score
            job["tier"] = regex_tier
            job["ai_reasoning"] = None
            with _ai_imports_lock:
                s = _ai_imports.get(batch_uuid, {})
                s["scored_fallback"] = s.get("scored_fallback", 0) + 1
                if result.get("error"):
                    s["last_error"] = str(result["error"])[:240]
        scored_jobs.append(job)

    try:
        with Db() as conn:
            cur = conn.cursor()
            for j in scored_jobs:
                bullets = pick_bullets(j["description"], j["title"])
                langs, frameworks, misc = customize_skills(j["description"])
                resume_text = generate_resume_txt(j, bullets, langs, frameworks, misc)
                # ON CONFLICT strategy: if a job with the same ID already
                # exists, ONLY upgrade its scoring fields when (1) the
                # existing row is regex-only (ai_reasoning IS NULL) AND
                # (2) the incoming row has AI data. This handles the case
                # where a job was regex-fallback-scored in an earlier batch
                # and later re-scored successfully by Sonnet — without
                # clobbering any manual edits, notes, or AI-generated
                # resumes on pre-existing rows.
                cur.execute("""
                    INSERT INTO jobs
                        (id, company, title, location, work_type, salary,
                         posted_date, import_date, description, score,
                         tier, job_link, apply_link, resume_text,
                         ai_reasoning, regex_score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        score        = EXCLUDED.score,
                        tier         = EXCLUDED.tier,
                        ai_reasoning = EXCLUDED.ai_reasoning,
                        regex_score  = EXCLUDED.regex_score
                    WHERE jobs.ai_reasoning IS NULL
                      AND EXCLUDED.ai_reasoning IS NOT NULL
                """, (
                    j["id"], j["company"], j["title"], j["location"],
                    j["work_type"], j["salary"], j["posted_date"],
                    j["import_date"], j["description"], j["score"],
                    j["tier"], j["job_link"], j["apply_link"],
                    resume_text, j.get("ai_reasoning"), j.get("regex_score"),
                ))
                cur.execute(
                    "INSERT INTO job_state (job_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (j["id"],)
                )
    except Exception as e:
        _mem_update(batch_uuid, {"running": False, "status": "failed",
                                 "message": f"Resume DB insert error: {e}"})
        _flush_import_progress(batch_uuid)
        _update_import_row(batch_uuid, finished_at="NOW()")
        return

    with _ai_imports_lock:
        s = _ai_imports.get(batch_uuid, {})
        s.update({
            "running": False, "status": "completed", "added": len(scored_jobs),
            "message": f"(Resumed) Done! Inserted {len(scored_jobs)} jobs.",
        })
    _flush_import_progress(batch_uuid)
    _update_import_row(batch_uuid, finished_at="NOW()", added=len(scored_jobs), pending_jobs=None)


def _resume_in_flight_imports():
    """Called at server startup — respawn polling threads for any
    batch-mode imports that were running when the server last stopped.
    Live-mode running rows are force-marked failed (can't resume)."""
    try:
        with Db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, import_date, mode, location, anthropic_batch_id
                FROM import_batches
                WHERE status IN ('queued', 'running')
            """)
            rows = cur.fetchall()
    except Exception as e:
        print(f"_resume_in_flight_imports: DB error: {e}")
        return

    for batch_uuid, import_date, mode, location, anthropic_batch_id in rows:
        if mode == "live":
            # Live import can't be resumed — job list is gone
            _update_import_row(batch_uuid, status="failed",
                               message="Interrupted by server restart (live mode)",
                               finished_at="NOW()")
            print(f"Resume: marked live import {batch_uuid[:8]} as failed")
            continue

        if not anthropic_batch_id:
            # Batch was queued but never submitted
            _update_import_row(batch_uuid, status="failed",
                               message="Abandoned before Anthropic batch was submitted",
                               finished_at="NOW()")
            print(f"Resume: marked unsubmitted batch {batch_uuid[:8]} as failed")
            continue

        # Rebuild in-memory state
        with _ai_imports_lock:
            _ai_imports[batch_uuid] = {
                "id": batch_uuid, "status": "running", "mode": "batch",
                "import_date": import_date, "location": location,
                "running": True, "progress": 0, "total": 0, "added": 0,
                "scored_ai": 0, "scored_fallback": 0, "estimated_cost": 0.0,
                "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
                "stopped": False, "last_error": "",
                "batch_id": anthropic_batch_id, "batch_processing_status": None,
                "batch_request_counts": None,
                "message": f"Resuming after restart — polling {anthropic_batch_id[:8]}\u2026",
                "jobs_found": [],
            }
            _ai_import_stop_events[batch_uuid] = threading.Event()

        threading.Thread(
            target=_run_import_csv_ai_batch_resume,
            args=(batch_uuid, anthropic_batch_id),
            daemon=True,
        ).start()
        print(f"Resume: spawned polling thread for batch {batch_uuid[:8]} "
              f"(anthropic={anthropic_batch_id[:8]})")


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
                               j.ai_resume_generated_at, j.ai_resume_status,
                               j.ai_resume_last_raw, j.ai_resume_last_errors,
                               j.ai_resume_reasoning, j.created_at,
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

        # ── /api/imports-stream (SSE) — live updates for all batches ─
        if self.path == "/api/imports-stream":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                def _fetch_all_imports():
                    with Db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT * FROM import_batches "
                            "ORDER BY created_at DESC LIMIT 200"
                        )
                        rows = cur.fetchall()
                        desc = cur.description
                    return [_import_row_to_dict(r, desc) for r in rows]

                imports = _fetch_all_imports()
                frame = json.dumps({"type": "snapshot", "imports": imports})
                self.wfile.write(f"data: {frame}\n\n".encode())
                self.wfile.flush()

                # Track last-seen signature per UUID to detect changes
                last_sig = {
                    imp["id"]: (imp.get("status"), imp.get("progress"),
                                imp.get("message"))
                    for imp in imports
                }

                while True:
                    time.sleep(1)
                    with _ai_imports_lock:
                        active_ids = list(_ai_imports.keys())
                    sent_any = False
                    for uid in active_ids:
                        with _ai_imports_lock:
                            mem = dict(_ai_imports.get(uid) or {})
                        if not mem:
                            continue
                        sig = (mem.get("status"), mem.get("progress"),
                               mem.get("message"))
                        if last_sig.get(uid) == sig:
                            continue
                        last_sig[uid] = sig
                        update = {
                            "id": uid,
                            "import_date": mem.get("import_date"),
                            "mode": mem.get("mode"),
                            "location": mem.get("location"),
                            "status": mem.get("status"),
                            "progress": mem.get("progress"),
                            "total": mem.get("total"),
                            "added": mem.get("added"),
                            "scored_ai": mem.get("scored_ai"),
                            "scored_fallback": mem.get("scored_fallback"),
                            "estimated_cost": mem.get("estimated_cost"),
                            "message": mem.get("message"),
                            "last_error": mem.get("last_error"),
                            "batch_processing_status": mem.get(
                                "batch_processing_status"
                            ),
                        }
                        frame = json.dumps({"type": "update", "import": update})
                        self.wfile.write(f"data: {frame}\n\n".encode())
                        sent_any = True
                    if sent_any:
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        # ── /api/imports — list or single import batch ────────────
        if self.path == "/api/imports" or \
                self.path.startswith("/api/imports?") or \
                self.path.startswith("/api/imports/"):
            parsed = urllib.parse.urlparse(self.path)
            parts = parsed.path.rstrip("/").split("/")
            # /api/imports/<uuid>
            if len(parts) >= 4 and parts[3]:
                batch_uuid = parts[3]
                try:
                    with Db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT * FROM import_batches WHERE id = %s",
                            (batch_uuid,)
                        )
                        row = cur.fetchone()
                        if not row:
                            _json_response(self, {"error": "Not found"}, 404)
                            return
                        d = _import_row_to_dict(row, cur.description)
                    _json_response(self, d)
                except Exception as e:
                    _json_response(self, {"error": str(e)}, 500)
                return
            # /api/imports[?status=...]
            qs = urllib.parse.parse_qs(parsed.query)
            status_filter = qs.get("status", [None])[0]
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    if status_filter:
                        statuses = [s.strip() for s in status_filter.split(",")]
                        cur.execute(
                            "SELECT * FROM import_batches "
                            "WHERE status = ANY(%s) "
                            "ORDER BY created_at DESC LIMIT 200",
                            (statuses,)
                        )
                    else:
                        cur.execute(
                            "SELECT * FROM import_batches "
                            "ORDER BY created_at DESC LIMIT 200"
                        )
                    rows = cur.fetchall()
                    desc = cur.description
                result = [_import_row_to_dict(r, desc) for r in rows]
                _json_response(self, result)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/ai-resume/batches — list or single AI resume batch ──
        if self.path == "/api/ai-resume/batches" or \
                self.path.startswith("/api/ai-resume/batches?") or \
                self.path.startswith("/api/ai-resume/batches/"):
            parsed = urllib.parse.urlparse(self.path)
            parts = parsed.path.rstrip("/").split("/")
            # /api/ai-resume/batches/<uuid>
            if len(parts) >= 5 and parts[4]:
                batch_uuid = parts[4]
                try:
                    with Db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT * FROM ai_resume_batches WHERE id = %s",
                            (batch_uuid,),
                        )
                        row = cur.fetchone()
                        if not row:
                            _json_response(
                                self, {"error": "Not found"}, 404
                            )
                            return
                        d = _ai_resume_batch_row_to_dict(row, cur.description)
                    _json_response(self, d)
                except Exception as e:
                    _json_response(self, {"error": str(e)}, 500)
                return
            # list
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT * FROM ai_resume_batches "
                        "ORDER BY created_at DESC LIMIT 50"
                    )
                    rows = cur.fetchall()
                    desc = cur.description
                result = [_ai_resume_batch_row_to_dict(r, desc) for r in rows]
                _json_response(self, result)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── /api/ai-resume/failure/<job_id> — failed Sonnet inspection ──
        # Returns the captured raw Sonnet output, the validator errors,
        # and a best-effort rendered preview (which the user can edit
        # and save via /api/ai-resume/edit). Used by the Failure
        # Inspection modal opened from the red "AI Failed" badge.
        if self.path.startswith("/api/ai-resume/failure/"):
            job_id = self.path[len("/api/ai-resume/failure/"):].strip()
            if not job_id:
                _json_response(self, {"error": "Missing job ID"}, 400)
                return
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id, company, title, location, work_type, "
                        "salary, posted_date, import_date, description, "
                        "score, tier, job_link, apply_link, "
                        "ai_resume_status, ai_resume_last_raw, "
                        "ai_resume_last_errors "
                        "FROM jobs WHERE id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
                if not row:
                    _json_response(self, {"error": "Job not found"}, 404)
                    return
                (jid, company, title, location, work_type, salary,
                 posted_date, import_date, description, score, tier,
                 job_link, apply_link, status, raw_text,
                 errors_json) = row
                errors_list = []
                if errors_json:
                    if isinstance(errors_json, list):
                        errors_list = errors_json
                    elif isinstance(errors_json, str):
                        try:
                            errors_list = json.loads(errors_json)
                        except Exception:
                            errors_list = [errors_json]

                # Best-effort rendered preview: try to parse the raw
                # Sonnet output and run it through generate_resume_txt
                # so the user can edit a real text resume rather than
                # raw JSON. If the parse fails, fall back to the raw
                # text itself. We also pull out Sonnet's analysis
                # block (if present) so the failure modal can show
                # WHAT Sonnet was trying to do alongside WHY it
                # failed validation.
                preview = None
                preview_source = None  # "rendered" | "raw" | None
                analysis = None
                if raw_text:
                    parsed = _parse_ai_resume_response(raw_text)
                    if parsed.get("ok"):
                        analysis = parsed.get("analysis")
                        try:
                            from process_new_postings import (
                                generate_resume_txt, customize_skills
                            )
                            job_dict = {
                                "id": jid, "company": company,
                                "title": title, "location": location,
                                "work_type": work_type, "salary": salary,
                                "posted_date": posted_date,
                                "import_date": import_date,
                                "description": description or "",
                                "score": score, "tier": tier,
                                "job_link": job_link,
                                "apply_link": apply_link,
                            }
                            langs, frameworks, misc = customize_skills(
                                job_dict["description"]
                            )
                            preview = generate_resume_txt(
                                job_dict, parsed["bullets"],
                                langs, frameworks, misc,
                            )
                            preview_source = "rendered"
                        except Exception as e:
                            preview = raw_text
                            preview_source = "raw"
                            errors_list = list(errors_list) + [
                                f"render preview failed: {str(e)[:200]}"
                            ]
                    else:
                        preview = raw_text
                        preview_source = "raw"

                _json_response(self, {
                    "job_id": jid,
                    "company": company,
                    "title": title,
                    "status": status,
                    "errors": errors_list,
                    "raw": raw_text,
                    "preview": preview,
                    "preview_source": preview_source,
                    "reasoning": analysis,
                })
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
                    cur.execute(
                        "DELETE FROM import_batches WHERE import_date = %s",
                        (batch,)
                    )
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

        # ── POST /api/ai-import-stop — legacy stop (targets live import)
        if self.path == "/api/ai-import-stop":
            # Find the most recently started running live import and stop it
            target_id = None
            with _ai_imports_lock:
                running = [(bid, s) for bid, s in _ai_imports.items()
                           if s.get("running") and s.get("mode") == "live"]
            if running:
                target_id = running[-1][0]
                ev = _ai_import_stop_events.get(target_id)
                if ev:
                    ev.set()
                _mem_update(target_id, {
                    "message": "Stop requested \u2014 finishing remaining jobs with regex fallback\u2026",
                })
                _json_response(self, {"ok": True})
            else:
                _json_response(self, {"ok": True, "message": "Not running"})
            return

        # ── POST /api/import-csv-ai — AI scoring CSV import ─
        # Optional ?mode=batch allows concurrent batch imports.
        # Live mode stays single-runner (rate-limit safety).
        if self.path.startswith("/api/import-csv-ai"):
            if not _ANTHROPIC_KEY:
                _json_response(self, {"error": "Anthropic API key not configured"}, 500)
                return
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                loc = (qs.get("location", [""])[0] or "").strip() or None
                mode = (qs.get("mode", ["live"])[0] or "live").strip().lower()
                # CSV filename — captured so the Imports view can show
                # the original filename instead of the synthesized
                # composite label. Optional for backward compat.
                fname = (qs.get("filename", [""])[0] or "").strip() or None
                length = int(self.headers.get("Content-Length", 0))
                csv_bytes = self.rfile.read(length)
                if not csv_bytes:
                    _json_response(self, {"error": "Empty body"}, 400)
                    return

                # Live mode: single-runner guard
                if mode == "live":
                    with _ai_imports_lock:
                        global _ai_live_import_running
                        if _ai_live_import_running:
                            _json_response(self, {
                                "error": "A live-mode AI import is already running. "
                                         "Use batch mode for concurrent imports."
                            }, 409)
                            return
                        _ai_live_import_running = True

                batch_uuid, label = _new_import_row(
                    mode=mode, location=loc, csv_filename=fname
                )
                stop_event = threading.Event()
                with _ai_imports_lock:
                    _ai_imports[batch_uuid] = {
                        "id": batch_uuid, "status": "queued", "mode": mode,
                        "import_date": label, "location": loc,
                        "running": False, "progress": 0, "total": 0, "added": 0,
                        "scored_ai": 0, "scored_fallback": 0, "estimated_cost": 0.0,
                        "regex_agree": 0, "ai_promoted": 0, "ai_demoted": 0,
                        "stopped": False, "last_error": "",
                        "batch_id": None, "batch_processing_status": None,
                        "batch_request_counts": None, "message": "Queued",
                        "jobs_found": [],
                    }
                    _ai_import_stop_events[batch_uuid] = stop_event

                target = (_run_import_csv_ai_batch if mode == "batch"
                          else _run_import_csv_ai)
                threading.Thread(
                    target=target,
                    args=(batch_uuid, csv_bytes, loc),
                    daemon=True,
                ).start()
                _json_response(self, {"ok": True, "mode": mode, "id": batch_uuid,
                                      "import_date": label})
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-resume/estimate — cost preview ──
        # Body: {job_ids: [...]}. Returns counts + batch/live cost.
        if self.path == "/api/ai-resume/estimate":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                job_ids = body.get("job_ids") or []
                if not isinstance(job_ids, list):
                    _json_response(self, {"error": "job_ids must be a list"}, 400)
                    return
                n = len(job_ids)
                _json_response(self, {
                    "count": n,
                    "cost_batch": round(n * AI_RESUME_COST_PER_JOB_BATCH, 4),
                    "cost_live": round(n * AI_RESUME_COST_PER_JOB_LIVE, 4),
                })
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-resume/batch — submit a Sonnet batch ──
        # Body: {job_ids: [...]}. Spawns a background thread and
        # returns a batch UUID immediately. Client polls
        # /api/ai-resume/batches/<uuid> for status.
        if self.path == "/api/ai-resume/batch":
            if not _ANTHROPIC_KEY:
                _json_response(
                    self,
                    {"error": "Anthropic API key not configured"},
                    500,
                )
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                job_ids = body.get("job_ids") or []
                if not isinstance(job_ids, list) or not job_ids:
                    _json_response(
                        self,
                        {"error": "job_ids must be a non-empty list"},
                        400,
                    )
                    return
                # De-dup while preserving order
                seen = set()
                dedup_ids = []
                for j in job_ids:
                    if j and j not in seen:
                        seen.add(j)
                        dedup_ids.append(j)
                batch_uuid = _new_ai_resume_batch_row(dedup_ids)
                threading.Thread(
                    target=_run_ai_resume_batch,
                    args=(batch_uuid, dedup_ids),
                    daemon=True,
                ).start()
                _json_response(self, {
                    "ok": True,
                    "batch_uuid": batch_uuid,
                    "count": len(dedup_ids),
                })
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-resume/batches/<uuid>/cancel ──
        if (self.path.startswith("/api/ai-resume/batches/")
                and self.path.endswith("/cancel")):
            try:
                path = self.path[len("/api/ai-resume/batches/"):]
                batch_uuid = path[:-len("/cancel")]
                ev = _ai_resume_batch_stop_events.get(batch_uuid)
                if ev:
                    ev.set()
                    _mem_update_ai_resume(batch_uuid, {
                        "message": ("Stop requested — polling will stop "
                                    "on next tick. Anthropic batch still "
                                    "running."),
                    })
                    _json_response(self, {"ok": True})
                else:
                    _json_response(
                        self,
                        {"ok": True, "message": "Not running in this process"},
                    )
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-resume/regenerate — live single-job rerun ──
        # Body: {job_id}. Synchronous; blocked if the job is in a pending batch.
        if self.path == "/api/ai-resume/regenerate":
            if not _ANTHROPIC_KEY:
                _json_response(
                    self,
                    {"error": "Anthropic API key not configured"},
                    500,
                )
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                job_id = (body.get("job_id") or "").strip()
                if not job_id:
                    _json_response(self, {"error": "job_id required"}, 400)
                    return
                ok, message = _regenerate_ai_resume_live(job_id)
                _json_response(
                    self,
                    {"ok": ok, "message": message},
                    200 if ok else 400,
                )
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        # ── POST /api/ai-resume/edit — plain textarea save ──
        # Body: {job_id, resume_text}. Overwrites jobs.resume_text and the
        # on-disk resume.txt. Promotes 'failed' jobs to 'ready' (this is
        # how the user recovers from a Sonnet validation failure) and
        # clears the captured raw output + errors. For jobs that are
        # already 'ready', preserves ai_resume_generated_at so the
        # original generation timestamp survives a manual tweak.
        if self.path == "/api/ai-resume/edit":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                job_id = (body.get("job_id") or "").strip()
                resume_text = body.get("resume_text")
                if not job_id or resume_text is None:
                    _json_response(
                        self,
                        {"error": "job_id and resume_text required"},
                        400,
                    )
                    return
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id, company, title, ai_resume_status "
                        "FROM jobs WHERE id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        _json_response(self, {"error": "job not found"}, 404)
                        return
                    current_status = row[3]
                    if current_status == "failed":
                        # Recovery path: bump to ready, set the
                        # generation timestamp, clear the raw/errors
                        # AND clear reasoning (the user's edit no
                        # longer matches Sonnet's explanation).
                        cur.execute(
                            "UPDATE jobs SET resume_text = %s, "
                            "ai_resume_status = 'ready', "
                            "ai_resume_generated_at = NOW(), "
                            "ai_resume_last_raw = NULL, "
                            "ai_resume_last_errors = NULL, "
                            "ai_resume_reasoning = NULL "
                            "WHERE id = %s",
                            (resume_text, job_id),
                        )
                    else:
                        # Edit-in-place on an already-ready resume:
                        # preserve generated_at, but clear the
                        # reasoning since the user just changed the
                        # text Sonnet was explaining.
                        cur.execute(
                            "UPDATE jobs SET resume_text = %s, "
                            "ai_resume_reasoning = NULL "
                            "WHERE id = %s",
                            (resume_text, job_id),
                        )
                    job = {"id": row[0], "company": row[1], "title": row[2]}
                disk_path = _find_on_disk_resume_path(job)
                if disk_path:
                    try:
                        with open(disk_path, "w") as f:
                            f.write(resume_text)
                    except Exception as e:
                        print(f"/api/ai-resume/edit: disk write failed: {e}")
                _json_response(self, {"ok": True})
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

        # ── POST /api/imports/:id/cancel ─────────────────
        if self.path.startswith("/api/imports/") and self.path.endswith("/cancel"):
            parts = self.path.rstrip("/").split("/")
            # expect ["", "api", "imports", "<uuid>", "cancel"]
            if len(parts) >= 5:
                batch_uuid = parts[3]
                try:
                    with Db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT status, mode, anthropic_batch_id "
                            "FROM import_batches WHERE id = %s",
                            (batch_uuid,)
                        )
                        row = cur.fetchone()
                    if not row:
                        _json_response(self, {"error": "Not found"}, 404)
                        return
                    db_status, mode, anthropic_batch_id = row
                    if db_status not in ("queued", "running"):
                        _json_response(self, {
                            "error": f"Import is already {db_status}"
                        }, 409)
                        return
                    # Best-effort Anthropic cancel for batch mode
                    if mode == "batch" and anthropic_batch_id:
                        try:
                            _get_anthropic_client().messages.batches.cancel(
                                anthropic_batch_id
                            )
                        except Exception as ce:
                            print(f"Anthropic batch cancel failed: {ce}")
                    # Signal the worker to stop
                    with _ai_imports_lock:
                        ev = _ai_import_stop_events.get(batch_uuid)
                    if ev:
                        ev.set()
                    _json_response(self, {"ok": True, "status": "canceling"})
                except Exception as e:
                    _json_response(self, {"error": str(e)}, 500)
            else:
                _json_response(self, {"error": "Invalid path"}, 400)
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

    def do_DELETE(self):
        if not _is_authenticated(self):
            self.send_response(401)
            self.end_headers()
            return

        # ── DELETE /api/imports/:id — delete batch + all its jobs ──
        if self.path.startswith("/api/imports/"):
            parsed = urllib.parse.urlparse(self.path)
            parts = parsed.path.rstrip("/").split("/")
            if len(parts) < 4 or not parts[3]:
                _json_response(self, {"error": "Missing import id"}, 400)
                return
            batch_uuid = parts[3]
            qs = urllib.parse.parse_qs(parsed.query)
            force = qs.get("force", ["0"])[0] == "1"
            try:
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT status, mode, anthropic_batch_id, import_date "
                        "FROM import_batches WHERE id = %s",
                        (batch_uuid,)
                    )
                    row = cur.fetchone()
                if not row:
                    _json_response(self, {"error": "Not found"}, 404)
                    return
                db_status, mode, anthropic_batch_id, import_date = row
                if db_status in ("queued", "running") and not force:
                    _json_response(self, {
                        "error": "Import is still running. Add ?force=1 to delete anyway."
                    }, 409)
                    return
                # Stop the worker if active
                if db_status in ("queued", "running"):
                    if mode == "batch" and anthropic_batch_id:
                        try:
                            _get_anthropic_client().messages.batches.cancel(
                                anthropic_batch_id
                            )
                        except Exception as ce:
                            print(f"Anthropic batch cancel on delete failed: {ce}")
                    with _ai_imports_lock:
                        ev = _ai_import_stop_events.get(batch_uuid)
                    if ev:
                        ev.set()
                # Cascade delete
                with Db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id FROM jobs WHERE import_date = %s",
                        (import_date,)
                    )
                    job_ids = [r[0] for r in cur.fetchall()]
                    if job_ids:
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
                    cur.execute(
                        "DELETE FROM import_batches WHERE id = %s",
                        (batch_uuid,)
                    )
                # Remove from memory
                with _ai_imports_lock:
                    _ai_imports.pop(batch_uuid, None)
                    _ai_import_stop_events.pop(batch_uuid, None)
                _json_response(self, {
                    "ok": True,
                    "deleted_jobs": len(job_ids) if job_ids else 0
                })
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
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
    _resume_in_flight_imports()
    _resume_in_flight_ai_resume_batches()
    server = http.server.ThreadingHTTPServer(("", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()
