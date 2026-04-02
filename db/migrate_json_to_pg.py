#!/usr/bin/env python3
"""
One-time migration: JSON files → PostgreSQL

Run this from the repo root after the DB schema is applied:
  DB_HOST=<host> DB_PASSWORD=<pw> python3 db/migrate_json_to_pg.py

Source files (read-only, never modified):
  resumes/all_jobs_metadata.json   → jobs + job_state tables
  dashboard/dashboard_state.json   → job_state table (status, notes, timestamps)
  dashboard/application_plans.json → application_plans + application_plan_jobs
  dashboard/company_cache.json     → company_cache table
"""

import json
import os
import sys
import uuid
import logging

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.db import Db, init_pool  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_json(path):
    full = os.path.join(BASE, path)
    if not os.path.exists(full):
        logger.warning("File not found, skipping: %s", full)
        return None
    with open(full) as f:
        return json.load(f)


def migrate_jobs(cursor, jobs_meta, state_map):
    inserted = 0
    skipped = 0
    for job in jobs_meta:
        jid = job.get("id")
        if not jid:
            continue
        cursor.execute("""
            INSERT INTO jobs (id, company, title, location, work_type, salary,
                              posted_date, import_date, description, score, tier,
                              job_link, apply_link, resume_text)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, (
            jid,
            job.get("company"),
            job.get("title"),
            job.get("location"),
            job.get("work_type"),
            job.get("salary"),
            job.get("posted_date"),
            job.get("import_date"),
            job.get("description"),
            job.get("score"),
            job.get("tier"),
            job.get("job_link"),
            job.get("apply_link"),
            job.get("resume_path"),  # store path for now; S3 migration is separate
        ))
        if cursor.rowcount:
            inserted += 1
        else:
            skipped += 1

        # Insert job_state row
        state = state_map.get(jid, {})
        ts = state.get("timestamps", {})
        cursor.execute("""
            INSERT INTO job_state (job_id, status, notes, applicants,
                                   live_status, live_status_checked, timestamps)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (job_id) DO UPDATE SET
              status=EXCLUDED.status,
              notes=EXCLUDED.notes,
              applicants=EXCLUDED.applicants,
              live_status=EXCLUDED.live_status,
              live_status_checked=EXCLUDED.live_status_checked,
              timestamps=EXCLUDED.timestamps,
              updated_at=NOW()
        """, (
            jid,
            state.get("status", "New"),
            state.get("notes"),
            state.get("applicants"),
            state.get("live_status"),
            state.get("live_status_checked"),
            json.dumps(ts),
        ))

    logger.info("Jobs: %d inserted, %d skipped (already existed)", inserted, skipped)


def _to_uuid(val):
    """Convert any string to a valid UUID, using uuid5 if it's not already a UUID."""
    try:
        return str(uuid.UUID(str(val)))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(val)))


def migrate_plans(cursor, plans):
    inserted = 0
    for plan in plans:
        pid = _to_uuid(plan.get("id"))
        cursor.execute("""
            INSERT INTO application_plans (id, title, date)
            VALUES (%s::uuid,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, (pid, plan.get("title"), plan.get("date")))

        for job in plan.get("jobs", []):
            jid = job.get("id")
            # Only insert if job exists in jobs table
            cursor.execute("SELECT 1 FROM jobs WHERE id=%s", (jid,))
            if cursor.fetchone():
                cursor.execute("""
                    INSERT INTO application_plan_jobs (plan_id, job_id, notes)
                    VALUES (%s::uuid,%s,%s)
                    ON CONFLICT DO NOTHING
                """, (pid, jid, job.get("notes")))
        inserted += 1

    logger.info("Plans: %d inserted", inserted)


def migrate_company_cache(cursor, cache):
    inserted = 0
    for name, info in cache.items():
        cursor.execute("""
            INSERT INTO company_cache (company_name, summary, logo_url, industry)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (company_name) DO NOTHING
        """, (name, info.get("summary"), info.get("logo_url"), info.get("industry")))
        if cursor.rowcount:
            inserted += 1
    logger.info("Company cache: %d entries inserted", inserted)


def main():
    logger.info("Starting JSON → PostgreSQL migration")
    init_pool()

    jobs_meta = load_json("resumes/all_jobs_metadata.json") or []
    state_raw = load_json("dashboard/dashboard_state.json") or {}
    plans_raw = load_json("dashboard/application_plans.json") or []
    cache_raw = load_json("dashboard/company_cache.json") or {}

    logger.info("Loaded: %d jobs, %d state entries, %d plans, %d cached companies",
                len(jobs_meta), len(state_raw), len(plans_raw), len(cache_raw))

    with Db() as conn:
        cur = conn.cursor()
        migrate_jobs(cur, jobs_meta, state_raw)
        migrate_plans(cur, plans_raw)
        migrate_company_cache(cur, cache_raw)

        # Summary
        cur.execute("SELECT COUNT(*) FROM jobs")
        job_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM job_state")
        state_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM application_plans")
        plan_count = cur.fetchone()[0]

    logger.info("Migration complete. DB totals: %d jobs, %d state rows, %d plans",
                job_count, state_count, plan_count)


if __name__ == "__main__":
    main()
